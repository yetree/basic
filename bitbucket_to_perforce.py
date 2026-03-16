#!/usr/bin/env python3
"""
Bitbucket PR to Perforce Submit Script

Syncs merged Bitbucket PRs to a Perforce depot, one changelist per PR.
Tracks already-submitted PRs in a local state file to support incremental runs.

Usage:
    python bitbucket_to_perforce.py [--dry-run] [--pr-id <id>] [--limit <n>]

Requirements:
    - p4 CLI installed and accessible in PATH
    - Perforce client (workspace) configured
    - Environment variables set (see .env.example)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Bitbucket config ────────────────────────────────────────────────────────
BITBUCKET_URL = os.getenv("BITBUCKET_URL", "https://api.bitbucket.org/2.0")
BITBUCKET_WORKSPACE = os.getenv("BITBUCKET_WORKSPACE", "")
BITBUCKET_REPO_SLUG = os.getenv("BITBUCKET_REPO_SLUG", "")
BITBUCKET_USERNAME = os.getenv("BITBUCKET_USERNAME", "")
BITBUCKET_APP_PASSWORD = os.getenv("BITBUCKET_APP_PASSWORD", "")

# ── Perforce config ─────────────────────────────────────────────────────────
P4PORT = os.getenv("P4PORT", "localhost:1666")
P4USER = os.getenv("P4USER", "")
P4CLIENT = os.getenv("P4CLIENT", "")
P4PASSWD = os.getenv("P4PASSWD", "")
P4DEPOT_BASE = os.getenv("P4DEPOT_BASE", "//depot")

# ── State file ───────────────────────────────────────────────────────────────
STATE_FILE = Path(os.getenv("STATE_FILE", "submitted_prs.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bitbucket_to_perforce.log"),
    ],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  State management
# ═══════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"submitted_pr_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def mark_pr_submitted(pr_id: int, changelist: str):
    state = load_state()
    entry = {"pr_id": pr_id, "changelist": changelist}
    if entry not in state["submitted_pr_ids"]:
        state["submitted_pr_ids"].append(entry)
    save_state(state)


def is_pr_submitted(pr_id: int) -> bool:
    state = load_state()
    return any(e["pr_id"] == pr_id for e in state["submitted_pr_ids"])


# ═══════════════════════════════════════════════════════════════════════════
#  Perforce helpers
# ═══════════════════════════════════════════════════════════════════════════

def _p4_env() -> dict:
    env = os.environ.copy()
    env.update({
        "P4PORT": P4PORT,
        "P4USER": P4USER,
        "P4CLIENT": P4CLIENT,
        "P4PASSWD": P4PASSWD,
    })
    return env


def run_p4(args: list[str], input_text: str = None, check: bool = True) -> subprocess.CompletedProcess:
    """Execute a p4 command and return the result."""
    cmd = ["p4"] + args
    logger.debug("p4 %s", " ".join(args))
    result = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        env=_p4_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"p4 {' '.join(args)} failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return result


def p4_get_workspace_root() -> str:
    result = run_p4(["client", "-o"])
    for line in result.stdout.splitlines():
        if line.startswith("Root:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("Cannot determine P4 workspace root from 'p4 client -o'")


def p4_file_exists(depot_path: str) -> bool:
    result = run_p4(["files", depot_path], check=False)
    return result.returncode == 0 and result.stdout.strip() != ""


def p4_create_changelist(description: str) -> str:
    """Create an empty numbered changelist and return its ID."""
    # Indent every line of description after the first with a tab (p4 spec format)
    indented = description.replace("\n", "\n\t")
    spec = (
        f"Change:\tnew\n"
        f"Client:\t{P4CLIENT}\n"
        f"User:\t{P4USER}\n"
        f"Status:\tnew\n"
        f"Description:\n\t{indented}\n"
        f"Files:\n"
    )
    result = run_p4(["change", "-i"], input_text=spec)
    # Output: "Change 42 created."
    for token in result.stdout.split():
        if token.isdigit():
            return token
    raise RuntimeError(f"Unexpected 'p4 change -i' output: {result.stdout!r}")


def p4_revert_and_delete_changelist(changelist: str):
    run_p4(["revert", "-c", changelist, "//..."], check=False)
    run_p4(["change", "-d", changelist], check=False)


def p4_add(local_path: Path, changelist: str):
    run_p4(["add", "-c", changelist, str(local_path)])


def p4_edit(local_path: Path, changelist: str):
    run_p4(["edit", "-c", changelist, str(local_path)])


def p4_delete(local_path: Path, changelist: str):
    run_p4(["delete", "-c", changelist, str(local_path)])


def p4_submit(changelist: str) -> bool:
    result = run_p4(["submit", "-c", changelist], check=False)
    if result.returncode != 0:
        logger.error("p4 submit failed:\n%s", result.stderr.strip())
        return False
    logger.info("Submitted changelist %s: %s", changelist, result.stdout.strip())
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  Bitbucket client
# ═══════════════════════════════════════════════════════════════════════════

class BitbucketClient:
    def __init__(self):
        self._auth = (BITBUCKET_USERNAME, BITBUCKET_APP_PASSWORD)
        self._base = (
            f"{BITBUCKET_URL}/repositories"
            f"/{BITBUCKET_WORKSPACE}/{BITBUCKET_REPO_SLUG}"
        )

    def _get(self, path: str, **params) -> dict:
        url = f"{self._base}{path}"
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, auth=self._auth, params=params)
            resp.raise_for_status()
            return resp.json()

    def get_merged_prs(self, limit: int = 100) -> list[dict]:
        """Return merged PRs sorted oldest-first."""
        data = self._get(
            "/pullrequests",
            state="MERGED",
            pagelen=min(limit, 50),
            sort="updated_on",
        )
        prs = data.get("values", [])
        # Handle pagination
        while data.get("next") and len(prs) < limit:
            with httpx.Client(timeout=30) as client:
                resp = client.get(data["next"], auth=self._auth)
                resp.raise_for_status()
                data = resp.json()
                prs.extend(data.get("values", []))
        prs.sort(key=lambda p: p.get("updated_on", ""))
        return prs[:limit]

    def get_pr(self, pr_id: int) -> dict:
        return self._get(f"/pullrequests/{pr_id}")

    def get_pr_diffstat(self, pr_id: int) -> list[dict]:
        """Return file-level diff stats for a PR."""
        data = self._get(f"/pullrequests/{pr_id}/diffstat")
        entries = data.get("values", [])
        while data.get("next"):
            with httpx.Client(timeout=30) as client:
                resp = client.get(data["next"], auth=self._auth)
                resp.raise_for_status()
                data = resp.json()
                entries.extend(data.get("values", []))
        return entries

    def get_file_content(self, commit_hash: str, file_path: str) -> Optional[bytes]:
        url = (
            f"{BITBUCKET_URL}/repositories"
            f"/{BITBUCKET_WORKSPACE}/{BITBUCKET_REPO_SLUG}"
            f"/src/{commit_hash}/{file_path}"
        )
        with httpx.Client(timeout=60) as client:
            resp = client.get(url, auth=self._auth)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.content


# ═══════════════════════════════════════════════════════════════════════════
#  Core sync logic
# ═══════════════════════════════════════════════════════════════════════════

def depot_path_for(file_path: str) -> str:
    return f"{P4DEPOT_BASE}/{file_path}"


def local_path_for(workspace_root: str, file_path: str) -> Path:
    return Path(workspace_root) / file_path


def submit_pr(
    pr: dict,
    bb: BitbucketClient,
    workspace_root: str,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Submit a single Bitbucket PR as a Perforce changelist.
    Returns the changelist number on success, None on failure.
    """
    pr_id = pr["id"]
    pr_title = pr["title"]
    pr_author = pr.get("author", {}).get("display_name", "unknown")
    pr_url = pr.get("links", {}).get("html", {}).get("href", "")
    merge_commit = (pr.get("merge_commit") or {}).get("hash", "")
    pr_description = (pr.get("description") or "").strip()

    if not merge_commit:
        logger.warning("PR #%d has no merge_commit, skipping.", pr_id)
        return None

    logger.info("─── PR #%d: %s", pr_id, pr_title)

    diff_stats = bb.get_pr_diffstat(pr_id)
    if not diff_stats:
        logger.warning("PR #%d has no file changes, skipping.", pr_id)
        return None

    # Build changelist description
    cl_description = (
        f"PR #{pr_id}: {pr_title}\n\n"
        f"Author:       {pr_author}\n"
        f"Merge commit: {merge_commit}\n"
        f"URL:          {pr_url}\n"
    )
    if pr_description:
        cl_description += f"\n{pr_description}\n"

    if dry_run:
        logger.info("[DRY RUN] Would create changelist for PR #%d", pr_id)
        for ds in diff_stats:
            status = ds.get("status", "?")
            new_path = (ds.get("new") or {}).get("path", "")
            old_path = (ds.get("old") or {}).get("path", "")
            path_display = new_path or old_path
            logger.info("  [%s] %s", status, path_display)
        return "DRY_RUN"

    changelist = p4_create_changelist(cl_description)
    logger.info("Created changelist %s", changelist)

    try:
        for ds in diff_stats:
            status = ds.get("status")           # added | modified | removed | renamed | merge conflict
            new_info = ds.get("new") or {}
            old_info = ds.get("old") or {}
            new_path = new_info.get("path", "")
            old_path = old_info.get("path", "")

            if status == "removed":
                local = local_path_for(workspace_root, old_path)
                if local.exists():
                    p4_delete(local, changelist)
                    logger.info("  DELETE %s", old_path)
                else:
                    logger.warning("  DELETE skipped (not in workspace): %s", old_path)

            elif status in ("added", "modified", "merge conflict"):
                target_path = new_path or old_path
                content = bb.get_file_content(merge_commit, target_path)
                if content is None:
                    logger.warning("  Could not fetch %s, skipping.", target_path)
                    continue

                local = local_path_for(workspace_root, target_path)
                local.parent.mkdir(parents=True, exist_ok=True)

                if status == "modified" and local.exists():
                    p4_edit(local, changelist)
                    local.write_bytes(content)
                    logger.info("  EDIT %s", target_path)
                else:
                    # File may already exist in depot even on "added" (edge case)
                    if local.exists() and p4_file_exists(depot_path_for(target_path)):
                        p4_edit(local, changelist)
                        local.write_bytes(content)
                        logger.info("  EDIT (was add) %s", target_path)
                    else:
                        local.write_bytes(content)
                        p4_add(local, changelist)
                        logger.info("  ADD  %s", target_path)

            elif status == "renamed":
                # Delete old path, add new path
                old_local = local_path_for(workspace_root, old_path)
                if old_local.exists():
                    p4_delete(old_local, changelist)
                    logger.info("  DELETE (rename from) %s", old_path)

                content = bb.get_file_content(merge_commit, new_path)
                if content:
                    new_local = local_path_for(workspace_root, new_path)
                    new_local.parent.mkdir(parents=True, exist_ok=True)
                    new_local.write_bytes(content)
                    p4_add(new_local, changelist)
                    logger.info("  ADD   (rename to) %s", new_path)

            else:
                logger.warning("  Unknown diff status '%s' for %s, skipping.", status, new_path or old_path)

    except Exception:
        logger.exception("Error while preparing changelist %s, reverting.", changelist)
        p4_revert_and_delete_changelist(changelist)
        raise

    if not p4_submit(changelist):
        p4_revert_and_delete_changelist(changelist)
        return None

    return changelist


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

def validate_config():
    required = {
        "BITBUCKET_WORKSPACE": BITBUCKET_WORKSPACE,
        "BITBUCKET_REPO_SLUG": BITBUCKET_REPO_SLUG,
        "BITBUCKET_USERNAME": BITBUCKET_USERNAME,
        "BITBUCKET_APP_PASSWORD": BITBUCKET_APP_PASSWORD,
        "P4PORT": P4PORT,
        "P4USER": P4USER,
        "P4CLIENT": P4CLIENT,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Submit Bitbucket PRs to Perforce depot, one changelist per PR."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be submitted without making any changes.",
    )
    parser.add_argument(
        "--pr-id",
        type=int,
        metavar="ID",
        help="Process a single PR by its Bitbucket PR ID.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        metavar="N",
        help="Maximum number of merged PRs to process (default: 100).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-submit PRs that were already processed (ignores state file).",
    )
    args = parser.parse_args()

    validate_config()

    try:
        workspace_root = p4_get_workspace_root()
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)
    logger.info("Perforce workspace root: %s", workspace_root)

    # Sync workspace to get latest depot state
    if not args.dry_run:
        logger.info("Syncing Perforce workspace...")
        result = run_p4(["sync"], check=False)
        if result.returncode != 0:
            logger.warning("p4 sync warning: %s", result.stderr.strip())

    bb = BitbucketClient()

    if args.pr_id:
        prs = [bb.get_pr(args.pr_id)]
        logger.info("Processing single PR #%d", args.pr_id)
    else:
        logger.info("Fetching merged PRs from Bitbucket...")
        prs = bb.get_merged_prs(limit=args.limit)
        logger.info("Found %d merged PR(s).", len(prs))

    submitted = 0
    skipped = 0
    failed = 0

    for pr in prs:
        pr_id = pr["id"]

        if not args.force and is_pr_submitted(pr_id):
            logger.info("PR #%d already submitted, skipping (use --force to re-submit).", pr_id)
            skipped += 1
            continue

        try:
            cl = submit_pr(pr, bb, workspace_root, dry_run=args.dry_run)
            if cl:
                if not args.dry_run:
                    mark_pr_submitted(pr_id, cl)
                submitted += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("Failed to submit PR #%d: %s", pr_id, e)
            failed += 1

    logger.info(
        "\n═══ Done ═══  Submitted: %d  |  Skipped: %d  |  Failed: %d",
        submitted, skipped, failed,
    )

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

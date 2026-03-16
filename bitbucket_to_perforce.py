#!/usr/bin/env python3
"""
Bitbucket PR → Perforce Submit (git-based)

머지 커밋을 PR 단위로 인식하여 Perforce에 changelist를 하나씩 제출합니다.
Bitbucket API 없이 로컬 git clone만 있으면 됩니다.

동작 방식:
  1. git log --merges 로 머지 커밋(= PR) 목록을 오래된 순으로 조회
  2. 각 머지 커밋의 두 부모 사이 diff로 변경 파일 파악
  3. git show <hash>:<file> 로 최종 파일 내용 추출
  4. Perforce에 changelist 생성 → 파일 반영 → submit

Usage:
    python bitbucket_to_perforce.py [--dry-run] [--limit N] [--force]
                                    [--since <commit>] [--branch <name>]

Requirements:
    - git 설치 및 PATH 등록
    - p4 CLI 설치 및 PATH 등록
    - Perforce client(workspace) 사전 생성
    - .env 파일 (GIT_REPO_PATH, P4PORT, P4USER, P4CLIENT 등)
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Git 설정 ─────────────────────────────────────────────────────────────────
GIT_REPO_PATH = os.getenv("GIT_REPO_PATH", ".")          # 로컬 git 저장소 경로
GIT_BRANCH    = os.getenv("GIT_BRANCH", "main")           # 조회할 브랜치
GIT_REMOTE    = os.getenv("GIT_REMOTE", "origin")         # fetch 대상 remote

# ── Perforce 설정 ─────────────────────────────────────────────────────────────
P4PORT       = os.getenv("P4PORT", "localhost:1666")
P4USER       = os.getenv("P4USER", "")
P4CLIENT     = os.getenv("P4CLIENT", "")
P4PASSWD     = os.getenv("P4PASSWD", "")
P4DEPOT_BASE = os.getenv("P4DEPOT_BASE", "//depot")       # 예: //depot/myproject

# ── 상태 파일 ─────────────────────────────────────────────────────────────────
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
#  상태 관리
# ═══════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"submitted": []}


def is_submitted(merge_hash: str) -> bool:
    return any(e["hash"] == merge_hash for e in load_state()["submitted"])


def mark_submitted(merge_hash: str, changelist: str, subject: str):
    state = load_state()
    state["submitted"].append({
        "hash": merge_hash,
        "changelist": changelist,
        "subject": subject,
    })
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ═══════════════════════════════════════════════════════════════════════════
#  Git 헬퍼
# ═══════════════════════════════════════════════════════════════════════════

def run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", GIT_REPO_PATH] + args
    logger.debug("git %s", " ".join(args))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed:\n{result.stderr.strip()}"
        )
    return result


def git_fetch():
    """remote에서 최신 커밋 가져오기."""
    logger.info("git fetch %s %s ...", GIT_REMOTE, GIT_BRANCH)
    run_git(["fetch", GIT_REMOTE, GIT_BRANCH], check=False)


def git_merge_commits(branch: str, since: Optional[str] = None, limit: int = 200) -> list[dict]:
    """
    브랜치의 머지 커밋 목록을 오래된 순으로 반환.

    반환 형식:
        [{"hash": str, "parents": [str, str], "subject": str,
          "author": str, "date": str}, ...]
    """
    fmt = "%H%x00%P%x00%s%x00%aN%x00%aI"   # NUL 구분자로 필드 분리
    cmd = ["log", "--merges", f"--format={fmt}", "--reverse"]
    if since:
        cmd += [f"{since}..{branch}"]
    else:
        cmd += [branch]
    if limit:
        cmd += [f"-{limit}"]

    result = run_git(cmd)
    commits = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) < 5:
            continue
        commit_hash, parents_raw, subject, author, date = parts
        parents = parents_raw.split()
        if len(parents) < 2:
            continue   # 진짜 머지 커밋이 아닌 경우 스킵
        commits.append({
            "hash": commit_hash,
            "parents": parents,
            "subject": subject,
            "author": author,
            "date": date,
        })
    return commits


def git_diff_files(parent1: str, parent2: str) -> list[dict]:
    """
    두 커밋 사이의 파일 변경 목록 반환.

    반환 형식:
        [{"status": "A"|"M"|"D"|"R", "old_path": str, "new_path": str}, ...]
    """
    result = run_git(["diff", "--name-status", "-M", parent1, parent2])
    files = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status_raw = parts[0]

        if status_raw.startswith("R"):          # Rename (R90, R100 등)
            old_path = parts[1] if len(parts) > 1 else ""
            new_path = parts[2] if len(parts) > 2 else ""
            files.append({"status": "R", "old_path": old_path, "new_path": new_path})
        elif status_raw == "D":
            files.append({"status": "D", "old_path": parts[1], "new_path": ""})
        elif status_raw in ("A", "M", "C"):
            files.append({"status": status_raw, "old_path": "", "new_path": parts[1]})
        else:
            logger.warning("알 수 없는 diff 상태 '%s', 건너뜀.", status_raw)
    return files


def git_file_content(commit_hash: str, file_path: str) -> Optional[bytes]:
    """특정 커밋 시점의 파일 내용 반환. 파일이 없으면 None."""
    result = run_git(["show", f"{commit_hash}:{file_path}"], check=False)
    if result.returncode != 0:
        return None
    # binary 모드로 다시 실행
    cmd = ["git", "-C", GIT_REPO_PATH, "show", f"{commit_hash}:{file_path}"]
    result = subprocess.run(cmd, capture_output=True)
    return result.stdout if result.returncode == 0 else None


def extract_pr_number(subject: str) -> str:
    """커밋 메시지에서 PR 번호 추출 (Bitbucket 스타일 포함)."""
    # Bitbucket: "Merged in feature/foo (pull request #42)"
    # GitHub:    "Merge pull request #42 from ..."
    m = re.search(r"#(\d+)", subject)
    return f"#{m.group(1)}" if m else ""


# ═══════════════════════════════════════════════════════════════════════════
#  Perforce 헬퍼
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
    cmd = ["p4"] + args
    logger.debug("p4 %s", " ".join(args))
    result = subprocess.run(
        cmd, input=input_text, capture_output=True, text=True, env=_p4_env()
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"p4 {' '.join(args)} failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return result


def p4_workspace_root() -> str:
    result = run_p4(["client", "-o"])
    for line in result.stdout.splitlines():
        if line.startswith("Root:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("p4 client -o에서 Root를 찾을 수 없습니다.")


def p4_file_in_depot(depot_path: str) -> bool:
    result = run_p4(["files", depot_path], check=False)
    return result.returncode == 0 and result.stdout.strip() != ""


def p4_create_changelist(description: str) -> str:
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
    for token in result.stdout.split():
        if token.isdigit():
            return token
    raise RuntimeError(f"p4 change -i 출력 파싱 실패: {result.stdout!r}")


def p4_revert_delete(changelist: str):
    run_p4(["revert", "-c", changelist, "//..."], check=False)
    run_p4(["change", "-d", changelist], check=False)


def p4_submit(changelist: str) -> bool:
    result = run_p4(["submit", "-c", changelist], check=False)
    if result.returncode != 0:
        logger.error("p4 submit 실패:\n%s", result.stderr.strip())
        return False
    logger.info("Changelist %s 제출 완료: %s", changelist, result.stdout.strip())
    return True


def local_path(workspace_root: str, file_path: str) -> Path:
    return Path(workspace_root) / file_path


def depot_path(file_path: str) -> str:
    return f"{P4DEPOT_BASE}/{file_path}"


# ═══════════════════════════════════════════════════════════════════════════
#  PR 단위 submit
# ═══════════════════════════════════════════════════════════════════════════

def submit_merge_commit(
    commit: dict,
    workspace_root: str,
    dry_run: bool = False,
) -> Optional[str]:
    """
    하나의 머지 커밋(= PR)을 Perforce changelist로 제출.
    성공 시 changelist 번호 반환, 실패 시 None.
    """
    merge_hash = commit["hash"]
    parents     = commit["parents"]
    subject     = commit["subject"]
    author      = commit["author"]
    date        = commit["date"]
    pr_num      = extract_pr_number(subject)

    logger.info("─── %s %s %s", merge_hash[:8], pr_num, subject)

    # diff: 첫 번째 부모(main/master)와 두 번째 부모(feature branch) 사이
    base_parent   = parents[0]   # main 쪽
    feature_tip   = parents[1]   # feature 브랜치 tip

    changed_files = git_diff_files(base_parent, merge_hash)
    if not changed_files:
        logger.warning("  변경 파일 없음, 건너뜀.")
        return None

    cl_description = (
        f"[{pr_num}] {subject}\n\n"
        f"Author:      {author}\n"
        f"Date:        {date}\n"
        f"Merge hash:  {merge_hash}\n"
        f"Base parent: {base_parent}\n"
    )

    if dry_run:
        logger.info("  [DRY RUN] changelist 생성 예정")
        for f in changed_files:
            path = f["new_path"] or f["old_path"]
            logger.info("    [%s] %s", f["status"], path)
        return "DRY_RUN"

    changelist = p4_create_changelist(cl_description)
    logger.info("  Changelist %s 생성", changelist)

    try:
        for f in changed_files:
            status   = f["status"]
            new_path = f["new_path"]
            old_path = f["old_path"]

            if status == "D":
                lp = local_path(workspace_root, old_path)
                if lp.exists():
                    run_p4(["delete", "-c", changelist, str(lp)])
                    logger.info("    DELETE %s", old_path)
                else:
                    logger.warning("    DELETE 스킵 (워크스페이스에 없음): %s", old_path)

            elif status in ("A", "M", "C"):
                content = git_file_content(merge_hash, new_path)
                if content is None:
                    logger.warning("    내용 조회 실패, 건너뜀: %s", new_path)
                    continue

                lp = local_path(workspace_root, new_path)
                lp.parent.mkdir(parents=True, exist_ok=True)

                in_depot = p4_file_in_depot(depot_path(new_path))

                if status == "M" and in_depot:
                    run_p4(["edit", "-c", changelist, str(lp)])
                    lp.write_bytes(content)
                    logger.info("    EDIT  %s", new_path)
                else:
                    lp.write_bytes(content)
                    run_p4(["add", "-c", changelist, str(lp)])
                    logger.info("    ADD   %s", new_path)

            elif status == "R":
                # 이름 변경: 구 경로 삭제 + 새 경로 추가
                old_lp = local_path(workspace_root, old_path)
                if old_lp.exists():
                    run_p4(["delete", "-c", changelist, str(old_lp)])
                    logger.info("    DELETE (rename from) %s", old_path)

                content = git_file_content(merge_hash, new_path)
                if content:
                    new_lp = local_path(workspace_root, new_path)
                    new_lp.parent.mkdir(parents=True, exist_ok=True)
                    new_lp.write_bytes(content)
                    run_p4(["add", "-c", changelist, str(new_lp)])
                    logger.info("    ADD   (rename to) %s", new_path)

    except Exception:
        logger.exception("  오류 발생, changelist %s 취소.", changelist)
        p4_revert_delete(changelist)
        raise

    if not p4_submit(changelist):
        p4_revert_delete(changelist)
        return None

    return changelist


# ═══════════════════════════════════════════════════════════════════════════
#  진입점
# ═══════════════════════════════════════════════════════════════════════════

def validate_config():
    required = {"P4PORT": P4PORT, "P4USER": P4USER, "P4CLIENT": P4CLIENT}
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error("필수 환경변수 누락: %s", ", ".join(missing))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="git 머지 커밋을 PR 단위로 Perforce에 submit합니다."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="실제 submit 없이 처리 내용만 출력")
    parser.add_argument("--limit", type=int, default=200, metavar="N",
                        help="최대 처리할 머지 커밋 수 (기본: 200)")
    parser.add_argument("--since", metavar="COMMIT",
                        help="이 커밋 이후의 머지 커밋만 처리 (git log <COMMIT>..HEAD)")
    parser.add_argument("--branch", default=GIT_BRANCH, metavar="NAME",
                        help=f"조회할 git 브랜치 (기본: {GIT_BRANCH})")
    parser.add_argument("--force", action="store_true",
                        help="이미 제출된 커밋도 재처리 (상태 파일 무시)")
    parser.add_argument("--no-fetch", action="store_true",
                        help="git fetch 생략")
    args = parser.parse_args()

    validate_config()

    try:
        ws_root = p4_workspace_root()
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)
    logger.info("Perforce 워크스페이스 경로: %s", ws_root)

    # Perforce 워크스페이스 sync
    if not args.dry_run:
        logger.info("p4 sync 실행...")
        result = run_p4(["sync"], check=False)
        if result.returncode != 0:
            logger.warning("p4 sync 경고: %s", result.stderr.strip())

    # git fetch
    if not args.no_fetch:
        git_fetch()

    # 머지 커밋 목록 조회
    logger.info("브랜치 '%s'의 머지 커밋 조회 중...", args.branch)
    commits = git_merge_commits(args.branch, since=args.since, limit=args.limit)
    logger.info("머지 커밋 %d개 발견.", len(commits))

    submitted = skipped = failed = 0

    for commit in commits:
        h = commit["hash"]

        if not args.force and is_submitted(h):
            logger.info("이미 제출됨 (--force로 재처리 가능): %s %s",
                        h[:8], commit["subject"])
            skipped += 1
            continue

        try:
            cl = submit_merge_commit(commit, ws_root, dry_run=args.dry_run)
            if cl:
                if not args.dry_run:
                    mark_submitted(h, cl, commit["subject"])
                submitted += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("실패: %s %s — %s", h[:8], commit["subject"], e)
            failed += 1

    logger.info(
        "\n═══ 완료 ═══  제출: %d  |  건너뜀: %d  |  실패: %d",
        submitted, skipped, failed,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

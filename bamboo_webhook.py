#!/usr/bin/env python3
"""
Bamboo → Linux Server 웹훅 수신기

흐름:
  Bitbucket push → Bamboo 빌드 → Bamboo POST 알림 (Linux server)
  → git pull master → python bitbucket_to_perforce.py → P4 submit

환경변수 (.env):
  WEBHOOK_SECRET    웹훅 인증 토큰 (Bamboo → Authorization: Bearer <token>)
  GIT_REPO_PATH     git 저장소 경로              (기본: .)
  GIT_BRANCH        pull할 브랜치                (기본: master)
  GIT_REMOTE        git remote 이름              (기본: origin)
  SYNC_SCRIPT       실행할 스크립트 경로          (기본: bitbucket_to_perforce.py)
  SYNC_DRY_RUN      true이면 --dry-run 모드       (기본: false)
  LISTEN_HOST       서버 바인딩 호스트             (기본: 0.0.0.0)
  LISTEN_PORT       서버 포트                     (기본: 8000)

Usage:
    uvicorn bamboo_webhook:app --host 0.0.0.0 --port 8000
    또는
    python bamboo_webhook.py
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────────────
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
GIT_REPO_PATH  = os.getenv("GIT_REPO_PATH", ".")
GIT_BRANCH     = os.getenv("GIT_BRANCH", "master")
GIT_REMOTE     = os.getenv("GIT_REMOTE", "origin")
SYNC_SCRIPT    = os.getenv("SYNC_SCRIPT", str(Path(__file__).parent / "bitbucket_to_perforce.py"))
SYNC_DRY_RUN   = os.getenv("SYNC_DRY_RUN", "false").lower() == "true"
LISTEN_HOST    = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT    = int(os.getenv("LISTEN_PORT", "8000"))

# ── 로깅 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bamboo_webhook.log"),
    ],
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Bamboo Webhook Receiver", version="1.0.0")

# ── 동시 실행 방지 ─────────────────────────────────────────────────────────────
_sync_lock = asyncio.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
#  핵심 로직
# ═══════════════════════════════════════════════════════════════════════════════

def git_pull() -> None:
    """master 브랜치를 최신으로 당깁니다."""
    logger.info("git pull %s %s ...", GIT_REMOTE, GIT_BRANCH)
    result = subprocess.run(
        ["git", "-C", GIT_REPO_PATH, "pull", GIT_REMOTE, GIT_BRANCH],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git pull 실패:\n{result.stderr.strip()}")
    logger.info("git pull 완료:\n%s", result.stdout.strip())


def run_p4_submit() -> None:
    """bitbucket_to_perforce.py 를 실행해 새 커밋을 P4에 submit합니다."""
    cmd = [sys.executable, SYNC_SCRIPT]
    if SYNC_DRY_RUN:
        cmd.append("--dry-run")

    logger.info("P4 submit 스크립트 실행: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    # stdout/stderr 모두 기록
    if result.stdout:
        logger.info("[sync stdout]\n%s", result.stdout.strip())
    if result.stderr:
        logger.warning("[sync stderr]\n%s", result.stderr.strip())

    if result.returncode != 0:
        raise RuntimeError(f"P4 submit 스크립트 비정상 종료 (code={result.returncode})")

    logger.info("P4 submit 완료.")


async def handle_sync(trigger_info: str) -> None:
    """git pull → P4 submit 순서로 실행. 동시 중복 실행 방지."""
    if _sync_lock.locked():
        logger.warning("[%s] 이전 sync가 진행 중입니다. 이번 요청은 건너뜁니다.", trigger_info)
        return

    async with _sync_lock:
        logger.info("=== sync 시작 [%s] ===", trigger_info)
        try:
            await asyncio.to_thread(git_pull)
            await asyncio.to_thread(run_p4_submit)
            logger.info("=== sync 완료 [%s] ===", trigger_info)
        except Exception as exc:
            logger.error("=== sync 실패 [%s]: %s ===", trigger_info, exc)


# ═══════════════════════════════════════════════════════════════════════════════
#  인증 헬퍼
# ═══════════════════════════════════════════════════════════════════════════════

def verify_secret(request: Request) -> None:
    """WEBHOOK_SECRET 이 설정된 경우 Authorization 헤더를 검증합니다."""
    if not WEBHOOK_SECRET:
        return  # 시크릿 미설정 → 인증 건너뜀

    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {WEBHOOK_SECRET}"
    if auth != expected:
        logger.warning("웹훅 인증 실패 (IP: %s)", request.client.host if request.client else "unknown")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# ═══════════════════════════════════════════════════════════════════════════════
#  엔드포인트
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/webhook/bamboo", status_code=status.HTTP_202_ACCEPTED)
async def bamboo_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Bamboo 빌드 완료 알림을 수신합니다.

    Bamboo Notification 설정:
      - Type: HTTP
      - URL : http://<this-server>:8000/webhook/bamboo
      - Event: Build completed (성공 시)
      - Custom Header: Authorization: Bearer <WEBHOOK_SECRET>

    Bamboo가 전송하는 페이로드 예시:
      {
        "build": {
          "buildResultKey": "PRJ-PLAN-42",
          "buildState": "Successful",   ← "Successful" | "Failed" | "Unknown"
          "planName": "My Plan",
          "buildNumber": 42,
          "lifeCycleState": "Finished"
        }
      }

    buildState 가 "Successful" 인 경우에만 sync를 실행합니다.
    페이로드가 없거나 buildState 키가 없으면 무조건 sync를 실행합니다.
    """
    verify_secret(request)

    # 페이로드 파싱 (JSON이 아니어도 허용)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    build_info = payload.get("build", payload)
    build_state = build_info.get("buildState") or build_info.get("status", "")
    build_key   = build_info.get("buildResultKey") or build_info.get("planKey", "unknown")

    logger.info("웹훅 수신 — key=%s, state=%s", build_key, build_state or "(없음)")

    # buildState가 명시적으로 실패인 경우 sync 하지 않음
    if build_state and build_state.lower() not in ("successful", "success"):
        logger.info("빌드 상태 '%s' → sync 생략.", build_state)
        return {"status": "skipped", "reason": f"build state is '{build_state}'"}

    trigger_info = f"{build_key} state={build_state or 'N/A'}"
    background_tasks.add_task(handle_sync, trigger_info)

    return {"status": "accepted", "trigger": trigger_info}


@app.get("/health")
async def health():
    """헬스체크 엔드포인트"""
    return {
        "status": "ok",
        "git_repo": GIT_REPO_PATH,
        "git_branch": GIT_BRANCH,
        "sync_script": SYNC_SCRIPT,
        "dry_run": SYNC_DRY_RUN,
        "sync_busy": _sync_lock.locked(),
    }


@app.get("/")
async def root():
    return {"message": "Bamboo webhook receiver is running. POST /webhook/bamboo"}


# ═══════════════════════════════════════════════════════════════════════════════
#  직접 실행
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("Bamboo webhook receiver 시작 (host=%s port=%d)", LISTEN_HOST, LISTEN_PORT)
    uvicorn.run("bamboo_webhook:app", host=LISTEN_HOST, port=LISTEN_PORT, reload=False)

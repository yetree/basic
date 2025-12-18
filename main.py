from fastapi import FastAPI
import httpx
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

# 메신저 API 설정 (실제 사용하는 메신저 API에 맞게 수정하세요)
MESSENGER_API_URL = "https://your-messenger-api.com/send_text"
MESSENGER_TOKEN = "your_token_here"
RECIPIENT_ID = "your_recipient_id"

# 플랜/태스크 API 설정
PROJECT_ID = "your_project_id"
PLANS_API_URL = "https://your-api.com/plans"
TASKS_API_URL = "https://your-api.com/tasks"


async def send_text(message: str):
    """메시지를 전송하는 함수"""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                MESSENGER_API_URL,
                json={
                    "recipient": {"id": RECIPIENT_ID},
                    "message": {"text": message}
                },
                headers={
                    "Authorization": f"Bearer {MESSENGER_TOKEN}",
                    "Content-Type": "application/json"
                },
                timeout=10.0
            )
            response.raise_for_status()
            print(f"메시지 전송 성공: {message}")
            return response.json()
        except httpx.HTTPError as e:
            print(f"메시지 전송 실패: {e}")
            return None


def _parse_datetime(value: str) -> datetime | None:
    """ISO 날짜 문자열을 datetime으로 변환합니다."""

    if not value:
        return None

    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


async def fetch_plans(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """플랜 목록을 조회합니다."""

    response = await client.get(
        f"{PLANS_API_URL}/{PROJECT_ID}", timeout=10.0
    )
    response.raise_for_status()
    data = response.json()
    # 응답이 배열인 경우를 명확히 처리
    if isinstance(data, list):
        return data
    return data.get("plans", [])


async def fetch_tasks(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """태스크 목록을 조회합니다."""

    response = await client.get(
        TASKS_API_URL, params={"project_id": PROJECT_ID}, timeout=10.0
    )
    response.raise_for_status()
    data = response.json()
    # 응답이 배열인 경우를 명확히 처리
    if isinstance(data, list):
        return data
    return data.get("tasks", [])


async def generate_daily_report() -> str:
    """최근 7일 내 생성되고 플랜에 속한 태스크 일간 리포트를 생성합니다."""

    async with httpx.AsyncClient() as client:
        plans, tasks = await asyncio.gather(fetch_plans(client), fetch_tasks(client))

    plan_lookup: Dict[str, Dict[str, Any]] = {
        str(plan.get("id")): plan for plan in plans if plan.get("id") is not None
    }

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    tasks_by_plan: Dict[str, List[Dict[str, Any]]] = {}

    for task in tasks:
        plan_id = task.get("plan_id") or task.get("planId")
        created_at = _parse_datetime(task.get("created_at") or task.get("createdAt"))

        if not plan_id or created_at is None:
            continue

        if created_at < cutoff:
            continue

        plan_key = str(plan_id)
        tasks_by_plan.setdefault(plan_key, []).append(task)

    if not tasks_by_plan:
        return "최근 7일 내 플랜에 속한 태스크가 없습니다."

    report_lines: List[str] = ["🗓️ 일간 태스크 리포트"]

    for plan_id, plan_tasks in tasks_by_plan.items():
        plan = plan_lookup.get(plan_id, {})
        plan_title = plan.get("title") or plan.get("name") or f"플랜 {plan_id}"
        report_lines.append(f"\n📌 {plan_title} ({len(plan_tasks)}건)")

        for task in plan_tasks:
            task_title = task.get("title") or task.get("name") or "제목 없음"
            created_at = _parse_datetime(task.get("created_at") or task.get("createdAt"))
            created_at_display = (
                created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
                if created_at
                else "날짜 미상"
            )
            report_lines.append(f"- {task_title} (생성: {created_at_display})")

    return "\n".join(report_lines)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 시 실행되는 이벤트"""
    # 시작 시 실행
    print("FastAPI 메신저 봇이 시작되었습니다!")
    await send_text("안녕하세요! 메신저 봇이 시작되었습니다. 🤖")

    yield

    # 종료 시 실행
    print("FastAPI 메신저 봇이 종료되었습니다!")


# FastAPI 앱 생성
app = FastAPI(
    title="메신저 봇 API",
    description="메신저에 메시지를 보내는 봇 API",
    version="1.0.0",
    lifespan=lifespan
)


@app.post("/reports/daily")
async def daily_report():
    """일간 리포트를 생성하고 메신저로 전송합니다."""

    report = await generate_daily_report()
    await send_text(report)
    return {"status": "success", "report": report}


@app.get("/")
async def root():
    """기본 엔드포인트"""
    return {"message": "메신저 봇이 실행 중입니다!"}


@app.post("/send")
async def send_message(message: str):
    """수동으로 메시지를 보내는 엔드포인트"""
    result = await send_text(message)
    if result:
        return {"status": "success", "message": "메시지가 전송되었습니다"}
    else:
        return {"status": "error", "message": "메시지 전송에 실패했습니다"}


@app.get("/health")
async def health_check():
    """헬스체크 엔드포인트"""
    return {"status": "healthy"}

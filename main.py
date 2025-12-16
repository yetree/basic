from fastapi import FastAPI
import httpx
from contextlib import asynccontextmanager

# 메신저 API 설정 (실제 사용하는 메신저 API에 맞게 수정하세요)
MESSENGER_API_URL = "https://your-messenger-api.com/send_text"
MESSENGER_TOKEN = "your_token_here"
RECIPIENT_ID = "your_recipient_id"


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

# FastAPI 메신저 봇

FastAPI로 만든 메신저 봇 애플리케이션입니다. 서버가 시작될 때 자동으로 메시지를 전송합니다.

## 주요 기능

- 🚀 서버 시작 시 자동으로 메시지 전송
- 📤 수동 메시지 전송 API 제공
- 💪 비동기 처리로 빠른 응답

## 설치 방법

```bash
pip install -r requirements.txt
```

## 설정

`main.py` 파일에서 다음 설정을 수정하세요:

```python
MESSENGER_API_URL = "https://your-messenger-api.com/send_text"  # 메신저 API URL
MESSENGER_TOKEN = "your_token_here"  # 메신저 API 토큰
RECIPIENT_ID = "your_recipient_id"  # 수신자 ID
```

## 실행 방법

```bash
uvicorn main:app --reload
```

또는 포트를 지정하여 실행:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## API 엔드포인트

- `GET /` - 기본 엔드포인트 (봇 상태 확인)
- `POST /send?message=메시지내용` - 수동으로 메시지 전송
- `GET /health` - 헬스체크

## 사용 예시

서버가 시작되면 자동으로 "안녕하세요! 메신저 봇이 시작되었습니다. 🤖" 메시지가 전송됩니다.

수동으로 메시지를 보내려면:

```bash
curl -X POST "http://localhost:8000/send?message=안녕하세요"
```

## 주의사항

- 실제 메신저 API (Telegram, Slack, KakaoTalk 등)의 URL과 인증 정보로 수정해야 합니다
- API 키는 환경 변수로 관리하는 것을 권장합니다

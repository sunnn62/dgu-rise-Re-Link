# 실종아동 QR 발견-신고 실시간 채팅 서비스

실종 아동의 옷에 부착된 QR코드를 발견자가 스캔하면, **앱 설치 없이 브라우저에서 바로**
보호자와 실시간으로 소통할 수 있는 서비스입니다. 발견자·보호자 모두 서로의 실명이나
전화번호를 알 수 없이 서버가 익명으로 중계합니다.

- **백엔드**: FastAPI + 내장 WebSocket (Socket.IO 등 외부 실시간 라이브러리 없이 순수 WebSocket)
- **DB**: SQLite (개발용) — SQLAlchemy ORM 사용, `DATABASE_URL`만 바꾸면 PostgreSQL로 전환 가능
- **프론트**: 순수 HTML/CSS/JS (Jinja2 서버 렌더링), 모바일 반응형
- **알림**: SMS는 목업(콘솔 로그) — 인터페이스만 맞춰두어 실제 API로 쉽게 교체 가능

---

## 1. 실행 방법 (Windows PowerShell 기준)

프로젝트 폴더에는 이미 `.venv` 가상환경이 준비되어 있습니다.

### 의존성 설치 (최초 1회, 또는 새 환경에서)

```powershell
cd "c:\Users\Mint\OneDrive\바탕 화면\medical-ai-hackathon\relink"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 서버 실행

```powershell
cd "c:\Users\Mint\OneDrive\바탕 화면\medical-ai-hackathon\relink"
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

`Application startup complete` 메시지가 뜨면 준비 완료입니다.
이 터미널은 계속 켜두세요 — **SMS 목업 로그(보호자 링크 포함)가 여기에 출력**됩니다.

> `--reload`는 개발용(코드 저장 시 자동 재시작)입니다. 운영에서는 제거하세요.

---

## 2. 사용 흐름 & 테스트 방법

로컬에서 WebSocket 실시간 채팅을 확인하려면 **브라우저 창을 2개**(발견자용 / 보호자용)
띄워 동시에 접속합니다. 한쪽은 일반 창, 다른 쪽은 시크릿(인코그니토) 창을 쓰면 편합니다.

### 2-1. 관리자: 아이 등록 & QR 발급

새 터미널에서:

```powershell
$body = @{ name="김철수"; guardian_phone="010-1234-5678"; guardian_name="김보호" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8000/admin/children" -Method Post -ContentType "application/json" -Body $body
```

응답 예시:

```json
{
  "qr_token": "Bec04zb83ZuErbiE...",
  "found_url": "http://localhost:8000/found/Bec04zb83ZuErbiE...",
  "qr_download_url": "/admin/children/Bec04zb83ZuErbiE.../qr"
}
```

- **`qr_token`** 을 복사해 둡니다.
- QR 이미지가 필요하면 브라우저에서 `http://127.0.0.1:8000/admin/children/{qr_token}/qr` 를 열면
  PNG가 다운로드됩니다. (실제로는 이 QR을 아이 옷에 부착)

### 2-2. 발견자: QR 스캔 → 발견 신고

브라우저(창 1)에서:

```
http://127.0.0.1:8000/found/{qr_token}
```

- 상단에 "위험한 상황이면 먼저 112에 신고" 안내가 고정 표시됩니다.
- **"아이를 발견했어요, 보호자와 연결하기"** 버튼을 누르면 채팅방이 생성되고 발견자 채팅 화면으로 이동합니다.

### 2-3. 보호자: SMS 링크로 입장

발견 버튼을 누르는 순간, **서버 터미널(1번)에 보호자 입장 링크가 출력**됩니다:

```
[SMS 목업 발송] to=*******5678 message=[아이발견알림] ... http://localhost:8000/chat/{room_id}?role=guardian&token={guardian_token}
```

이 링크를 복사해 **다른 브라우저 창(창 2, 시크릿 권장)** 에 붙여넣으면 보호자로 입장합니다.

### 2-4. 실시간 채팅 확인

- 두 창에서 메시지를 주고받으면 실시간으로 상대 화면에 나타납니다.
- 발견자 화면 하단의 **"📍 내 위치 공유하기"** 버튼 → 위치 권한 허용 → 지도 링크가 채팅으로 전송됩니다. (보호자 화면에는 이 버튼이 없습니다)
- 상단에 연결 상태("연결됨")가 표시되고, 연결이 끊기면 자동으로 최대 3회 재연결합니다.
- 서로의 실명·전화번호는 노출되지 않고 "발견자" / "보호자" 로만 표시됩니다.

### 2-5. 채팅방 종료 (관리자)

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/admin/rooms/{room_id}/close" -Method Post
```

종료하면 접속 중인 사용자에게 안내가 뜨고 연결이 닫히며, 이후 재접속은 거부됩니다.

---

## 3. 라우트 요약

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/admin/children` | 아이 등록 + QR 토큰 발급 |
| GET | `/admin/children/{qr_token}/qr` | QR 이미지(PNG) 다운로드 |
| POST | `/admin/rooms/{room_id}/close` | 채팅방 종료(closed) |
| GET | `/found/{qr_token}` | 발견 신고 랜딩 페이지 |
| POST | `/found/{qr_token}/start` | 발견 신고 시작(채팅방 생성 + 보호자 SMS) |
| GET | `/chat/{room_id}?role=finder` | 발견자 채팅 화면 |
| GET | `/chat/{room_id}?role=guardian&token=...` | 보호자 채팅 화면(토큰 필요) |
| WS | `/ws/chat/{room_id}?role=...` | 실시간 채팅 WebSocket |

> QR 다운로드 경로는 명세의 `/admin/children/{id}/qr` 대신 **`/admin/children/{qr_token}/qr`** 를 사용합니다.
> 내부 PK(`id`)를 URL에 노출하지 않기 위한 의도적 설계입니다.

### WebSocket 메시지 형식 (JSON)

```jsonc
// 클라이언트 -> 서버
{"type": "text", "content": "여기 놀이터 앞이에요"}
{"type": "location", "latitude": 37.5665, "longitude": 126.9780}

// 서버 -> 클라이언트
{"type": "history", "messages": [ ... ]}          // 접속 시 이전 대화 복원
{"type": "text", "content": "...", "sender_role": "finder"}
{"type": "location", "latitude": ..., "longitude": ..., "sender_role": "finder"}
{"type": "system", "content": "보호자가 입장했습니다."}
```

모든 메시지는 즉시 DB에 저장되어, 연결이 끊긴 뒤 재접속해도 이전 대화가 복원됩니다.

---

## 4. SMS 목업 → 실제 API 교체 방법

`sms.py` 의 `send_sms(phone: str, message: str) -> bool` **함수 시그니처만 그대로 유지**하고
내부 구현만 실제 API(알리고 / 쿨SMS 등) 호출로 바꾸면 됩니다. 호출부(`main.py`) 수정은 필요 없습니다.

```python
def send_sms(phone: str, message: str) -> bool:
    try:
        resp = requests.post(SMS_API_URL, data={...}, timeout=5)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        # 실패 시 로깅 후 False 반환 (호출부는 예외가 안 난다고 가정)
        print(f"[SMS 발송 실패] {e}")
        return False
```

API 키는 코드에 하드코딩하지 말고 환경변수로 주입하세요.

---

## 5. 프로덕션 전환

- **DB**: 환경변수 `DATABASE_URL` 을 PostgreSQL 접속 문자열로 지정하면 코드 수정 없이 전환됩니다.
  (예: `postgresql+psycopg://user:pw@host:5432/dbname`)
- **BASE_URL**: `main.py` 의 `BASE_URL` 을 실제 서비스 도메인(HTTPS)으로 교체하세요.
  QR과 보호자 SMS 링크에 이 값이 사용됩니다.
- **실행**: `--reload` 를 빼고, 필요 시 여러 워커로 실행하세요. 단, 현재 WebSocket 연결
  상태는 **프로세스 메모리(단일 인스턴스)** 에만 있으므로, 다중 워커/다중 서버로
  수평 확장하려면 Redis Pub/Sub 등 외부 메시지 브로커로 브로드캐스트를 공유해야 합니다.

---

## 6. 보안 · 개인정보 (중요)

- `qr_token`, 보호자 `token` 은 각각 `secrets.token_urlsafe(32)` 로 생성한 32바이트 이상의
  암호학적으로 안전한 랜덤 문자열입니다.
- **아이의 내부 `id`·이름·보호자 전화번호는 URL이나 API 응답, 화면 어디에도 노출되지 않습니다.**
  URL에 실리는 것은 역추적 불가능한 랜덤 토큰뿐입니다.
- 보호자 채팅방은 `guardian_token` 이 일치해야만 입장할 수 있어 URL 위조를 방지합니다
  (HTTP 화면 + WebSocket 양쪽에서 검증).
- 채팅방은 관리자가 종료(`closed`)하면 신규 WebSocket 연결이 거부됩니다.
  (24시간 자동 만료 정책은 스케줄러/배치로 확장 가능)
- 채팅 입력은 프론트에서 이스케이프 처리하여 XSS를 방지합니다.

### 데모 단계의 한계 (실서비스 전 반드시 보완)

- **관리자 API 인증 없음**: `/admin/*` 엔드포인트에 인증/인가가 없습니다.
  실서비스에서는 관리자 로그인·권한 검사를 반드시 추가하세요.
- **개인정보 평문 저장**: `guardian_phone` 등이 평문으로 저장됩니다.
  실서비스에서는 **암호화 저장(예: 애플리케이션 레벨 AES-GCM)** 을 적용하세요.
  (`models.py` 에 관련 TODO 주석이 있습니다)
- **HTTPS 필수**: 위치·대화 내용이 오가므로 운영에서는 반드시 TLS(HTTPS/WSS)를 사용하세요.

---

## 7. 파일 구조

```
relink/
├── PLAN.md                 # 프로젝트 실행 계획
├── main.py                 # FastAPI 앱, 모든 라우트 + WebSocket 핸들러
├── models.py                # SQLAlchemy 모델 (Child / ChatRoom / Message)
├── database.py               # DB 엔진·세션 설정
├── websocket_manager.py       # room별 WebSocket 커넥션 관리
├── sms.py                    # SMS 발송 인터페이스 (목업)
├── qr_utils.py                # QR 코드 생성
├── templates/
│   ├── found_landing.html  # 발견 신고 랜딩 페이지
│   └── chat.html           # 채팅 화면 (발견자/보호자 공용)
├── static/
│   ├── chat.js             # WebSocket 클라이언트 + Geolocation + 자동 재연결
│   └── style.css           # 반응형 스타일
└── requirements.txt
```

## 8. 개발용 DB 초기화

테스트 데이터를 지우고 새로 시작하려면 서버를 끈 뒤 `missing_child_chat.db` 파일을
삭제하면 됩니다. 다음 서버 시작 시 빈 테이블이 자동 생성됩니다.

```powershell
Remove-Item ".\missing_child_chat.db"
```

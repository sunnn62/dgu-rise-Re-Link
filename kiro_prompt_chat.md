# Kiro 개발 프롬프트: 실종아동 QR 발견-신고 채팅 서비스

아래 명세대로 실시간 채팅 서비스를 구현해줘. 실종 아동의 옷에 부착된 QR코드를 발견자가 스캔하면, 앱 설치 없이 웹페이지에서 바로 보호자와 실시간으로 소통할 수 있는 서비스야.

---

## 1. 핵심 시나리오

1. 아이 옷에 고유 ID가 담긴 QR코드가 부착되어 있음 (예: `https://서비스도메인/found/{child_id}`)
2. 발견자가 QR을 스캔 → 앱 설치 없이 브라우저로 웹페이지 접속
3. 웹페이지에서 발견자가 "발견했어요" 버튼을 누르면:
   - 보호자에게 즉시 알림(SMS 또는 웹 알림) 발송
   - 발견자-보호자 간 익명 채팅방 자동 생성 및 실시간 연결
4. 발견자는 위치 공유 버튼으로 현재 위치를 보호자에게 전송 가능
5. 채팅에는 서로의 전화번호나 실명이 노출되지 않음 (서버가 중계)

## 2. 기술 스택

- **백엔드**: Python FastAPI
- **실시간 통신**: FastAPI 내장 WebSocket (Socket.IO 등 외부 라이브러리 없이 순수 WebSocket으로 구현)
- **DB**: SQLite (개발용, `child_id`, 채팅방, 메시지, 보호자 연락처 저장). 프로덕션에서 PostgreSQL로 바꿀 수 있게 SQLAlchemy ORM 사용
- **프론트**: 별도 프레임워크 없이 순수 HTML/CSS/JS (Jinja2 템플릿으로 FastAPI가 직접 서빙). 모바일 브라우저에서 바로 열리는 게 목적이므로 반응형 필수
- **알림**: SMS는 목업 함수로 우선 구현 (`send_sms(phone, message)` 함수가 실제 발송 대신 콘솔에 로그만 남기도록. 나중에 알리고/쿨SMS API로 교체할 수 있게 인터페이스 분리)
- **위치**: 브라우저 Geolocation API로 발견자 위치(위도/경도) 획득 후 WebSocket으로 전송

## 3. 데이터 모델

```python
Child:
  id (PK, UUID)
  name
  guardian_phone
  guardian_name
  qr_token (고유 랜덤 토큰, URL에 노출되는 값. id와 별도로 두어 아이 실명·정보 추측 불가하게)
  created_at

ChatRoom:
  id (PK)
  child_id (FK)
  status (waiting | active | closed)
  created_at
  closed_at

Message:
  id (PK)
  room_id (FK)
  sender_role (finder | guardian)
  content
  message_type (text | location)
  latitude, longitude (message_type이 location일 때만)
  created_at
```

QR에는 `child_id`가 아니라 반드시 `qr_token`만 노출할 것. `qr_token`으로 아이 이름이나 보호자 정보를 역추적할 수 없어야 함.

## 4. API / 라우트 설계

**발견자 플로우 (인증 불필요, 누구나 접근)**
- `GET /found/{qr_token}` — 발견 신고 랜딩 페이지 (HTML). "아이를 발견하셨나요?" 버튼만 있는 초기 화면
- `POST /found/{qr_token}/start` — 발견 신고 시작. ChatRoom 생성(status=waiting), 보호자에게 SMS 발송(목업), 채팅방 URL을 발견자에게 반환
- `GET /chat/{room_id}?role=finder` — 발견자용 채팅 화면 (HTML)
- `WS /ws/chat/{room_id}?role=finder` — 발견자 WebSocket 연결

**보호자 플로우 (SMS 링크로 접속, 최소한의 확인만)**
- `GET /chat/{room_id}?role=guardian&token=...` — 보호자용 채팅 화면. `token`은 SMS에 담긴 1회성 인증 토큰(room_id와 별개로 발급, 위조 방지)
- `WS /ws/chat/{room_id}?role=guardian` — 보호자 WebSocket 연결

**관리자용 (최소 기능만)**
- `POST /admin/children` — 아이 등록 및 QR 토큰 발급 (등록 시 QR 이미지도 생성해서 반환, `qrcode` 파이썬 라이브러리 사용)
- `GET /admin/children/{id}/qr` — QR 이미지 다운로드

## 5. WebSocket 로직

- 같은 `room_id`로 접속한 두 커넥션(finder, guardian)을 서버 메모리의 room manager(dict)에서 매칭
- 메시지 형식 (JSON):
```json
{"type": "text", "content": "여기 놀이터 앞이에요", "sender_role": "finder"}
{"type": "location", "latitude": 37.5, "longitude": 127.0, "sender_role": "finder"}
{"type": "system", "content": "보호자가 입장했습니다"}
```
- 한쪽이 입장/퇴장하면 상대방에게 system 메시지로 알림
- 모든 메시지는 즉시 DB에도 저장 (연결이 끊겨도 이력 보존, 나중에 부모가 채팅방 재접속 시 이전 메시지 로드)
- 연결 끊김 시 자동 재연결 로직을 프론트 JS에 포함 (재연결 시도 3회, 실패하면 "연결이 끊겼습니다. 새로고침 해주세요" 안내)

## 6. 화면 요구사항

**발견 랜딩 페이지 (`/found/{qr_token}`)**
- 큰 버튼 "아이를 발견했어요, 보호자와 연결하기"
- 버튼 누르기 전 안내 문구: "버튼을 누르면 보호자에게 즉시 알림이 가고, 실시간 채팅이 시작됩니다. 아이의 개인정보는 공개되지 않습니다."
- 위급 상황 안내: "위험한 상황이면 채팅 전에 먼저 112에 신고해주세요" 문구를 상단에 고정

**채팅 화면 (공통, 발견자/보호자)**
- 카카오톡 스타일 말풍선 UI (내 메시지 오른쪽, 상대 메시지 왼쪽)
- 하단 입력창 + 전송 버튼
- "내 위치 공유하기" 버튼 (발견자 화면에만 표시) — 누르면 Geolocation 권한 요청 후 지도 링크 형태로 메시지 전송 (예: 카카오맵/구글맵 열리는 링크)
- 상단에 "OO 발견 신고 채팅방 · 연결됨" 상태 표시

## 7. 보안·개인정보 요구사항 (중요)

- `qr_token`, 보호자용 `token`은 각각 최소 32자 이상의 암호학적으로 안전한 랜덤 문자열(`secrets.token_urlsafe`)로 생성
- 채팅 메시지에 전화번호·주소 등 개인정보가 포함되어도 서버가 자동으로 상대방 실명을 붙이지 않음 (발견자/보호자 모두 "발견자", "보호자"로만 표시)
- ChatRoom은 일정 시간(예: 24시간) 또는 관리자가 종료 처리하면 `status=closed`로 전환, closed 상태에서는 WebSocket 연결 거부
- `qr_token`으로 아이 이름을 URL이나 API 응답에 절대 노출하지 말 것 (발견자 화면에는 "아이"라고만 표시하거나, 보호자가 사전에 등록한 별칭만 표시)

## 8. 파일 구조

```
missing-child-chat/
├── main.py                 # FastAPI 앱, 라우트 정의
├── models.py                # SQLAlchemy 모델
├── database.py               # DB 세션/엔진 설정
├── websocket_manager.py       # room별 커넥션 관리 클래스
├── sms.py                    # SMS 발송 인터페이스 (목업 구현)
├── qr_utils.py                # QR 코드 생성
├── templates/
│   ├── found_landing.html
│   ├── chat.html
├── static/
│   ├── chat.js               # WebSocket 클라이언트, Geolocation 처리
│   └── style.css
├── requirements.txt
└── README.md                 # 실행 방법, 목업→실제 SMS API 교체 방법 설명
```

## 9. 구현 순서 (단계별로 진행하고 각 단계마다 동작 확인)

1. FastAPI 뼈대 + DB 모델 + 관리자용 아이 등록/QR 발급 API
2. WebSocket 기본 연결 + room 매칭 로직 (텍스트 채팅만 먼저 동작하게)
3. 발견 랜딩 페이지 + 채팅 화면 프론트(HTML/JS) 연결
4. 위치 공유 기능 추가
5. SMS 목업 연동 + 보호자 인증 토큰 플로우
6. 보안 점검 (토큰 노출 여부, closed 상태 처리) + README 정리

## 10. 주의사항

- 실제 SMS 발송 API 키는 넣지 말고 목업으로 두되, 나중에 실제 API로 바꾸기 쉽도록 `sms.py`의 함수 시그니처만 맞춰둘 것
- 이 서비스는 실제 아동 안전과 연결되는 만큼, 데모/개발 단계여도 "제출한 개인정보(전화번호 등)는 암호화 저장 권장" 주석을 코드에 남겨줄 것
- 로컬 개발 시 WebSocket 테스트를 위해 브라우저 두 개 창(발견자용/보호자용)으로 동시 접속해서 확인하는 방법을 README에 안내할 것

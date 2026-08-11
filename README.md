<div align="center">

# Re:Link

### QR 한 번으로, 발견자와 보호자를 가장 빠르고 안전하게 연결합니다.

앱 설치도 로그인도 없이 시작하는 **실종 아동 발견 대응 서비스**

<p>
  <img src="https://img.shields.io/badge/FastAPI-0.115.6-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI 0.115.6">
  <img src="https://img.shields.io/badge/Python-Backend-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python Backend">
  <img src="https://img.shields.io/badge/WebSocket-Native-111111?style=flat-square" alt="Native WebSocket">
  <img src="https://img.shields.io/badge/Upstage-Solar-6C5CE7?style=flat-square" alt="Upstage Solar">
  <img src="https://img.shields.io/badge/Status-Completed-2E8B57?style=flat-square" alt="Completed">
</p>

[서비스 흐름](#서비스-흐름) · [핵심 기능](#핵심-기능) · [빠른 시작](#빠른-시작) · [API 문서](API_CONTRACT.md) · [프로젝트 계획](PLAN.md)

</div>

---

## 왜 Re:Link인가요?

실종 아동을 발견해도 보호자에게 연락할 방법이 없거나, 신고 과정이 복잡해 중요한 시간을 놓칠 수 있습니다. Re:Link는 미리 등록된 QR 태그를 통해 **발견자는 개인정보 노출 없이 즉시 알리고**, **보호자는 대시보드에서 상황과 대화를 확인**할 수 있게 합니다.

위치는 지속적으로 추적하지 않습니다. 발견자가 직접 공유한 순간의 좌표만 **1회성 위치 스냅샷**으로 전달합니다.

## 서비스 흐름

| 01. 태그 등록 | 02. 발견 및 스캔 | 03. 위치 공유 | 04. 안전한 연결 |
|:---:|:---:|:---:|:---:|
| 보호자가 QR 시리얼을 아이와 연결하고 실종 상태를 활성화합니다. | 발견자가 QR을 스캔합니다. 앱 설치와 로그인은 필요 없습니다. | 발견자가 동의하면 현재 위치를 한 번만 공유합니다. | 보호자는 대시보드에서 신고를 확인하고 익명 채팅에 참여합니다. |

## 핵심 기능

- **로그인 없는 발견 신고** — QR 스캔만으로 개인정보 없는 발견 페이지와 신고 흐름에 진입합니다.
- **보호자 통합 대시보드** — 여러 아이의 상태, QR, 진행 중인 채팅방을 한 화면에서 관리합니다.
- **1회성 위치 스냅샷** — 실시간 추적 대신 발견자가 명시적으로 공유한 시점의 좌표와 오차 반경을 전달합니다. 위치 공유를 원하지 않으면 112 신고 경로를 안내합니다.
- **익명 실시간 채팅** — 별도 실시간 라이브러리 없이 네이티브 WebSocket으로 발견자와 보호자를 연결하며, 이름과 전화번호를 노출하지 않습니다.
- **가족 초대 및 공동 관리** — 1회용 초대 코드로 여러 보호자가 아이의 상태와 채팅을 함께 관리할 수 있습니다.
- **Upstage Solar 모더레이션** — 협박, 갈취, 개인정보 요구 등 위험 메시지를 백그라운드에서 감지하고 이후 전송을 제한합니다.

## 프라이버시 설계

> **필요한 정보만, 필요한 순간에만 전달합니다.**

- QR URL에는 역추적하기 어려운 랜덤 토큰만 포함됩니다.
- 채팅에서는 사용자를 `발견자`와 `보호자` 역할로만 표시합니다.
- 위치는 사용자의 동의 후 한 번 전송되며 지속 추적하지 않습니다.
- 뒤늦게 참여한 발견자는 참여 이전의 대화 기록을 볼 수 없습니다.

## 기술 스택

| 영역 | 기술 |
|---|---|
| Backend | Python, FastAPI, Uvicorn |
| Realtime | Native WebSocket |
| Frontend | Jinja2, Vanilla JavaScript, CSS |
| Database | SQLAlchemy, SQLite / PostgreSQL |
| AI Safety | Upstage Solar API, OpenAI-compatible SDK |
| Location | Browser Geolocation, Kakao Local API |
| QR | qrcode, Pillow |

## 빠른 시작

### 1. 가상환경 및 의존성 설치

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 환경 변수 설정

프로젝트 루트에 `.env` 파일을 만듭니다. 외부 연동이 필요할 때만 해당 키를 추가하세요.

```dotenv
BASE_URL=http://localhost:8000
UPSTAGE_API_KEY=your_api_key
KAKAO_REST_API_KEY=your_api_key
```

비밀값은 저장소에 커밋하지 마세요. 키가 없으면 Solar 모더레이션과 장소 검색은 비활성화됩니다.

### 3. 서버 실행

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

브라우저에서 `http://127.0.0.1:8000`에 접속합니다.

> 현재 SMS 발송은 콘솔 로그 기반 목업입니다. 실제 문자 발송 서비스가 연동된 상태가 아닙니다.

## 더 알아보기

- [API 계약 문서](API_CONTRACT.md) — 라우트, 인증, 요청·응답 규격
- [프로젝트 계획](PLAN.md) — 설계 결정, 구현 배경, 로드맵

---

<div align="center">
  <sub>발견에서 연결까지, Re:Link</sub>
</div>

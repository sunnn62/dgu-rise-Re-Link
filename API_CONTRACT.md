# Re:Link API 계약 문서

`main.py` 기준으로 실제 구현된 라우트를 그대로 문서화한 것입니다. 이 프로젝트는 별도 JSON REST API + SPA 구조가 아니라 **FastAPI + Jinja2 서버 렌더링 + 폼 제출 + WebSocket** 구조이므로, 아래 "응답"은 대부분 HTML 페이지 또는 리다이렉트입니다. 순수 JSON을 반환하는 곳은 관리자 시드 도구뿐입니다.

- **베이스 URL**: 로컬 개발 `http://localhost:8000` (배포 후 실제 도메인으로 교체, `main.py`의 `BASE_URL` 참고)
- **인증 방식**: 세션 쿠키 (`relink_session`, `httponly`, `samesite=lax`, 7일 유지). 로그인 성공 시 서버가 `Set-Cookie`로 발급하고, 이후 모든 보호자 전용 라우트는 이 쿠키만으로 판정합니다. **URL에 인증 토큰을 담지 않습니다.**
- **폼 제출 방식**: 대부분 `POST` + `application/x-www-form-urlencoded` (HTML `<form>` 그대로 제출). 성공/실패 모두 `303 See Other` 리다이렉트로 응답하며, 실패 사유는 리다이렉트 URL의 `?error=코드` 쿼리 파라미터로 전달됩니다. 프론트는 이 코드값으로 화면에 에러 메시지를 분기해서 보여줘야 합니다(아래 각 라우트의 "에러 코드" 참고).

---

## 1. 보호자 회원가입 / 로그인 / 로그아웃

### `GET /register`
회원가입 폼 페이지.

| 쿼리 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `error` | string | 아니오 | 아래 에러 코드 중 하나. 있으면 폼 위에 에러 문구 표시 |

### `POST /register`
회원가입 + 최초 아이 1명 등록(시리얼 claim)을 한 번에 처리합니다.

**요청 (form-urlencoded)**

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `guardian_name` | string | 예 | 보호자 이름 |
| `guardian_phone` | string | 예 | 로그인 ID로 쓰임. 형식: `010-0000-0000` |
| `guardian_pin` | string | 예 | 숫자 6자리 이상 |
| `child_name` | string | 예 | 최초 등록할 아이 이름 |
| `serial` | string | 예 | QR 스티커에 인쇄된 시리얼 번호 (예: `RL-AB2C-9KMP`) |

**응답**
- 성공: `303` → `/guardian/dashboard` (세션 쿠키 `Set-Cookie` 포함, 가입 즉시 로그인 상태)
- 실패: `303` → `/register?error=<코드>`

**에러 코드**

| 코드 | 의미 |
|---|---|
| `missing_fields` | 필수 필드 중 하나 이상 비어 있음 |
| `invalid_pin` | PIN이 숫자 6자리 이상 형식이 아님 |
| `phone_taken` | 이미 가입된 전화번호 (→ 로그인 유도) |
| `invalid_serial` | 존재하지 않는 시리얼 번호 |
| `serial_used` | 이미 다른 아이에게 연결된 시리얼 번호 |

> ⚠️ 알려진 갭: `phone_taken`은 "이 번호가 가입돼 있는지"를 제3자도 알아낼 수 있는 계정 열거(enumeration) 여지가 있음 — PLAN.md에 의도적 트레이드오프로 명시돼 있음.

---

### `GET /login`
로그인 폼 페이지.

| 쿼리 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `error` | string | 아니오 | `invalid_credentials` \| `locked` |
| `minutes` | int | `error=locked`일 때만 | 잠금 해제까지 남은 분(올림) |

### `POST /login`

**요청 (form-urlencoded)**

| 필드 | 타입 | 필수 |
|---|---|---|
| `guardian_phone` | string | 예 |
| `guardian_pin` | string | 예 |

**응답**
- 성공: `303` → `/guardian/dashboard` (세션 쿠키 발급)
- 실패: `303` → `/login?error=<코드>`

**에러 코드**

| 코드 | 의미 |
|---|---|
| `invalid_credentials` | 전화번호 또는 PIN 불일치 (존재하지 않는 번호도 동일한 코드 — 로그인 단계 계정 열거 방지) |
| `locked` | 5회 연속 실패로 15분간 잠김. `minutes` 파라미터와 함께 옴 |

> 아직 미구현: "PIN을 잊으셨나요?" (OTP 기반 PIN 재설정)는 Week 2 스코프. 지금 로그인 화면엔 안내 문구만 있고 실제 라우트는 없음.

### `POST /logout`
세션 폐기 + 쿠키 삭제. 요청 바디 없음. 항상 `303` → `/login`.

---

## 2. 보호자 대시보드 (세션 필요)

아래 라우트는 전부 세션 쿠키가 없거나 유효하지 않으면 `303` → `/login`으로 리다이렉트합니다(에러 코드 없이 조용히 이동).

### `GET /guardian/dashboard`
로그인한 보호자의 아이 목록 + 아이별 진행 중인 채팅방을 보여주는 HTML 페이지.

내부적으로 템플릿에 전달되는 아이 데이터 구조(프론트가 화면을 만들 때 참고):

```jsonc
{
  "guardian_name": "홍길동",
  "children": [
    {
      "id": "uuid",
      "name": "아이 이름",
      "status": "normal | missing",
      "qr_token": "QR 다운로드용 토큰",
      "active_room_id": "uuid | null"  // null이 아니면 '진행 중인 채팅방 입장' 버튼 표시
    }
  ]
}
```

### `POST /guardian/dashboard/children`
아이 추가(시리얼 claim).

**요청 (form-urlencoded)**: `child_name`, `serial` (둘 다 필수)

**응답**
- 성공: `303` → `/guardian/dashboard`
- 실패: `303` → `/guardian/dashboard?error=<코드>`

**에러 코드**: `missing_fields` | `invalid_serial` | `serial_used`

> ⚠️ **알려진 갭**: 이 에러 코드들이 리다이렉트 URL엔 붙지만, `guardian_dashboard.html`은 `error` 쿼리 파라미터를 읽지도, 화면에 표시하지도 않습니다. 즉 **지금은 시리얼이 틀려도 사용자에게 아무 메시지 없이 그냥 대시보드로 돌아갑니다.** PLAN.md Week 2 프론트 체크리스트("시리얼 번호 오류 처리 UI")에 이미 있는 항목이니 그때 채우면 됩니다.

### `POST /guardian/dashboard/children/{child_id}/toggle`
실종 신고 상태 토글(`normal` ↔ `missing`). 요청 바디 없음.

**응답**: 성공 시 `303` → `/guardian/dashboard`

**에러**: `404` (존재하지 않는 아이) / `403` (본인 소유 아이가 아님 — JSON body: `{"detail": "..."}`)

### `POST /guardian/dashboard/children/{child_id}/delete`
아이 삭제(연결된 QrToken도 자동으로 미사용 상태로 되돌림). 요청 바디 없음.

**응답**: 성공 시 `303` → `/guardian/dashboard`

**에러**: `404` / `403` (toggle과 동일)

---

## 3. 발견자 플로우 (인증 불필요, 누구나 접근)

### `GET /found/{qr_token}`
발견 신고 랜딩 페이지. 존재하지 않는 토큰이면 `404`.

### `POST /found/{qr_token}/start`
"발견했어요" 버튼 클릭 시 채팅방을 생성한다.

**응답 (JSON)**

```jsonc
{
  "room_id": "uuid",
  "chat_url": "/chat/{room_id}?role=finder",
  "ws_url": "/ws/chat/{room_id}?role=finder"
}
```

**에러**
- `404`: 존재하지 않는 QR 토큰
- `403`: 아이가 `missing` 상태가 아님 (`"현재 실종 신고 상태가 아닌 아이입니다. 채팅을 시작할 수 없습니다."`) — GAP A 서버 재검증

### `GET /chat/{room_id}?role=finder|guardian`
채팅 화면(HTML). `role=guardian`이면 로그인 세션 필요.

**에러**
- `403`: role이 잘못됐거나, `guardian`인데 이 방의 아이가 본인 소유가 아님(또는 로그인 안 함)
- `404`: 존재하지 않는 방
- `410`: 이미 종료(closed)된 방

### `WS /ws/chat/{room_id}?role=finder|guardian`
실시간 채팅. `guardian`은 세션 쿠키(브라우저가 자동 전송)로 소유권 재검증, `finder`는 인증 없음.

**서버 → 클라이언트 메시지**

```jsonc
{"type": "history", "messages": [ /* 아래 형식의 배열 */ ]}
{"type": "text", "content": "...", "sender_role": "finder | guardian"}
{"type": "location", "latitude": 37.5, "longitude": 127.0, "sender_role": "finder | guardian"}
{"type": "system", "content": "..."}
```

**클라이언트 → 서버 메시지**

```jsonc
{"type": "text", "content": "여기 놀이터 앞이에요"}
{"type": "location", "latitude": 37.5665, "longitude": 126.9780}
```

**GAP B 동작 (중요)**: `role=finder`가 `location`을 한 번도 안 보낸 상태에서 `text`를 보내면, 서버는 저장하지 않고 아래 시스템 메시지만 돌려줍니다.

```jsonc
{"type": "system", "content": "먼저 위치를 공유해야 채팅을 시작할 수 있습니다."}
```

위치를 보내면 그 이후부터는 텍스트가 정상적으로 열리고, 서버가 아래 안내를 한 번 보냅니다.

```jsonc
{"type": "system", "content": "위치가 공유되어 이제 채팅을 보낼 수 있습니다."}
```

> ⚠️ **알려진 갭 (PLAN.md P0 미완료 항목)**: "위치 공유 없이 112 신고하기" 대체 경로가 프론트/백엔드 어디에도 없습니다. 지금은 위치 공유를 거부한 발견자는 그냥 채팅이 막힌 채로 남습니다 — `tel:112` 버튼도, 보호자에게 보내는 "발견자가 112 경로로 안내받음" 시스템 메시지도 아직 구현 전입니다.

**연결 거부 상황(핸드셰이크 단계에서 `close`, 재연결 의미 없음)**
- 잘못된 `role`
- 존재하지 않거나 `closed`된 방
- `role=guardian`인데 세션이 없거나 다른 보호자의 아이 방
- 아이가 `missing` 상태가 아님(보호자가 그 사이 신고를 해제한 경우 포함)

---

## 4. 개발용 관리자 시드 도구 (프로덕션 미노출, 인증 없음)

⚠️ 이 섹션은 프론트가 실사용 흐름에서 호출할 일이 없는 **개발/테스트 전용** 라우트입니다. 인증이 전혀 없으므로(PLAN P2 미해결 항목) 배포 시 반드시 제거하거나 보호해야 합니다.

| 메서드 | 경로 | 용도 |
|---|---|---|
| `POST` | `/admin/children` | 테스트용 아이+보호자 즉석 생성 (JSON 응답: `qr_token`, `found_url`, `qr_download_url`) |
| `POST` | `/admin/children/{qr_token}/status` | 아이 상태 강제 전환 (`{"status": "normal\|missing"}`) |
| `GET` | `/admin/children/{qr_token}/qr` | QR PNG 다운로드 (실사용에서도 `guardian_dashboard.html`이 이 경로를 그대로 씀) |
| `POST` | `/admin/rooms/{room_id}/close` | 채팅방 강제 종료 |

---

## 5. 아직 이 문서에 없는 것 (구현 전)

- PIN 재설정(OTP) 관련 라우트 — Week 2 예정
- 대시보드 시리얼 오류 표시 — 라우트는 있으나 템플릿 미반영 (위 2번 섹션 참고)
- 112 신고 대체 경로 — 프론트/백엔드 모두 미구현 (위 3번 섹션 참고)

이 문서는 코드가 바뀔 때마다 같이 갱신해야 실제 계약과 어긋나지 않습니다.

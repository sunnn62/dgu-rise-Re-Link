# Re:Link API 계약 문서

`main.py` 기준으로 실제 구현된 라우트를 그대로 문서화한 것입니다. 이 프로젝트는 별도 JSON REST API + SPA 구조가 아니라 **FastAPI + Jinja2 서버 렌더링 + 폼 제출 + WebSocket** 구조이므로, 아래 "응답"은 대부분 HTML 페이지 또는 리다이렉트입니다. 순수 JSON을 반환하는 곳은 발견 신고 시작, 장소 검색, 관리자 시드 도구뿐입니다.

- **베이스 URL**: 로컬 개발 `http://localhost:8000`. 배포 도메인은 `main.py`의 `BASE_URL` 환경변수로 결정됨(기동 시 HTTPS 여부를 검증하며, 로컬(`localhost`/`127.0.0.1`)만 예외).
- **인증 방식**: 세션 쿠키(`relink_session`, `httponly`, `samesite=lax`, 7일 유지). 로그인 성공 시 서버가 `Set-Cookie`로 발급하고, 이후 모든 보호자 전용 라우트는 이 쿠키만으로 판정합니다. **URL에 인증 토큰을 담지 않습니다.**
- **발견자 익명 쿠키**: `relink_finder`(httponly). 발견자가 채팅 화면(`GET /chat/{room_id}`)에 처음 접속할 때 발급되며, "이 발견자가 이 방에 언제 처음 들어왔는지"·"이 발견자가 이 방에서 위치를 공유했는지"를 방별로 기록하는 데 쓰입니다(다중 발견자 이력 격리, 아래 6번 섹션 참고).
- **폼 제출 방식**: 대부분 `POST` + `application/x-www-form-urlencoded`(HTML `<form>` 그대로 제출). 성공/실패 모두 `303 See Other` 리다이렉트로 응답하며, 실패 사유는 리다이렉트 URL의 `?error=코드` 쿼리 파라미터로 전달됩니다. 프론트는 이 코드값으로 화면에 에러 메시지를 분기해서 보여줘야 합니다(아래 각 라우트의 "에러 코드" 참고).

---

## 1. 보호자 회원가입 / 로그인 / 로그아웃 / PIN 재설정

### `GET /register`
회원가입 폼 페이지.

| 쿼리 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `error` | string | 아니오 | 아래 에러 코드 중 하나 |

### `POST /register`
보호자 **계정만** 생성합니다. **QR(태그) 등록은 더 이상 여기서 하지 않습니다** — 가입 후 로그인 상태로 대시보드에 진입시키고, 태그 등록은 대시보드의 "태그 추가" 폼(`POST /guardian/dashboard/children`)에서 별도로 진행합니다.

**요청 (form-urlencoded)**

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `guardian_name` | string | 예 | 보호자 이름 |
| `guardian_phone` | string | 예 | 로그인 ID로 쓰임. 형식: `010-0000-0000` |
| `guardian_pin` | string | 예 | 숫자 6자리 이상 |
| `privacy_consent` | string | 예 | 체크박스 값(`"true"`). **없거나 비어 있으면 서버가 가입을 거부합니다**(프론트 체크박스 상태와 무관하게 서버에서 재검증) |

**응답**
- 성공: `303` → `/guardian/dashboard` (세션 쿠키 발급, 가입 즉시 로그인 상태)
- 실패: `303` → `/register?error=<코드>`

**에러 코드**

| 코드 | 의미 |
|---|---|
| `missing_fields` | `guardian_name`/`guardian_phone` 중 하나 이상 비어 있음 |
| `invalid_pin` | PIN이 숫자 6자리 이상 형식이 아님 |
| `phone_taken` | 이미 가입된 전화번호(→ 로그인 유도) |
| `consent_required` | 개인정보 수집·이용 동의 체크박스가 체크되지 않음 |

> ⚠️ 알려진 갭(의도된 트레이드오프): `phone_taken`은 "이 번호가 가입돼 있는지"를 제3자도 알아낼 수 있는 계정 열거(enumeration) 여지가 있음 — PLAN.md에 명시된 트레이드오프.
>
> `invalid_serial`/`serial_used`는 **더 이상 이 라우트에서 나오지 않습니다**(태그 등록이 분리됐으므로). 그 두 코드는 이제 `POST /guardian/dashboard/children`에서만 발생합니다.

---

### `GET /login`
로그인 폼 페이지.

| 쿼리 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `error` | string | 아니오 | `invalid_credentials` \| `locked` |
| `minutes` | int | `error=locked`일 때만 | 잠금 해제까지 남은 분(올림) |
| `reset` | string | 아니오 | `success`면 PIN 재설정 완료 후 리다이렉트된 것 — 성공 안내 표시 |

### `POST /login`

**요청 (form-urlencoded)**: `guardian_phone`, `guardian_pin` (둘 다 필수)

**응답**
- 성공: `303` → `/guardian/dashboard` (세션 쿠키 발급)
- 실패: `303` → `/login?error=<코드>`

**에러 코드**

| 코드 | 의미 |
|---|---|
| `invalid_credentials` | 전화번호 또는 PIN 불일치(존재하지 않는 번호도 동일한 코드 — 계정 열거 방지) |
| `locked` | 5회 연속 실패로 15분간 잠김. `minutes` 파라미터와 함께 옴 |

### `POST /logout`
세션 폐기 + 쿠키 삭제. 요청 바디 없음. 항상 `303` → `/login`.

---

### PIN 재설정(OTP 기반) — 구현 완료

### `GET /login/forgot-pin`
전화번호를 입력해 OTP 발송을 요청하는 화면.

| 쿼리 파라미터 | 설명 |
|---|---|
| `sent` | `true`면 "OTP를 발송했습니다" 안내 표시 |
| `error` | `missing_phone` \| `cooldown` \| `too_many` |
| `wait` | `error=cooldown`일 때, 재요청까지 남은 초 |

### `POST /login/forgot-pin`
**요청**: `guardian_phone`

등록 안 된 번호로 요청해도 **발송 성공과 동일한 화면**을 보여줍니다(계정 열거 방지). 실제 SMS는 콘솔 로그 목업입니다(`sms.py`).

- **rate limit**: 같은 번호로 60초 내 재요청 불가, 1시간 내 최대 5회
- 성공: `303` → `/login/forgot-pin?sent=true`
- 실패: `303` → `/login/forgot-pin?error=<코드>`(`missing_phone` | `cooldown&wait=N` | `too_many`)

### `GET /login/reset-pin?phone=...`
OTP 코드 + 새 PIN을 입력하는 화면. `phone` 쿼리로 이전 화면에서 입력한 전화번호를 이어받습니다.

| 쿼리 파라미터 | 설명 |
|---|---|
| `phone` | 이전 화면에서 넘어온 전화번호(폼 hidden 필드로 다시 제출됨) |
| `error` | `missing_fields` \| `invalid_pin` \| `invalid_otp` |

### `POST /login/reset-pin`
**요청**: `guardian_phone`, `otp_code`(6자리), `new_pin`(6자리 이상 숫자)

- OTP는 **검증 시도 자체로 소모**됩니다(성공/실패 불문 1회용) — 틀리면 `/login/forgot-pin`에서 새로 받아야 합니다.
- OTP는 5분 후 만료.
- 성공: `303` → `/login?reset=success`, PIN 변경과 함께 로그인 잠금(5회 실패 카운트)도 초기화됩니다.
- 실패: `303` → `/login/reset-pin?phone=<번호>&error=<코드>`

---

## 2. 보호자 대시보드 (세션 필요)

아래 라우트는 전부 세션 쿠키가 없거나 유효하지 않으면 `303` → `/login`으로 리다이렉트합니다(에러 코드 없이 조용히 이동).

### `GET /guardian/dashboard`
로그인한 보호자에게 **연결된**(최초 등록 + 초대로 합류) 아이 목록 + 아이별 진행 중인 채팅방을 보여주는 HTML 페이지.

| 쿼리 파라미터 | 설명 |
|---|---|
| `error` | `missing_fields` \| `invalid_serial` \| `serial_used`(태그 추가 실패 시) |
| `invite_code` | 방금 발급된 가족 초대 코드(1회만 표시, 새로고침하면 사라짐) |
| `join_error` | 초대 코드 입력 실패 사유(아래 3번 섹션 참고) |
| `joined` | `success`면 초대 코드로 합류 성공 |

템플릿에 전달되는 아이 데이터 구조:

```jsonc
{
  "guardian_name": "홍길동",
  "children": [
    {
      "id": "uuid",
      "name": "아이 이름",
      "status": "normal | missing",
      "qr_token": "QR 다운로드용 토큰",
      "serial": "RL-AB2C-9KMP",
      "active_room_id": "uuid | null",   // null이 아니면 '진행 중인 채팅방 입장' 버튼 표시
      "is_primary": true                  // 이 보호자가 최초 등록자인지(가족 초대 UI 분기용)
    }
  ],
  "invite_code": "AB3D9KMP | null",
  "join_error": "코드 | null",
  "joined": "success | null"
}
```

### `POST /guardian/dashboard/children`
아이 추가(시리얼 claim). 이 보호자가 그 아이의 **최초 등록자(role="primary")**가 됩니다.

**요청**: `child_name`, `serial` (둘 다 필수)

**응답**: 성공 `303` → `/guardian/dashboard` / 실패 `303` → `/guardian/dashboard?error=<코드>`(`missing_fields` | `invalid_serial` | `serial_used`) — 대시보드 템플릿이 이 에러를 화면에 표시합니다.

### `POST /guardian/dashboard/children/{child_id}/toggle`
실종 신고 상태 토글(`normal` ↔ `missing`). 요청 바디 없음.

**권한**: 이 아이에 **연결된(linked) 보호자라면 누구나** 가능(최초 등록자든 초대로 합류한 사람이든 동일) — 가족 구성원 누구든 위급 상황에 신고를 켤 수 있어야 하므로.

**응답**: 성공 `303` → `/guardian/dashboard` / **에러**: `404`(존재하지 않는 아이) · `403`(연결되지 않은 보호자)

### `POST /guardian/dashboard/children/{child_id}/delete`
아이 삭제(연결된 QrToken도 자동으로 미사용 상태로 되돌림). 요청 바디 없음.

**권한**: **최초 등록자(`is_primary=true`)만** 가능 — 초대로 합류한 보호자는 403. 삭제는 민감한 조작이라 실수/다툼으로 데이터가 통째로 사라지는 사고를 막기 위한 의도적 제약.

**응답**: 성공 `303` → `/guardian/dashboard` / **에러**: `404` · `403`

---

## 3. 가족 초대 (아이 한 명 ↔ 보호자 여러 명)

아이 한 명에 보호자를 여러 명 연결할 수 있습니다(예: 부모 두 명이 같은 아이를 함께 관리). QrToken의 "시리얼 매칭" 패턴을 그대로 재사용한 8자리 1회용 코드 방식입니다.

### `POST /guardian/dashboard/children/{child_id}/invite`
초대 코드 발급. **최초 등록자만** 가능(403 — 초대로 합류한 사람은 또 다른 사람을 초대 못 함). 요청 바디 없음.

**응답**: 성공 `303` → `/guardian/dashboard?invite_code=XXXXXXXX`(8자리, 대문자+숫자) / **에러**: `404`(존재하지 않는 아이) · `403`(최초 등록자가 아님)

- 코드는 **24시간 후 만료**, **1회 사용하면 즉시 소멸**합니다.

### `POST /guardian/dashboard/join`
로그인한 보호자가 초대 코드를 입력해 그 아이에 연결(합류)합니다.

**요청**: `code`(8자리, 대소문자 무관 — 서버가 대문자로 정규화)

**응답**: 성공 `303` → `/guardian/dashboard?joined=success` / 실패 `303` → `/guardian/dashboard?join_error=<코드>`

**에러 코드**

| 코드 | 의미 |
|---|---|
| `missing_code` | 코드를 입력하지 않고 제출 |
| `invalid_code` | 존재하지 않는 코드 |
| `expired_code` | 만료됐거나 이미 사용된 코드(동시에 두 요청이 같은 코드를 쓰면, 늦게 도착한 쪽이 이 코드를 받음) |
| `already_linked` | 이미 이 아이에 연결된 보호자(자기 자신이 발급한 코드를 입력한 경우 등) — 이 경우 코드는 소모되지 않음 |

**권한 정리**: 초대로 합류한 보호자(`role="invited"`)도 채팅 열람·실종 신고 토글·QR 다운로드는 최초 등록자와 **동일하게** 할 수 있습니다. **아이 삭제와 초대 코드 발급만** 최초 등록자로 제한됩니다.

---

## 4. 발견자 플로우 (인증 불필요, 누구나 접근)

### `GET /found/{qr_token}`
발견 신고 랜딩 페이지. 존재하지 않는 토큰이면 `404`.

### `POST /found/{qr_token}/start`
"발견했어요" 버튼 클릭 시 채팅방을 생성합니다. **같은 아이에 대해 이미 진행 중인(closed가 아닌) 방이 있으면 새로 만들지 않고 그 방을 재사용**합니다(같은 QR을 여러 번 스캔/새로고침해도 방이 중복 생성되지 않음). 방을 재사용하는 경우 보호자 SMS도 다시 보내지 않습니다.

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
- `403`: 아이가 `missing` 상태가 아님(GAP A 서버 재검증)

### `GET /chat/{room_id}?role=finder|guardian`
채팅 화면(HTML). `role=guardian`이면 로그인 세션 필요하고, **이 방의 아이에 연결된(ChildGuardian) 보호자인지** 재검증합니다(최초 등록자든 초대로 합류한 사람이든 통과). `role=finder`로 처음 접속하면 `relink_finder` 익명 쿠키가 발급됩니다.

**에러**
- `403`: role이 잘못됐거나, `guardian`인데 로그인 안 했거나 이 방의 아이에 연결되지 않음
- `404`: 존재하지 않는 방
- `410`: 이미 종료(closed)된 방

---

## 5. 실시간 채팅 WebSocket

### `WS /ws/chat/{room_id}?role=finder|guardian`

`guardian`은 세션 쿠키로 "이 방의 아이에 연결된 보호자인가"를 재검증, `finder`는 인증 없이(단, 익명 쿠키로 방별 상태를 추적) 접속.

**서버 → 클라이언트 메시지**

```jsonc
{"type": "history", "messages": [ /* 아래 형식의 배열 */ ]}
{"type": "gate", "location_shared": true}   // finder 접속 직후에만 옴(아래 참고)
{"type": "text", "content": "...", "sender_role": "finder | guardian"}
{"type": "location", "content": "장소명 또는 빈 문자열", "latitude": 37.5, "longitude": 127.0, "accuracy": 15.2, "sender_role": "finder | guardian"}
{"type": "system", "content": "..."}
```

- **`gate` 메시지**: `role=finder`로 접속하면 history 직후 딱 한 번 옵니다. `location_shared`는 **이 발견자 개인**이 이 방에서 이미 위치를 공유했는지 여부입니다(다른 발견자가 공유했어도 이 값에 영향 없음 — 발견자 단위 판정). 프론트는 이 값으로 위치 게이트를 보여줄지/채팅 입력창을 바로 열지 결정해야 합니다(새로고침해도 게이트가 불필요하게 다시 뜨지 않도록).
- **`location` 메시지의 `content`**: 원래 위치 메시지는 `content`를 안 씁니다(좌표로 표현). **장소 검색으로 지정한 위치일 때만** 그 장소 이름이 `content`에 담겨 옵니다 — GPS로 공유했으면 빈 문자열입니다. 프론트는 이 값으로 "GPS 위치" vs "검색으로 지정한 위치"를 구분해서 표시해야 합니다.
- **`location` 메시지의 `accuracy`**: GPS 공유는 오차 반경(미터)이 옵니다. 장소 검색으로 지정한 위치는 오차 개념이 없어 `null`입니다.
- **다중 발견자 이력 격리**: `role=finder`가 받는 `history`는 **이 발견자가 이 방에 처음 들어온 시각 이후**의 메시지만 포함합니다(같은 방을 재사용하는 구조에서, 나중에 합류한 발견자가 이전 발견자-보호자 대화를 소급해서 못 읽게 함). `role=guardian`은 항상 전체 이력을 받습니다.

**클라이언트 → 서버 메시지**

```jsonc
{"type": "text", "content": "여기 놀이터 앞이에요"}
{"type": "location", "latitude": 37.5665, "longitude": 126.9780, "accuracy": 15.2}
{"type": "location", "latitude": 37.554, "longitude": 126.970, "place_name": "서울역"}  // 장소 검색으로 지정 시(accuracy 생략)
{"type": "decline_location"}   // finder 전용: 위치 공유 없이 112 신고 경로로 전환
```

**GAP B 동작**: `role=finder`가 **본인이** `location`을 한 번도 안 보낸 상태에서 `text`를 보내면 저장되지 않고 아래 시스템 메시지만 돌아옵니다.

```jsonc
{"type": "system", "content": "먼저 위치를 공유해야 채팅을 시작할 수 있습니다."}
```

위치를 보내면 그 이후부터 텍스트가 열리고, 서버가 아래 안내를 한 번 보냅니다.

```jsonc
{"type": "system", "content": "위치가 공유되어 이제 채팅을 보낼 수 있습니다."}
```

**`decline_location`**: finder가 위치 공유 없이 112 신고 경로로 넘어갔음을 서버에 알립니다. 채팅 입력창은 열리지 않으며(`location_shared`는 그대로 `false`), 보호자를 포함해 방 전체에 시스템 메시지가 브로드캐스트됩니다.

```jsonc
{"type": "system", "content": "발견자가 위치 공유 없이 112 신고 경로로 안내받았습니다.", "sender_role": "system"}
```

**LLM 모더레이션(악용 탐지)**: `text` 메시지는 즉시 저장·브로드캐스트된 뒤, 같은 방의 최근 대화(최대 5개)까지 함께 Upstage Solar API로 백그라운드 검사합니다. 위반이 감지되면 **그 발신자 역할(finder 또는 guardian)의 다음 메시지부터** 아래처럼 차단됩니다(이미 보낸 메시지는 회수되지 않음).

```jsonc
{"type": "system", "content": "OO의 메시지에서 안전 문제가 감지되어 이후 전송이 제한되었습니다."}
```

**연결 거부 상황(핸드셰이크 단계에서 `close`, 재연결 의미 없음)**
- 잘못된 `role`
- 존재하지 않거나 `closed`된 방
- `role=guardian`인데 세션이 없거나 이 방의 아이에 연결되지 않은 보호자
- 아이가 `missing` 상태가 아님(보호자가 그 사이 신고를 해제한 경우 포함)

---

## 6. 장소 검색 (GPS 오차 보정)

### `GET /api/place-search?query=검색어`
카카오 로컬 API(키워드 장소검색)를 서버가 대신 호출하는 프록시. REST API 키는 서버 환경변수(`KAKAO_REST_API_KEY`)에만 있고 클라이언트에 노출되지 않습니다.

**응답 (JSON)**

```jsonc
{
  "results": [
    {"name": "서울역", "address": "서울 중구 한강대로 405", "latitude": 37.554, "longitude": 126.970}
  ]
}
```

**에러**
- `503`: `KAKAO_REST_API_KEY` 미설정(기능 자체가 비활성 상태)
- `502`: 카카오 API 호출 실패/타임아웃

검색 결과 중 하나를 고르면, 프론트는 위 5번 섹션의 `{"type": "location", ..., "place_name": "..."}`로 WebSocket을 통해 전송해야 합니다.

---

## 7. 관리자 라우트 (X-Admin-Key 인증 필요)

⚠️ Week 1 문서에는 "인증 없음"이라고 돼 있었지만 **지금은 인증이 있습니다**. `.env`의 `ADMIN_API_KEY`가 설정돼 있어야 하며, 없으면 아래 라우트가 전부 401로 잠깁니다(fail-closed).

| 메서드 | 경로 | 용도 | 인증 |
|---|---|---|---|
| `POST` | `/admin/children` | 테스트용 아이+보호자 즉석 생성(JSON 응답: `qr_token`, `found_url`, `qr_download_url`) | `X-Admin-Key` 헤더 필수 |
| `POST` | `/admin/children/{qr_token}/status` | 아이 상태 강제 전환(`{"status": "normal\|missing"}`) | `X-Admin-Key` 헤더 필수 |
| `GET` | `/admin/children/{qr_token}/qr` | QR PNG 다운로드. **실사용에서도 대시보드가 이 경로를 그대로 씀** | `X-Admin-Key` **또는** 이 아이에 연결된 로그인 보호자(세션 쿠키) — 둘 중 하나만 통과하면 됨 |
| `POST` | `/admin/rooms/{room_id}/close` | 채팅방 강제 종료 | `X-Admin-Key` 헤더 필수 |
| `GET` | `/admin/qr-batch` | QR 배치 생성 폼(HTML, 관리자 키 입력창) | 없음(폼 자체는 공개, 제출 시 검증) |
| `POST` | `/admin/qr-batch` | QR+시리얼을 대량 생성해 ZIP(PNG들 + `serials.csv`)으로 다운로드. 배포 환경(Render 등)에서 셸 접근 없이 QR 배치를 만들기 위한 용도 | `admin_key` **form 필드**(헤더 아님 — 일반 `<form>` POST라 커스텀 헤더를 못 보내므로 이 라우트만 예외) |

`POST /admin/qr-batch` 요청 필드: `admin_key`(문자열), `count`(1~500).

---

## 8. 아직 이 문서에 없거나 의도적으로 미구현인 것

- 본인인증(PASS 등) 기반 가입 강화 — 논의는 됐으나 3주 범위에서는 보류, 로드맵 항목으로만 남음
- 실제 SMS API 연동 — 콘솔 로그 목업 유지 중(의도된 보류, PLAN.md P2)
- 개인정보(전화번호) 암호화 저장 — 평문 저장 유지 중(의도된 보류, PLAN.md P2)
- 채팅방 내 "다른 보호자도 접속 중" 표시 — 백엔드는 여러 보호자 동시 접속을 허용하지만 프론트 UI는 아직 없음(3주차_프론트 추가구현 요청.md 참고)

이 문서는 코드가 바뀔 때마다 같이 갱신해야 실제 계약과 어긋나지 않습니다.

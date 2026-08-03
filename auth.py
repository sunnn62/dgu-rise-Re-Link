"""[Week1] 보호자 인증(전화번호+PIN 로그인) 및 세션 관리.

PLAN.md "보호자 인증 방식 (로그인+PIN)"에 따라, 예전의 "관리 링크 소유 = 인증"
방식을 전화번호(ID) + PIN(6자리 이상 숫자, 비밀번호 역할) 로그인으로 대체한다.

이 모듈이 담당하는 것:
1. PIN 해싱/검증 — bcrypt/argon2 같은 전용 라이브러리는 이 프로젝트에 설치되어
   있지 않아(불필요한 무거운 의존성 추가를 피하기 위해) 표준 라이브러리인
   hashlib.pbkdf2_hmac으로 구현한다. PIN 자체가 짧은 만큼, 반복 횟수(iterations)를
   충분히 높여 무차별 대입 비용을 올린다.
2. 로그인 세션 — 데모 범위이므로 DB 테이블 대신 프로세스 메모리의 dict로 관리한다.
   (서버 재시작 시 모든 로그인 세션이 초기화된다는 뜻. 실서비스 전환 시에는
   Redis 등 외부 세션 스토어로 교체해야 한다.)
3. 로그인 시도 제한 — 5회 연속 실패 시 15분간 해당 전화번호 로그인을 잠근다.
   (PLAN: "PIN이 짧은 만큼 이게 없으면 사실상 인증이 없는 것과 같다")

주의: 이 파일의 함수들은 순수 로직만 담당하고, DB 조회/커밋은 호출부(main.py)의
책임이다. 계층을 분리해 두어야 main.py에서 SQLAlchemyError를 일관되게 처리할 수 있다.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# [Week1] PIN 정책 및 로그인 잠금 상수
# ---------------------------------------------------------------------------

# PLAN: "6자리 이상 + rate limit + 해싱 세 가지가 세트로 갖춰져야 최소 조건".
PIN_MIN_LENGTH = 6

# 5회 연속 실패 시 15분 잠금.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 15

# 로그인 세션 쿠키 이름(main.py에서 Set-Cookie/쿠키 읽기에 사용).
SESSION_COOKIE_NAME = "relink_session"

# PBKDF2 반복 횟수. PIN이 짧아 엔트로피가 낮으므로, 무차별 대입 비용을 올리기
# 위해 충분히 큰 값을 쓴다(OWASP 권장 SHA256 최소 60만 회 근방을 참고).
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_ALGO = "sha256"
_SALT_BYTES = 16


# ---------------------------------------------------------------------------
# [Week3] 전화번호 정규화
# ---------------------------------------------------------------------------
#
# [버그 수정] Guardian.phone은 지금까지 사용자가 입력한 문자열을 그대로
# 저장/조회했다. 가입 화면은 하이픈 포함 placeholder("010-1234-5678")를
# 보여주지만 강제하지는 않아서, 가입 때 하이픈 없이 입력하고 로그인/PIN
# 재설정 때 하이픈을 넣어 입력하면(또는 그 반대) 문자열이 정확히 일치하지
# 않아 "가입한 적 없는 번호"처럼 취급됐다 — 사용자 입장에서는 원인을 알 수
# 없는 로그인 실패였다. 등록/로그인/PIN 재설정 등 Guardian.phone을 저장하거나
# 조회하는 모든 지점에서 이 함수로 먼저 정규화해, 입력 형식과 무관하게 항상
# 같은 문자열로 비교되게 한다.


def normalize_phone(phone: str) -> str:
    """전화번호에서 숫자만 남긴다(하이픈·공백·괄호 등 구분자 제거).

    순수 문자열 가공이라 예외를 던지지 않는다. 빈 문자열이 들어오면 빈
    문자열을 반환한다(필수 입력 검증은 호출부 책임).
    """
    return re.sub(r"\D", "", phone)


# ---------------------------------------------------------------------------
# [Week1] PIN 해싱/검증
# ---------------------------------------------------------------------------


def validate_pin_format(pin: str) -> bool:
    """PIN이 정책(6자리 이상 숫자)을 만족하는지 검사한다.

    외부 입력(사용자가 입력한 PIN 문자열)에 대한 순수 검증이라 예외를 던지지
    않고 bool로만 결과를 알린다 — 호출부에서 분기 처리가 쉽도록.
    """
    return pin.isdigit() and len(pin) >= PIN_MIN_LENGTH


def hash_pin(pin: str) -> str:
    """PIN을 solt와 함께 PBKDF2로 해싱해 저장용 문자열로 만든다.

    저장 형식: "pbkdf2_sha256$반복횟수$salt(hex)$hash(hex)"
    이렇게 반복 횟수와 salt를 함께 저장해두면, 나중에 반복 횟수를 올려도
    기존 해시와 호환되는 검증 로직을 유지할 수 있다.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, pin.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_pin(pin: str, stored_hash: str) -> bool:
    """입력된 PIN이 저장된 해시와 일치하는지 검증한다.

    Returns:
        일치하면 True. stored_hash 형식이 손상되었거나 불일치하면 False.
        (DB에 저장된 값이 깨져 있어도 로그인 실패로만 처리하고, 서버 오류로
        번지지 않도록 방어적으로 처리한다.)
    """
    try:
        algo_part, iterations_str, salt_hex, hash_hex = stored_hash.split("$")
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        # stored_hash가 예상 형식이 아니거나(수동 DB 조작, 손상 등) None인 경우.
        # 인증 실패로 처리하되 서버 오류로 확대되지 않게 한다.
        return False

    algo = algo_part.removeprefix("pbkdf2_")
    try:
        derived = hashlib.pbkdf2_hmac(algo, pin.encode("utf-8"), salt, iterations)
    except ValueError:
        # 알 수 없는 해시 알고리즘 이름 등. 역시 인증 실패로 처리.
        return False

    # 타이밍 공격을 막기 위해 상수 시간 비교를 사용한다(단순 == 대신).
    return hmac.compare_digest(derived, expected)


# ---------------------------------------------------------------------------
# [Week1] 로그인 시도 제한(계정 잠금) 판정 헬퍼
# ---------------------------------------------------------------------------


def _as_aware_utc(value: datetime) -> datetime:
    """DB에서 읽은 datetime이 naive이면 UTC로 간주해 tzinfo를 붙인다.

    [버그 수정] SQLite는 DateTime(timezone=True) 컬럼이어도 tzinfo를 실제로
    저장하지 않는다. 저장할 때는 datetime.now(timezone.utc)(aware)를 넣지만,
    조회해서 다시 꺼내면 naive datetime(tzinfo=None)으로 돌아온다(PostgreSQL
    등 timezone-aware를 진짜로 지원하는 DB와 다른 동작). 이 프로젝트는 이
    컬럼에 항상 UTC 시각만 저장하므로, naive 값은 안전하게 UTC로 간주해도 된다.
    이 보정 없이 naive와 aware datetime을 비교하면 TypeError가 발생한다
    (로그인 5회 실패 후 locked_until이 채워진 채 다시 조회될 때 재현됨).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_locked(locked_until: datetime | None) -> bool:
    """계정이 현재 잠금 상태인지 판정한다(locked_until이 미래 시각이면 잠김)."""
    if locked_until is None:
        return False
    return _as_aware_utc(locked_until) > datetime.now(timezone.utc)


def remaining_lock_seconds(locked_until: datetime) -> float:
    """잠금 해제까지 남은 시간을 초 단위로 계산한다.

    호출부(main.py)가 DB에서 읽은 naive datetime을 그대로 넘겨도 되도록,
    aware 보정(_as_aware_utc)을 여기서 함께 처리한다(is_locked가 True로
    판정한 뒤에 호출하는 것을 전제로 한다).
    """
    return (_as_aware_utc(locked_until) - datetime.now(timezone.utc)).total_seconds()


def compute_lock_until(failed_count: int) -> datetime | None:
    """실패 횟수가 임계치에 도달했을 때 잠금 해제 시각을 계산한다.

    Returns:
        failed_count가 LOGIN_MAX_ATTEMPTS 이상이면 "지금부터 15분 후" 시각.
        아직 임계치 미달이면 None(잠그지 않음).
    """
    if failed_count < LOGIN_MAX_ATTEMPTS:
        return None
    return datetime.now(timezone.utc) + timedelta(minutes=LOGIN_LOCK_MINUTES)


# ---------------------------------------------------------------------------
# [Week1] 로그인 세션 저장소 (프로세스 메모리, 데모 범위)
# ---------------------------------------------------------------------------

# 세션 토큰 -> {"guardian_id": str, "created_at": datetime}.
# 서버 재시작 시 초기화된다. 실서비스 전환 시 Redis 등으로 교체 필요.
_SESSIONS: dict[str, dict] = {}

SESSION_TOKEN_BYTES = 32


def create_session(guardian_id: str) -> str:
    """로그인 성공 시 새 세션 토큰을 발급하고 저장소에 등록한다."""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    _SESSIONS[token] = {"guardian_id": guardian_id, "created_at": datetime.now(timezone.utc)}
    return token


def get_session_guardian_id(token: str | None) -> str | None:
    """세션 토�큰으로 guardian_id를 조회한다. 없거나 유효하지 않으면 None."""
    if not token:
        return None
    session = _SESSIONS.get(token)
    if session is None:
        return None
    return session["guardian_id"]


def delete_session(token: str | None) -> None:
    """로그아웃 시 세션을 폐기한다. 존재하지 않는 토큰이어도 조용히 무시한다."""
    if token:
        _SESSIONS.pop(token, None)


# ---------------------------------------------------------------------------
# [Week2] 발견자 익명 세션 (채팅 이력 열람 시작 시점 기록)
# ---------------------------------------------------------------------------
#
# 발견자는 로그인이 없어서 "이 사람이 이 방에 언제 처음 들어왔는지"를 서버가
# 알 방법이 따로 필요하다. 채팅 화면을 처음 여는 순간 익명 토큰 쿠키를 발급하고
# 방별 입장 시각을 기록해 둔다 — WebSocket 접속 시 이 시각 이후의 메시지만
# 이력으로 보내서, 나중에 합류한 발견자(2번 발견자)가 이전 발견자와 보호자의
# 대화를 소급해서 읽을 수 없게 한다(보호자는 항상 전체 이력을 본다).
#
# 로그인 세션(_SESSIONS)과 마찬가지로 프로세스 메모리에만 존재한다(데모 범위).
# 서버가 재시작되면 발견자의 입장 기록이 사라지는데, 이때는 "재접속 시점부터"
# 다시 보이게 되므로 정보가 더 노출되는 방향의 실패는 아니다(fail-safe).

FINDER_COOKIE_NAME = "relink_finder"

# 발견자 토큰 -> {room_id: {"joined_at": 최초 입장 시각(UTC aware),
#                           "location_shared": 이 발견자가 위치를 공유했는지}}
# [Week2] location_shared를 방(ChatRoom) 단위가 아니라 발견자 단위로 두는 이유:
# 발견자가 여러 명일 수 있는 구조(중복 방 재사용)에서, 1번 발견자가 위치를
# 공유했다고 2번 발견자까지 위치 없이 채팅이 열리면 GAP B("위치 전송이 곧
# 채팅 입장권")가 두 번째 발견자부터는 무력화되기 때문이다.
_FINDER_SESSIONS: dict[str, dict[str, dict]] = {}


def get_or_create_finder_token(token: str | None) -> str:
    """쿠키의 발견자 토큰이 유효하면 그대로 쓰고, 없거나 모르면 새로 발급한다."""
    if token and token in _FINDER_SESSIONS:
        return token
    new_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    _FINDER_SESSIONS[new_token] = {}
    return new_token


def _finder_room_entry(token: str, room_id: str) -> dict:
    """발견자-방 상태 레코드를 가져오거나 새로 만든다(내부용)."""
    rooms = _FINDER_SESSIONS.setdefault(token, {})
    entry = rooms.get(room_id)
    if entry is None:
        entry = {"joined_at": datetime.now(timezone.utc), "location_shared": False}
        rooms[room_id] = entry
    return entry


def mark_finder_joined(token: str, room_id: str) -> datetime:
    """이 발견자의 해당 방 최초 입장 시각을 기록한다(이미 있으면 유지).

    "최초" 시각을 유지하는 게 핵심이다 — 새로고침할 때마다 갱신되면 발견자가
    자기 자신이 보낸 이전 메시지도 못 보게 된다.
    """
    return _finder_room_entry(token, room_id)["joined_at"]


def get_finder_joined_at(token: str | None, room_id: str) -> datetime | None:
    """발견자 토큰의 해당 방 최초 입장 시각을 조회한다. 기록이 없으면 None."""
    if not token:
        return None
    entry = _FINDER_SESSIONS.get(token, {}).get(room_id)
    return entry["joined_at"] if entry else None


def mark_finder_location_shared(token: str | None, room_id: str) -> None:
    """이 발견자가 해당 방에서 위치를 공유했음을 기록한다.

    토큰이 없으면(쿠키 없이 WS 직접 접속 등) 기록할 곳이 없으므로 조용히
    무시한다 — 이 경우 해당 연결 동안만 채팅이 열리고(호출부의 지역 변수),
    재접속하면 다시 위치를 공유해야 한다(fail-safe).
    """
    if not token:
        return
    _finder_room_entry(token, room_id)["location_shared"] = True


def has_finder_shared_location(token: str | None, room_id: str) -> bool:
    """이 발견자가 해당 방에서 이미 위치를 공유했는지 조회한다."""
    if not token:
        return False
    entry = _FINDER_SESSIONS.get(token, {}).get(room_id)
    return bool(entry and entry["location_shared"])


def as_aware_utc(value: datetime) -> datetime:
    """_as_aware_utc의 공개 버전 — DB에서 읽은 naive datetime을 UTC로 보정한다.

    main.py가 발견자 이력 필터링(입장 시각과 Message.created_at 비교)에 쓴다.
    SQLite는 tzinfo를 저장하지 않으므로 이 보정 없이는 aware/naive 비교로
    TypeError가 난다(is_locked에서 실제로 겪은 버그와 같은 원인).
    """
    return _as_aware_utc(value)


# ---------------------------------------------------------------------------
# [Week2] PIN 재설정용 OTP (일회성 인증번호)
# ---------------------------------------------------------------------------
#
# PLAN.md "PIN 재설정" / "OTP 발송 자체도 남용 방지 필요":
# - "PIN 재설정" 요청은 로그인 없이 아무나 아무 번호로 누를 수 있으므로,
#   같은 번호로 60초 내 재요청 불가 + 1시간 내 최대 5회로 제한한다
#   (안 그러면 임의의 번호로 SMS 스팸/비용 유발이 가능하다).
# - 발송된 OTP는 5분 후 만료 + 1회 사용 후 즉시 무효화한다.
# - 실제 SMS 발송은 sms.py(목업)를 그대로 재사용한다. 이 모듈은 OTP
#   코드 자체의 생성·저장·검증·rate limit만 담당한다(관심사 분리).

OTP_LENGTH = 6
OTP_EXPIRE_MINUTES = 5
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_PER_HOUR = 5

# phone -> {
#   "code": str,                # 현재 유효한 OTP(검증 성공 또는 만료 시 제거)
#   "expires_at": datetime,
#   "last_sent_at": datetime,   # 재요청 쿨다운 판정 기준
#   "sent_at_history": list[datetime],  # 1시간 내 발송 횟수 판정용
# }
_OTP_STORE: dict[str, dict] = {}


def _prune_hourly_history(history: list[datetime], now: datetime) -> list[datetime]:
    """1시간이 지난 발송 이력을 제거한다(윈도우 슬라이딩)."""
    cutoff = now - timedelta(hours=1)
    return [ts for ts in history if ts > cutoff]


def can_request_otp(phone: str) -> tuple[bool, int | None]:
    """이 전화번호로 지금 OTP를 새로 보내도 되는지 판정한다.

    Returns:
        (허용 여부, 남은 대기 시간(초)|None).
        - 쿨다운(60초) 안에 재요청하면 (False, 남은 초).
        - 1시간 내 5회를 이미 채웠으면 (False, None) — 시간 단위 제한이라
          "몇 초 남았다"는 안내보다는 "잠시 후 다시 시도"가 더 적절하다.
        - 허용되면 (True, None).
    """
    now = datetime.now(timezone.utc)
    entry = _OTP_STORE.get(phone)
    if entry is None:
        return True, None

    last_sent_at = entry.get("last_sent_at")
    if last_sent_at is not None:
        elapsed = (now - last_sent_at).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            return False, int(OTP_RESEND_COOLDOWN_SECONDS - elapsed) + 1

    history = _prune_hourly_history(entry.get("sent_at_history", []), now)
    if len(history) >= OTP_MAX_PER_HOUR:
        return False, None

    return True, None


def issue_otp(phone: str) -> str:
    """새 OTP를 발급하고 저장소에 등록한다. 호출 전에 can_request_otp로 확인할 것.

    Returns:
        평문 OTP 코드(문자열, 숫자 6자리). 호출부가 sms.send_sms로 발송한다.
    """
    now = datetime.now(timezone.utc)
    code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))

    entry = _OTP_STORE.get(phone, {})
    history = _prune_hourly_history(entry.get("sent_at_history", []), now)
    history.append(now)

    _OTP_STORE[phone] = {
        "code": code,
        "expires_at": now + timedelta(minutes=OTP_EXPIRE_MINUTES),
        "last_sent_at": now,
        "sent_at_history": history,
    }
    return code


def verify_otp(phone: str, code: str) -> bool:
    """OTP를 검증한다. 성공/실패 여부와 무관하게 검증 시도 자체로 그 OTP는 소모된다.

    "1회 사용 후 즉시 무효화"(PLAN)를 만족하기 위해, 검증에 성공하든 실패하든
    같은 코드로 두 번 시도할 수 없도록 항상 저장소에서 제거한다. 재시도가
    필요하면 사용자는 재요청(issue_otp)을 통해 새 OTP를 받아야 한다.

    Returns:
        코드가 존재하고, 만료 전이며, 값이 일치하면 True. 그 외에는 False.
    """
    entry = _OTP_STORE.pop(phone, None)  # 시도와 동시에 무효화(1회용).
    if entry is None:
        return False

    if datetime.now(timezone.utc) > entry["expires_at"]:
        return False  # 만료된 OTP. 이미 pop했으므로 재사용 불가.

    return hmac.compare_digest(entry["code"], code)


# ---------------------------------------------------------------------------
# [Week3] 가족 초대 코드 (아이 1명에 보호자 여러 명을 연결하기 위한 1회용 코드)
# ---------------------------------------------------------------------------
#
# QrToken의 "짧은 코드로 매칭"(serial) 패턴을 그대로 재사용한다. 다른 점은:
# - QrToken은 DB 테이블(영구 저장, 배치로 미리 발급)이지만, 초대 코드는 즉석에서
#   발급하고 짧게 쓰고 버리는 값이라 DB 컬럼 형식(코드 문자열 생성 규칙)만 이
#   모듈에서 담당하고, 실제 저장/조회/만료·소모 처리는 models.InviteCode 테이블에서
#   한다(main.py가 오케스트레이션). 이 함수는 "코드 문자열을 어떻게 생성하는가"만
#   책임진다 — DB 접근이 없으므로 예외를 던지지 않는다.
# - 유효기간은 24시간(OTP의 5분보다 훨씬 길다) — 문자/카톡으로 전달하고 상대방이
#   회원가입까지 마칠 시간을 감안해야 하기 때문(OTP는 그 자리에서 바로 입력하는
#   값이라 짧아도 된다).

INVITE_CODE_LENGTH = 8
INVITE_CODE_EXPIRE_HOURS = 24

# QrToken._SERIAL_ALPHABET과 동일하게, 헷갈리는 문자(0/O, 1/I/L)를 제외한다.
# 문자/카톡으로 손으로 전달하고 손으로 입력하는 값이므로 QR 시리얼과 같은
# 이유로 같은 문자셋을 쓴다(generate_qr_batch.py 참고).
_INVITE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_invite_code() -> str:
    """초대 코드 문자열을 새로 생성한다(DB 저장/중복 검사는 호출부 책임).

    Returns:
        길이 INVITE_CODE_LENGTH의 랜덤 코드(예: "AB3D9KMP").
    """
    return "".join(secrets.choice(_INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))


def compute_invite_code_expiry() -> datetime:
    """지금부터 INVITE_CODE_EXPIRE_HOURS 후의 만료 시각을 계산한다."""
    return datetime.now(timezone.utc) + timedelta(hours=INVITE_CODE_EXPIRE_HOURS)


def is_invite_code_valid(expires_at: datetime, used_at: datetime | None) -> bool:
    """초대 코드가 아직 쓸 수 있는 상태인지 판정한다(만료 전이고 미사용).

    DB에서 읽은 naive datetime을 그대로 넘겨도 되도록 aware 보정을 여기서
    처리한다(SQLite가 tzinfo를 안 저장하는 문제, is_locked와 같은 이유).
    """
    if used_at is not None:
        return False
    return _as_aware_utc(expires_at) > datetime.now(timezone.utc)

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


def is_locked(locked_until: datetime | None) -> bool:
    """계정이 현재 잠금 상태인지 판정한다(locked_until이 미래 시각이면 잠김)."""
    if locked_until is None:
        return False
    return locked_until > datetime.now(timezone.utc)


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

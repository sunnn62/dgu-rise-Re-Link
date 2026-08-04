"""실종아동 QR 발견-신고 채팅 서비스: FastAPI 앱 진입점.

=== [Week1] PLAN.md 반영: 로그인+PIN 인증 방식으로 전환 ===
예전에는 보호자 인증이 "관리 링크(manage_token) 소유"였다. PLAN.md 최신 버전에서
이 방식은 두 가지 문제(계정 탈취 유사 구조, 링크 분실 시 재발급 어려움) 때문에
"전화번호(ID) + PIN(비밀번호) 로그인"으로 교체되었다. 이에 따라:
- 회원가입(/register), 로그인(/login), 로그아웃(/logout)이 실제로 동작한다.
- 보호자 대시보드는 URL에 토큰을 담지 않는 세션 기반 라우팅(/guardian/dashboard)이다.
- 채팅방 접근(GET /chat/{room_id}?role=guardian, WS /ws/chat/{room_id}?role=guardian)은
  더 이상 방마다 발급되는 1회성 토큰이 아니라, 매번 "로그인 세션의 guardian_id가
  이 방의 아이에 연결돼(ChildGuardian) 있는가"를 재검증한다(GAP B, 다른 보호자의
  방에 들어가지 못하게 막는 소유권 검증).

=== [Week3] 가족 초대 기능(아이 한 명 ↔ 보호자 여러 명) ===
아이 한 명에 보호자가 여러 명 연결될 수 있다(예: 부모 두 명이 같은 아이를 함께
관리). Child와 Guardian은 ChildGuardian 중간 테이블로 다대다 관계를 맺는다.
- 최초 등록자(role="primary")가 초대 코드(POST .../{child_id}/invite)를 발급하면
  8자리 코드가 24시간 동안 유효하다(QrToken의 시리얼 매칭 패턴을 재사용).
- 다른 보호자가 로그인 후 그 코드를 입력(POST /guardian/dashboard/join)하면
  ChildGuardian(role="invited") 레코드가 생기고, 그 즉시 코드는 소멸(1회용)한다.
- 두 role 모두 채팅 열람/실종 신고 토글/QR 다운로드는 동일하게 가능하다.
  아이 삭제와 초대 코드 발급만 role="primary"에게 제한된다(초대받은 사람의
  실수나 다툼으로 아이 데이터가 통째로 사라지는 사고 방지).

라우트 구성:
- [Week1] 보호자 회원가입/로그인/로그아웃: GET·POST /register, GET·POST /login, POST /logout
- [Week1] 보호자 대시보드(세션 기반): GET /guardian/dashboard,
  POST /guardian/dashboard/children (아이 추가), .../{child_id}/toggle, .../{child_id}/delete
- [Week3] 가족 초대: POST /guardian/dashboard/children/{child_id}/invite (코드 발급),
  POST /guardian/dashboard/join (코드로 합류)
- [Week3] 장소 검색(GET /api/place-search): 실내 등 GPS 오차가 큰 상황에서 발견자가
  장소명을 검색해 좌표를 직접 지정할 수 있도록 카카오 로컬 API를 서버가 대신 호출한다
  (REST API 키를 클라이언트에 노출하지 않기 위한 프록시).
- 관리자(개발용 시드 도구): 아이 등록/QR 발급/상태 전환/방 종료 (POST /admin/children 등)
- 발견자: 랜딩(GET /found/{qr_token}), 발견 신고 시작(POST /found/{qr_token}/start)
- 채팅 화면(GET /chat/{room_id}) 및 실시간 WebSocket(/ws/chat/{room_id})
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

# [Week2] .env 파일에서 UPSTAGE_API_KEY 등 비밀값을 환경변수로 로드한다.
# moderation 모듈이 os.getenv("UPSTAGE_API_KEY")를 읽기 전에 실행돼야 하므로
# 반드시 이 파일의 다른 프로젝트 모듈 import보다 먼저 호출한다. .env 파일이
# 없어도 조용히 아무 일도 하지 않는다(find_dotenv가 못 찾으면 load_dotenv는
# False를 반환할 뿐 예외를 던지지 않음 - dotenv 자체의 fail-open 동작).
from dotenv import load_dotenv

load_dotenv()

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import Request

import auth  # [Week1] PIN 해싱/검증 + 세션 관리
import generate_qr_batch  # [Week3] 배포 환경에서 QR 배치를 브라우저로 생성하는 데 재사용
import moderation  # [Week2] LLM 기반 채팅 메시지 악용 탐지
import qr_utils
import sms
from database import SessionLocal, get_db, init_db
from models import ChatRoom, Child, ChildGuardian, Guardian, InviteCode, Message, QrToken
from websocket_manager import ConnectionManager

# 이 파일이 위치한 디렉터리 기준으로 templates/static 경로를 잡는다(실행 위치 무관).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="실종아동 QR 발견-신고 채팅 서비스")

# 정적 파일(css/js) 및 Jinja2 템플릿 설정.
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# room별 실시간 커넥션을 관리하는 싱글턴. 프로세스 메모리에만 존재한다.
manager = ConnectionManager()

# QR/SMS 링크에 담길 도메인의 베이스. 배포 시 실제 도메인으로 교체할 것.
# generate_qr_batch.py와 반드시 같은 값을 써야 한다(둘 다 BASE_URL 환경변수 사용).
# [.env 주의] ".env"에 "BASE_URL=" 처럼 빈 값이 있으면 dotenv가 빈 문자열을 환경
# 변수로 설정한다(키 자체가 없는 것과 다름). os.getenv의 기본값은 키가 아예
# 없을 때만 쓰이므로, "or"로 빈 문자열도 기본값으로 치환되도록 명시적으로 처리한다.
BASE_URL = os.getenv("BASE_URL") or "http://localhost:8000"

# [Week3] 카카오 로컬 API(장소 검색) REST API 키. 없으면 /api/place-search가
# 503으로 응답한다(fail-closed — 검색 UI는 부가 기능이라 서버 기동 자체를
# 막지는 않지만, 키 없이 카카오 API를 호출할 수는 없으므로 그 기능만 비활성화).
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY") or None
_KAKAO_LOCAL_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
_PLACE_SEARCH_TIMEOUT_SECONDS = 5.0

# qr_token 길이(32바이트 이상 요구사항 충족).
QR_TOKEN_BYTES = 32

# [Week1] 로그인 세션 쿠키 유지 기간(7일). 만료되면 재로그인이 필요하다.
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 7

# 유효한 발신자 역할. 서버가 쿼리 파라미터로 받은 role을 신뢰의 기준으로 삼는다.
VALID_ROLES = {"finder", "guardian"}


# [Week2/P2] 개발용 시드 도구(/admin/*) 최소 인증용 API 키.
# .env에 ADMIN_API_KEY를 설정하지 않으면 이 값은 None이 되고, require_admin이
# 이를 "관리자 라우트 잠금"으로 해석해 401로 거부한다 — 모더레이션의 fail-open과는
# 반대로 여기는 fail-closed가 맞다(관리자 도구는 안전 기능이 아니라 공격 표면이므로,
# 설정을 깜빡했을 때 "그냥 열려있음"보다 "막혀있음"이 훨�다 안전하다).
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY") or None

# [Week2/P2] 배포 시 QR/SMS 링크가 실수로 HTTP 평문 도메인으로 나가는 것을 막기
# 위한 안전장치. 로컬 개발(localhost/127.0.0.1)은 예외로 허용한다(HTTPS 없이도
# 개발 가능해야 하므로). ENFORCE_HTTPS_BASE_URL=false로 명시적으로 끌 수 있다.
ENFORCE_HTTPS_BASE_URL = (os.getenv("ENFORCE_HTTPS_BASE_URL") or "true").lower() != "false"


def _validate_base_url(base_url: str, enforce_https: bool) -> None:
    """[Week2/P2] BASE_URL이 QR/SMS 링크에 쓰기에 안전한 형태인지 검사한다.

    PLAN "발표 당일 최소 조건": "HTTPS 적용 - Geolocation API는 HTTPS 또는
    localhost가 아니면 브라우저가 막음"이 배포 후에야 발견되면 이미 QR 스티커를
    인쇄한 뒤일 수 있다. 앱 기동 시점에 미리 검증해서, 실수로 http://로 배포
    도메인을 넣었을 때 서버가 조용히 뜨는 대신 바로 알 수 있게 한다.

    Raises:
        ValueError: BASE_URL이 파싱 불가능한 URL이거나, HTTPS 강제 조건에서
            localhost가 아닌 http:// 도메인일 때.
    """
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"BASE_URL이 올바른 URL 형식이 아닙니다: {base_url!r}")

    hostname = (parsed.hostname or "").lower()
    is_local = hostname in {"localhost", "127.0.0.1", "::1"}

    if enforce_https and parsed.scheme != "https" and not is_local:
        raise ValueError(
            f"BASE_URL이 HTTPS가 아닙니다({base_url!r}). 배포 도메인은 반드시 https://로 "
            "설정해야 합니다(Geolocation API가 HTTP에서는 동작하지 않습니다). "
            "로컬 개발이면 http://localhost:8000을 쓰거나, .env에 "
            "ENFORCE_HTTPS_BASE_URL=false를 설정해 이 검사를 건너뛸 수 있습니다."
        )


def require_admin(request: Request) -> None:
    """[Week2/P2] /admin/* 라우트의 최소 인증. FastAPI 의존성으로 사용한다.

    Header "X-Admin-Key"가 .env의 ADMIN_API_KEY와 일치해야 통과한다. 이 값이
    설정되지 않은 배포는 관리자 도구 자체를 완전히 잠근다(fail-closed) — 인증
    없이 아무나 아이를 등록하거나 채팅방을 강제 종료할 수 있으면 안 되므로.

    상수 시간 비교(hmac.compare_digest)로 타이밍 공격을 방지한다.

    Raises:
        HTTPException(401): 키가 없거나(서버 미설정 포함) 일치하지 않을 때.
    """
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="관리자 도구가 잠겨 있습니다. .env에 ADMIN_API_KEY를 설정해 주세요.",
        )

    provided = request.headers.get("X-Admin-Key", "")
    if not hmac.compare_digest(provided, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="관리자 인증에 실패했습니다.")


def _is_admin_request(request: Request) -> bool:
    """require_admin의 비파괴(불리언) 버전 — 예외 대신 True/False만 반환한다.

    관리자 전용이 아니라 "관리자 또는 소유 보호자" 둘 다 허용해야 하는 라우트
    (download_child_qr)에서 쓴다. require_admin처럼 401을 즉시 던지면 보호자
    세션 검사로 넘어갈 수 없기 때문에 판정만 분리했다.
    """
    if not ADMIN_API_KEY:
        return False
    provided = request.headers.get("X-Admin-Key", "")
    return hmac.compare_digest(provided, ADMIN_API_KEY)


@app.on_event("startup")
def on_startup() -> None:
    """앱 시작 시 DB 테이블을 준비하고, 배포 안전장치(BASE_URL 등)를 점검한다."""
    init_db()

    try:
        _validate_base_url(BASE_URL, ENFORCE_HTTPS_BASE_URL)
    except ValueError as error:
        # [Week2/P2] PLAN "발표 당일 최소 조건" 위반을 배포 시점에 즉시 드러낸다.
        # 여기서 서버 기동을 실패시키는 것이 의도다 — QR 인쇄 전에 반드시 잡아야
        # 하는 실수이므로, 조용히 넘어가는 것보다 시끄럽게 막는 쪽이 안전하다.
        raise RuntimeError(str(error)) from error

    if not ADMIN_API_KEY:
        print(
            "[경고] ADMIN_API_KEY가 설정되지 않았습니다. /admin/* 라우트가 모두 "
            "401로 잠긴 상태입니다. 개발용 시드 도구를 쓰려면 .env에 "
            "ADMIN_API_KEY=<임의의 긴 문자열>을 설정하세요."
        )


@app.get("/", response_class=HTMLResponse)
def home_page(request: Request) -> HTMLResponse:
    """서비스 소개 홈 화면을 렌더링한다."""
    return templates.TemplateResponse(
        request,
        "home.html",
        {"current_year": datetime.now(timezone.utc).year},
    )


# ---------------------------------------------------------------------------
# [Week1] 세션 헬퍼: 로그인 여부/소유권 판정의 단일 진입점
# ---------------------------------------------------------------------------


def get_current_guardian(request: Request, db: Session) -> Guardian | None:
    """[Week1] 요청의 세션 쿠키로 로그인한 보호자를 조회한다.

    쿠키가 없거나 세션이 유효하지 않으면 None. DB 조회 자체가 실패하는
    경우에도 예외를 전파하지 않고 None을 반환한다 — 이 함수는 "접근 허용
    여부"를 판정하는 게이트로만 쓰이므로, 오류 상황에서는 fail-closed(접근
    거부 = 로그인 페이지로)가 fail-open보다 안전하다는 것이 의도적인 설계다.
    호출부(대시보드 라우트들)는 이미 다른 쿼리에서 SQLAlchemyError를 별도로
    처리하므로, 여기서 조용히 삼키는 것이 이중으로 오류를 띄우지 않게 한다.
    """
    token = request.cookies.get(auth.SESSION_COOKIE_NAME)
    guardian_id = auth.get_session_guardian_id(token)
    if guardian_id is None:
        return None
    try:
        return db.query(Guardian).filter(Guardian.id == guardian_id).one_or_none()
    except SQLAlchemyError as error:
        print(f"[경고] 세션 조회 중 DB 오류(guardian_id={guardian_id}): {error}")
        return None


def _set_session_cookie(response: Response, session_token: str) -> None:
    """[Week1] 로그인 성공 시 세션 쿠키를 응답에 실어 보낸다.

    httponly로 JS 접근을 막고(XSS로 세션 토큰 탈취 방지), samesite=lax로
    기본적인 CSRF 노출을 줄인다. 배포 시 HTTPS 환경에서는 secure=True도 추가할 것.
    """
    response.set_cookie(
        auth.SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_COOKIE_MAX_AGE,
    )


# ---------------------------------------------------------------------------
# [개발용 시드 도구] 관리자용 아이 등록/QR 발급/상태 전환/방 종료
# 실사용 등록 경로는 아래 [Week1] 회원가입/대시보드 섹션이다.
# ---------------------------------------------------------------------------


class ChildCreateRequest(BaseModel):
    """[개발용 시드 도구] 아이 등록 요청 바디."""

    name: str = Field(min_length=1, max_length=100)
    guardian_phone: str = Field(min_length=1, max_length=20)
    guardian_name: str = Field(min_length=1, max_length=100)
    # [Week1] Guardian.pin_hash가 NOT NULL이므로 시드 도구에도 PIN이 필요하다.
    # 기존 전화번호를 재사용할 때는 이미 저장된 pin_hash가 있으므로 이 값은 무시된다.
    guardian_pin: str = Field(default="000000", pattern="^[0-9]{6,}$")
    # 등록 직후 상태. 기본은 normal(등록만, 실종 신고 안 함). 테스트 시 missing 지정 가능.
    status: str = Field(default="normal", pattern="^(normal|missing)$")


class ChildStatusRequest(BaseModel):
    """[개발용 시드 도구] 아이 상태 전환 요청 바디."""

    status: str = Field(pattern="^(normal|missing)$")


class ChildCreateResponse(BaseModel):
    """아이 등록 응답. 내부 PK(id)는 노출하지 않고 qr_token만 반환한다."""

    qr_token: str
    found_url: str
    qr_download_url: str


def _get_or_create_guardian(db: Session, phone: str, name: str, pin: str) -> Guardian:
    """[개발용 시드 도구 전용] 전화번호로 기존 보호자를 찾고, 없으면 새로 만든다.

    [Week1] 주의: 이 "있으면 재사용" 로직은 개발/테스트용 시드 도구(create_child)
    에서만 쓴다. 실제 보호자 자가 회원가입(register_submit)은 PLAN의 결정에 따라
    전화번호가 이미 있으면 절대 그 계정에 자동으로 붙이지 않고 명시적으로 실패
    처리한다 — 이 두 경로는 서로 다른 보안 요건 때문에 의도적으로 다르게 동작한다.

    호출부에서 commit/rollback을 책임진다(여기서는 add/flush만 수행).

    Raises:
        SQLAlchemyError: 조회/삽입 중 DB 오류. 호출부에서 처리한다.
    """
    phone = auth.normalize_phone(phone)
    guardian = db.query(Guardian).filter(Guardian.phone == phone).one_or_none()
    if guardian is not None:
        return guardian

    guardian = Guardian(phone=phone, name=name, pin_hash=auth.hash_pin(pin))
    db.add(guardian)
    db.flush()  # guardian.id를 확보하기 위해 flush(커밋은 호출부에서).
    return guardian


@app.post(
    "/admin/children", response_model=ChildCreateResponse, dependencies=[Depends(require_admin)]
)
def create_child(payload: ChildCreateRequest, db: Session = Depends(get_db)) -> ChildCreateResponse:
    """[개발용 시드 도구] 보호자를 조회/생성하고 아이를 등록해 QR 토큰을 발급한다.

    실사용 등록 경로는 보호자 자가 회원가입(/register)이며, 이 관리자 엔드포인트는
    개발·테스트용 시드 도구로만 사용한다(PLAN P2).

    Raises:
        HTTPException(500): DB 저장 실패(제약조건 위반, 연결 오류 등) 시.
    """
    # secrets.token_urlsafe(32)는 약 43자의 암호학적으로 안전한 랜덤 문자열을 생성한다.
    qr_token = secrets.token_urlsafe(QR_TOKEN_BYTES)

    try:
        guardian = _get_or_create_guardian(
            db, payload.guardian_phone, payload.guardian_name, payload.guardian_pin
        )
        child = Child(
            name=payload.name,
            primary_guardian_id=guardian.id,
            status=payload.status,
            qr_token=qr_token,
        )
        db.add(child)
        db.flush()  # [Week3] ChildGuardian이 참조할 child.id를 확보하기 위해 flush.
        db.add(ChildGuardian(child_id=child.id, guardian_id=guardian.id, role="primary"))
        db.commit()
    except IntegrityError as error:
        db.rollback()
        # qr_token unique 제약 충돌 등. 극히 드물지만 재시도 안내를 제공한다.
        raise HTTPException(
            status_code=500,
            detail="토큰 생성 중 충돌이 발생했습니다. 다시 시도해 주세요.",
        ) from error
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"아이 등록 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    found_url = qr_utils.build_found_url(BASE_URL, qr_token)
    return ChildCreateResponse(
        qr_token=qr_token,
        found_url=found_url,
        qr_download_url=f"/admin/children/{qr_token}/qr",
    )


@app.post("/admin/children/{qr_token}/status", dependencies=[Depends(require_admin)])
def set_child_status(
    qr_token: str, payload: "ChildStatusRequest", db: Session = Depends(get_db)
) -> dict:
    """[개발용 시드 도구] 아이의 실종 상태(normal/missing)를 전환한다.

    실사용에서는 보호자 대시보드의 토글이 이 역할을 하지만, GAP A 검증을
    테스트하기 위한 개발용 엔드포인트다.

    Raises:
        HTTPException(404): qr_token에 해당하는 아이가 없을 때.
        HTTPException(500): DB 오류 시.
    """
    try:
        child = db.query(Child).filter(Child.qr_token == qr_token).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500,
            detail=f"아이 조회 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    if child is None:
        raise HTTPException(status_code=404, detail="해당 QR 토큰으로 등록된 아이를 찾을 수 없습니다.")

    child.status = payload.status
    try:
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"상태 변경 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    return {"qr_token": qr_token, "status": child.status}


@app.get("/admin/children/{qr_token}/qr")
def download_child_qr(request: Request, qr_token: str, db: Session = Depends(get_db)) -> Response:
    """등록된 아이의 QR 이미지를 PNG로 다운로드한다.

    [Week2 버그 수정] 이 라우트는 관리자 시드 도구가 아니라 **보호자 대시보드의
    "QR 코드 다운로드" 버튼이 실제로 쓰는 사용자 기능**이다. /admin/* 일괄 인증
    작업 때 require_admin이 함께 걸리면서 보호자가 자기 아이 QR을 못 받는 문제
    (일반 <a href> 링크는 X-Admin-Key 헤더를 붙일 방법이 없음)가 생겨, 인증을
    "관리자 키 또는 로그인한 소유 보호자" 둘 중 하나로 완화했다.
    - 관리자 키(X-Admin-Key)가 유효하면 통과 (개발/시드 용도)
    - 아니면 세션 쿠키의 보호자가 이 아이의 소유자일 때만 통과 (대시보드 버튼)

    Raises:
        HTTPException(403): 관리자도 아니고 소유 보호자도 아닐 때.
        HTTPException(404): qr_token에 해당하는 아이가 없을 때.
        HTTPException(500): QR 이미지 생성 실패 시.
    """
    try:
        child = db.query(Child).filter(Child.qr_token == qr_token).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500,
            detail=f"아이 조회 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    if child is None:
        raise HTTPException(status_code=404, detail="해당 QR 토큰으로 등록된 아이를 찾을 수 없습니다.")

    if not _is_admin_request(request):
        guardian = get_current_guardian(request, db)
        if guardian is None:
            raise HTTPException(
                status_code=403,
                detail="본인이 등록한 아이의 QR만 다운로드할 수 있습니다. 로그인해주세요.",
            )
        # [Week3] "소유자"가 아니라 "연결된 보호자인가"로 판정한다(초대로 합류한
        # 가족 구성원도 QR을 다시 받을 수 있어야 하므로).
        try:
            linked = _is_guardian_linked_to_child(db, guardian.id, child.id)
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=500,
                detail=f"권한 확인 중 데이터베이스 오류가 발생했습니다: {error}",
            ) from error
        if not linked:
            raise HTTPException(
                status_code=403,
                detail="본인이 등록한 아이의 QR만 다운로드할 수 있습니다. 로그인해주세요.",
            )

    found_url = qr_utils.build_found_url(BASE_URL, child.qr_token)

    try:
        png_bytes = qr_utils.generate_qr_png(found_url)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=500, detail=f"QR 이미지를 생성하지 못했습니다: {error}") from error

    return Response(content=png_bytes, media_type="image/png")


# [Week3] 배포 환경(Render 등)에서 서버 셸/One-Off Jobs 없이 QR 사전 발급 배치를
# 만들기 위한 관리자 페이지. 원래는 로컬에서 generate_qr_batch.py를 직접 실행했지만,
# 무료 배포 티어는 셸 접근을 안 주는 경우가 많아 같은 로직을 브라우저 요청으로
# 트리거하도록 노출한다. 결과는 서버 디스크에 저장하지 않고 ZIP으로 바로 내려준다
# (배포 환경 재시작 시 디스크가 초기화될 수 있으므로).
_QR_BATCH_MAX_COUNT = 500


@app.get("/admin/qr-batch", response_class=HTMLResponse)
def qr_batch_form() -> str:
    """QR 배치 생성 폼(관리자 키 + 개수 입력). 최소한의 마크업만 제공한다."""
    return """<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><title>QR 배치 생성(관리자)</title></head>
<body>
<h1>QR 배치 생성</h1>
<p>관리자 키와 생성할 개수를 입력하면 QR PNG + serials.csv가 담긴 ZIP을 바로 다운로드합니다.</p>
<form method="post" action="/admin/qr-batch">
  <div><label>관리자 키 <input type="password" name="admin_key" required></label></div>
  <div><label>생성 개수 <input type="number" name="count" value="10" min="1" max="500" required></label></div>
  <button type="submit">생성 및 ZIP 다운로드</button>
</form>
</body></html>"""


@app.post("/admin/qr-batch")
def qr_batch_generate(
    admin_key: str = Form(...), count: int = Form(...), db: Session = Depends(get_db)
) -> Response:
    """관리자 키 + 개수를 받아 QrToken 배치를 생성하고 ZIP으로 내려준다.

    일반 <form> POST는 커스텀 헤더(X-Admin-Key)를 못 보내므로, 이 라우트만
    예외적으로 관리자 키를 form 필드로 받는다. require_admin의 fail-closed
    원칙은 그대로 유지한다(키 미설정/불일치 시 401).

    Raises:
        HTTPException(401): 관리자 키가 없거나 틀린 경우.
        HTTPException(400): count가 유효 범위를 벗어난 경우.
        HTTPException(500): QR 생성/DB 저장 중 오류가 발생한 경우.
    """
    if not ADMIN_API_KEY or not hmac.compare_digest(admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="관리자 키가 올바르지 않습니다.")

    if count > _QR_BATCH_MAX_COUNT:
        raise HTTPException(
            status_code=400, detail=f"한 번에 최대 {_QR_BATCH_MAX_COUNT}개까지 생성할 수 있습니다."
        )

    try:
        zip_bytes, created_count = generate_qr_batch.generate_batch_zip(count, db, BASE_URL)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

    filename = f"qr_batch_{created_count}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/rooms/{room_id}/close", dependencies=[Depends(require_admin)])
async def admin_close_room(room_id: str, db: Session = Depends(get_db)) -> dict:
    """관리자가 채팅방을 종료(closed)한다.

    종료 후에는 신규 WebSocket 연결이 거부되고, 현재 접속 중인 커넥션에는
    종료 안내를 보낸 뒤 연결을 닫는다.

    Raises:
        HTTPException(404): 방이 없을 때.
        HTTPException(500): DB 오류 시.
    """
    try:
        room = db.query(ChatRoom).filter(ChatRoom.id == room_id).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500,
            detail=f"채팅방 조회 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    if room is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 채팅방입니다.")

    if room.status != "closed":
        room.status = "closed"
        room.closed_at = datetime.now(timezone.utc)
        try:
            db.commit()
        except SQLAlchemyError as error:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"채팅방 종료 중 데이터베이스 오류가 발생했습니다: {error}",
            ) from error

    # 현재 접속 중인 커넥션에 종료를 알리고 연결을 닫는다.
    await manager.close_room(room_id, "관리자에 의해 채팅방이 종료되었습니다.")
    return {"room_id": room_id, "status": "closed"}


# ---------------------------------------------------------------------------
# 발견자 플로우: 발견 신고 시작(채팅방 생성) + WebSocket 실시간 채팅
# ---------------------------------------------------------------------------


class StartChatResponse(BaseModel):
    """발견 신고 시작 응답. 발견자에게 채팅방 접속 경로를 돌려준다."""

    room_id: str
    chat_url: str
    ws_url: str


# 역할별 표시명. 실명·전화번호 대신 항상 이 값만 화면/시스템 메시지에 사용한다.
ROLE_LABELS = {"finder": "발견자", "guardian": "보호자"}


def _message_to_dict(message: Message) -> dict:
    """Message ORM 객체를 WebSocket 전송용 JSON 딕셔너리로 변환한다."""
    return {
        "type": message.message_type,
        "content": message.content,
        "sender_role": message.sender_role,
        "latitude": message.latitude,
        "longitude": message.longitude,
        # [Week2] 위치 오차 반경(미터). location 메시지가 아니면 None.
        "accuracy": message.accuracy,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


@app.get("/found/{qr_token}", response_class=HTMLResponse)
def found_landing(request: Request, qr_token: str, db: Session = Depends(get_db)) -> HTMLResponse:
    """발견자용 랜딩 페이지(HTML).

    아이 이름 등 개인정보는 노출하지 않고, qr_token만 템플릿에 전달한다.
    존재하지 않는 토큰이면 404를 반환한다.

    Raises:
        HTTPException(404): qr_token에 해당하는 아이가 없을 때.
        HTTPException(500): DB 오류 시.
    """
    try:
        child = db.query(Child).filter(Child.qr_token == qr_token).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500,
            detail=f"아이 조회 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    if child is None:
        raise HTTPException(status_code=404, detail="유효하지 않은 QR입니다.")

    # 템플릿에는 개인정보 없이 QR 토큰과 활성 상태만 전달한다.
    return templates.TemplateResponse(
        request,
        "found_landing.html",
        {"qr_token": qr_token, "is_active": child.status == "missing"},
    )


@app.get("/api/place-search")
async def place_search(query: str) -> dict:
    """[Week3] 카카오 로컬 API(키워드 장소검색)를 서버가 대신 호출하는 프록시.

    실내 등 GPS/WiFi 위치 정확도가 낮은 상황에서, 발견자가 "OO편의점"처럼
    장소 이름을 검색해 좌표를 직접 지정할 수 있게 한다. REST API 키는 서버
    환경변수에만 있고 클라이언트(chat.js)에는 노출하지 않는다.

    Raises:
        HTTPException(503): KAKAO_REST_API_KEY가 설정되지 않은 경우.
        HTTPException(502): 카카오 API 호출이 실패하거나 타임아웃된 경우.
    """
    if not KAKAO_REST_API_KEY:
        raise HTTPException(status_code=503, detail="장소 검색 기능이 아직 설정되지 않았습니다.")

    query = query.strip()
    if not query:
        return {"results": []}

    try:
        async with httpx.AsyncClient(timeout=_PLACE_SEARCH_TIMEOUT_SECONDS) as client:
            response = await client.get(
                _KAKAO_LOCAL_SEARCH_URL,
                params={"query": query, "size": 10},
                headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
            )
        response.raise_for_status()
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=502, detail="장소 검색 응답이 지연되고 있습니다. 다시 시도해 주세요.") from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"장소 검색 중 오류가 발생했습니다: {error}") from error

    try:
        documents = response.json().get("documents", [])
    except ValueError as error:
        raise HTTPException(status_code=502, detail=f"장소 검색 응답을 해석하지 못했습니다: {error}") from error

    results = [
        {
            "name": doc.get("place_name", ""),
            "address": doc.get("road_address_name") or doc.get("address_name", ""),
            "latitude": float(doc["y"]),
            "longitude": float(doc["x"]),
        }
        for doc in documents
        if "y" in doc and "x" in doc
    ]
    return {"results": results}


@app.get("/chat/{room_id}", response_class=HTMLResponse)
def chat_page(
    request: Request,
    room_id: str,
    role: str = "finder",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """발견자·보호자 공용 채팅 화면(HTML).

    [Week1/GAP B] 보호자(role=guardian)는 더 이상 URL의 1회성 토큰이 아니라
    로그인 세션으로 인증한다. 이 방의 아이(child)에 로그인한 보호자가 연결돼
    있는지(ChildGuardian 조회)를 반드시 재검증한다 — 그렇지 않으면 로그인한
    보호자 A가 room_id만 알아내면 보호자 B의 아이 방에 들어갈 수 있는 문제가
    생긴다(PLAN GAP B "role 판정 + 방 소유권" 참고). [Week3] 아이 한 명에
    보호자가 여러 명 연결될 수 있게 되면서, "소유자 1명과 일치하는가"가 아니라
    "연결된 보호자 목록에 포함되는가"로 검증 방식이 바뀌었다.

    Raises:
        HTTPException(404): 방이 없을 때.
        HTTPException(403): role이 잘못됐거나 보호자 소유권 검증에 실패할 때.
        HTTPException(410): 이미 종료(closed)된 방일 때.
        HTTPException(500): DB 오류 시.
    """
    if role not in VALID_ROLES:
        raise HTTPException(status_code=403, detail="잘못된 접근입니다.")

    try:
        room = db.query(ChatRoom).filter(ChatRoom.id == room_id).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500,
            detail=f"채팅방 조회 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    if room is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 채팅방입니다.")

    if role == "guardian":
        # [Week1/GAP B] 세션 기반 소유권 재검증. 로그인 안 됨/연결 안 된 보호자면 거부.
        guardian = get_current_guardian(request, db)
        if guardian is None:
            raise HTTPException(
                status_code=403, detail="이 채팅방에 접근할 권한이 없습니다. 로그인해주세요."
            )
        try:
            linked = _is_guardian_linked_to_child(db, guardian.id, room.child_id)
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=500,
                detail=f"권한 확인 중 데이터베이스 오류가 발생했습니다: {error}",
            ) from error
        if not linked:
            raise HTTPException(
                status_code=403, detail="이 채팅방에 접근할 권한이 없습니다. 로그인해주세요."
            )

    if room.status == "closed":
        raise HTTPException(status_code=410, detail="이미 종료된 채팅방입니다.")

    # [Week1] 보호자는 쿠키(세션)로 인증되므로 ws_url에 별도 토큰을 붙이지 않는다.
    # 같은 오리진의 WebSocket 핸드셰이크에는 브라우저가 쿠키를 자동으로 실어 보낸다.
    ws_url = f"/ws/chat/{room_id}?role={role}"

    response = templates.TemplateResponse(
        request,
        "chat.html",
        {"room_id": room_id, "role": role, "ws_url": ws_url},
    )

    if role == "finder":
        # [Week2] 발견자 익명 세션: 이 방에 처음 들어온 시각을 기록해 둔다.
        # WebSocket 접속 시 이 시각 이후의 메시지만 이력으로 내려줘서, 나중에
        # 합류한 발견자(2번 발견자)가 이전 대화를 소급해 읽지 못하게 한다.
        # (mark_finder_joined는 "최초" 시각을 유지하므로 새로고침해도 자기
        # 대화는 계속 보인다. 보호자는 이 필터 없이 전체 이력을 본다.)
        finder_token = auth.get_or_create_finder_token(
            request.cookies.get(auth.FINDER_COOKIE_NAME)
        )
        auth.mark_finder_joined(finder_token, room_id)
        response.set_cookie(
            auth.FINDER_COOKIE_NAME,
            finder_token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_COOKIE_MAX_AGE,
        )

    return response


@app.post("/found/{qr_token}/start", response_model=StartChatResponse)
def start_chat(qr_token: str, db: Session = Depends(get_db)) -> StartChatResponse:
    """발견자가 '발견했어요'를 누르면 채팅방을 생성하고 보호자에게 SMS를 보낸다.

    [Week1] 예전에는 SMS에 1회성 인증 토큰이 담긴 채팅방 직행 링크를 보냈지만,
    로그인+세션 방식으로 바뀌면서 그 링크가 사라졌다. 이제 SMS는 "로그인해서
    확인하세요" 안내만 하고, 보호자는 대시보드의 "진행 중인 채팅방"에서 입장한다
    (PLAN "진행 중인 채팅방 접근" 참고).

    Raises:
        HTTPException(404): qr_token에 해당하는 아이가 없을 때.
        HTTPException(403): 아이가 실종(missing) 상태가 아닐 때(GAP A).
        HTTPException(500): DB 오류 시.
    """
    try:
        child = db.query(Child).filter(Child.qr_token == qr_token).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500,
            detail=f"아이 조회 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    if child is None:
        raise HTTPException(status_code=404, detail="해당 QR 토큰으로 등록된 아이를 찾을 수 없습니다.")

    # [GAP A] 실종 신고(missing) 상태일 때만 채팅을 시작할 수 있다. 평상시(normal)에는
    # QR을 스캔해도 채팅으로 이어지지 않도록 서버가 재검증한다(프론트 우회 방지).
    if child.status != "missing":
        raise HTTPException(
            status_code=403,
            detail="현재 실종 신고 상태가 아닌 아이입니다. 채팅을 시작할 수 없습니다.",
        )

    # [Week2/P2 남용 방지] 같은 아이에 대해 이미 진행 중인(closed가 아닌) 방이
    # 있으면 새로 만들지 않고 그대로 재사용한다. 이게 없으면 발견자가 같은 QR을
    # 여러 번 스캔/새로고침할 때마다 방이 새로 생겨 SMS가 반복 발송되고, 보호자
    # 대시보드에 방이 계속 쌓이는 문제가 생긴다(PLAN P2 "같은 QR로 채팅방 무한
    # 생성 방지 - rate limit 또는 이미 진행 중인 방 있으면 재사용").
    try:
        existing_room = (
            db.query(ChatRoom)
            .filter(ChatRoom.child_id == child.id, ChatRoom.status != "closed")
            .order_by(ChatRoom.created_at.desc())
            .first()
        )
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500,
            detail=f"채팅방 조회 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    if existing_room is not None:
        # 이미 있는 방을 그대로 돌려준다. SMS도 다시 보내지 않는다(첫 신고 때
        # 이미 보호자에게 알림이 갔으므로, 재스캔마다 반복 발송하면 스팸이 된다).
        return StartChatResponse(
            room_id=existing_room.id,
            chat_url=f"/chat/{existing_room.id}?role=finder",
            ws_url=f"/ws/chat/{existing_room.id}?role=finder",
        )

    # [Week1] guardian_token 없이 방만 생성한다. 보호자 접근은 로그인 세션으로 판정.
    room = ChatRoom(child_id=child.id, status="waiting")

    try:
        db.add(room)
        db.commit()
        db.refresh(room)
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"채팅방 생성 중 데이터베이스 오류가 발생했습니다: {error}",
        ) from error

    # [Week1] SMS에는 더 이상 직행 링크(토큰)를 담지 않는다. 로그인 후 대시보드에서
    # 확인하라고 안내한다.
    sms_message = (
        "[아이발견알림] 아이를 발견한 분과 실시간 채팅이 시작되었습니다. "
        "Re:Link에 로그인하여 대시보드에서 채팅방을 확인해 주세요."
    )
    # [버그 수정] Child.guardian relationship은 다대다 전환(Week3)으로 없어졌다
    # (Child.guardian_links를 통해 ChildGuardian -> Guardian으로 가야 한다).
    # [Week3] 이 아이에 연결된 보호자 "전원"에게 알린다 — 가족 여러 명이 함께
    # 관리하는 시나리오에서는 초대로 합류한 사람도 발견 사실을 알아야 하므로,
    # 최초 등록자 한 명에게만 보내는 건 이 기능의 취지와 맞지 않는다.
    try:
        guardian_phones = [link.guardian.phone for link in child.guardian_links]
    except SQLAlchemyError as error:
        # 연결된 보호자 조회 실패는 SMS 발송 실패로만 취급하고, 방 생성 자체는
        # 막지 않는다(SMS 발송 실패가 채팅방 생성을 막지 않는다는 기존 원칙과 동일).
        print(f"[경고] 보호자 목록 조회 실패(room_id={room.id}): {error}")
        guardian_phones = []

    for phone in guardian_phones:
        # SMS 발송 실패가 채팅방 생성 자체를 막지 않도록 결과만 확인하고 진행한다.
        sms_sent = sms.send_sms(phone, sms_message)
        if not sms_sent:
            # 목업에서는 항상 True지만, 실제 API 전환 시 실패 로깅 지점으로 사용한다.
            print(f"[경고] 보호자 SMS 발송 실패(room_id={room.id}). 재발송 로직이 필요합니다.")

    return StartChatResponse(
        room_id=room.id,
        chat_url=f"/chat/{room.id}?role=finder",
        ws_url=f"/ws/chat/{room.id}?role=finder",
    )


@app.websocket("/ws/chat/{room_id}")
async def chat_websocket(websocket: WebSocket, room_id: str, role: str = "finder") -> None:
    """발견자·보호자 실시간 채팅 WebSocket.

    - role 쿼리 파라미터(finder|guardian)를 서버가 신뢰의 기준으로 삼는다.
    - [Week1/GAP B] 보호자(role=guardian)는 더 이상 guardian_token이 아니라
      세션 쿠키로 인증한다. 쿠키의 세션이 가리키는 guardian_id가 이 방의
      아이에 연결돼(ChildGuardian) 있어야만 연결을 허용한다. [Week3] 아이 한
      명에 보호자가 여러 명 연결될 수 있게 되면서 단순 일치 비교가 목록 포함
      검사로 바뀌었다.
    - [GAP A] closed 상태이거나, 아이가 missing 상태가 아니면 연결을 거부한다.
    - 수신한 모든 메시지는 즉시 DB에 저장한 뒤 같은 방에 브로드캐스트한다.
    """
    # 1) 역할 검증. 유효하지 않으면 핸드셰이크 단계에서 거부한다.
    if role not in VALID_ROLES:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # 2) 방 존재/상태 검증. closed 이거나 없으면 거부.
    db = SessionLocal()
    try:
        try:
            room = db.query(ChatRoom).filter(ChatRoom.id == room_id).one_or_none()
            # GAP A 검증에 쓸 아이 상태를 같은 트랜잭션에서 함께 조회한다
            # (child 관계의 lazy-load도 DB 쿼리이므로 여기서 함께 감싼다).
            child_status = room.child.status if (room and room.child) else None
        except SQLAlchemyError:
            # DB 조회 실패 시 서버 내부 오류로 간주해 연결을 거부한다.
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return

        if room is None or room.status == "closed":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 2-1) [Week1/GAP B, Week3] 보호자는 세션 쿠키의 guardian_id가 이 방의
        # 아이에 연결돼 있어야만 입장 가능(다른 보호자의 방에 room_id로 무단 접근 방지).
        if role == "guardian":
            session_token = websocket.cookies.get(auth.SESSION_COOKIE_NAME)
            session_guardian_id = auth.get_session_guardian_id(session_token)
            if session_guardian_id is None:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            try:
                linked = _is_guardian_linked_to_child(db, session_guardian_id, room.child_id)
            except SQLAlchemyError:
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
                return
            if not linked:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

        # 2-2) [GAP A] 아이가 실종(missing) 상태일 때만 채팅 연결을 허용한다.
        # 보호자가 실종 신고를 해제(normal)하면 진행 중이던 WS 신규 연결도 거부된다.
        # (프론트에서만 막으면 우회되므로 WS 레벨에서 서버가 재검증한다.)
        if child_status != "missing":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 3) 연결 수락 및 방 등록.
        await manager.connect(room_id, websocket)

        # waiting 상태였다면 첫 접속 시 active로 전환한다.
        if room.status == "waiting":
            room.status = "active"
            try:
                db.commit()
            except SQLAlchemyError:
                db.rollback()  # 상태 전환 실패는 치명적이지 않으므로 롤백만 하고 진행.

        # 4) 신규 접속자에게 기존 대화 이력을 먼저 전송한다(재접속 시 이력 복원).
        #
        # [Week2] 발견자는 "자기가 이 방에 처음 들어온 시각" 이후의 메시지만 본다.
        # 발견자가 여러 명일 수 있는 구조(중복 방 재사용)에서, 2번 발견자가 1번
        # 발견자와 보호자의 이전 대화를 소급해 읽지 못하게 하기 위함이다.
        # 입장 시각은 채팅 화면(chat_page)에서 발급한 익명 쿠키로 식별한다.
        # 쿠키가 없으면(화면 안 거치고 WS 직접 접속 등) "지금부터"로 간주해
        # 이력을 아예 주지 않는다 — 더 보여주는 쪽이 아니라 덜 보여주는 쪽으로
        # 실패하게(fail-safe). 보호자는 항상 전체 이력을 본다.
        finder_joined_at = None
        finder_token = None
        if role == "finder":
            finder_token = websocket.cookies.get(auth.FINDER_COOKIE_NAME)
            finder_joined_at = auth.get_finder_joined_at(finder_token, room_id)
            if finder_joined_at is None:
                finder_joined_at = datetime.now(timezone.utc)

        try:
            history = (
                db.query(Message)
                .filter(Message.room_id == room_id)
                .order_by(Message.created_at)
                .all()
            )
            if finder_joined_at is not None:
                # SQLite가 tzinfo를 저장하지 않아 naive로 돌아오므로(auth.as_aware_utc
                # 주석 참고) DB 쿼리 대신 파이썬에서 보정 후 비교한다.
                history = [
                    m for m in history
                    if m.created_at is not None
                    and auth.as_aware_utc(m.created_at) >= finder_joined_at
                ]
            await websocket.send_json(
                {"type": "history", "messages": [_message_to_dict(m) for m in history]}
            )
        except SQLAlchemyError:
            # 이력 로드 실패는 실시간 채팅 자체를 막지 않는다. 이력만 생략.
            await websocket.send_json({"type": "history", "messages": []})

        # 4-1) [Week2] 발견자에게 현재 게이트 상태를 알려준다. 프론트가 이 값으로
        # 위치 공유 게이트를 보여줄지/입력창을 바로 열지 결정한다 — 이전에는
        # 프론트가 항상 게이트부터 보여줘서, 이미 위치를 공유한 발견자가
        # 새로고침하면 게이트가 다시 뜨는 문제가 있었다(서버가 알려주는 것으로 해결).
        finder_location_shared = False
        if role == "finder":
            finder_location_shared = auth.has_finder_shared_location(finder_token, room_id)
            await websocket.send_json(
                {"type": "gate", "location_shared": finder_location_shared}
            )

        # 5) 입장 시스템 메시지를 상대방에게 알린다.
        label = ROLE_LABELS.get(role, "상대방")
        await manager.broadcast(
            room_id,
            {"type": "system", "content": f"{label}가 입장했습니다."},
            exclude=websocket,
        )

        # 6) 수신 루프.
        await _receive_loop(
            websocket, db, room, role, finder_token, finder_location_shared
        )

    except WebSocketDisconnect:
        # 정상적인 연결 종료. 아래 finally에서 정리한다.
        pass
    finally:
        await manager.disconnect(room_id, websocket)
        label = ROLE_LABELS.get(role, "상대방")
        await manager.broadcast(
            room_id, {"type": "system", "content": f"{label}가 퇴장했습니다."}
        )
        db.close()


async def _receive_loop(
    websocket: WebSocket,
    db: Session,
    room: ChatRoom,
    role: str,
    finder_token: str | None = None,
    finder_location_shared: bool = False,
) -> None:
    """WebSocket 수신 루프: 메시지를 검증·저장·브로드캐스트한다.

    [GAP B] 발견자(finder)는 위치를 먼저 공유해야 텍스트 채팅이 열린다.
    [Week2 변경] 이 잠금은 방(room.location_shared) 단위가 아니라 **발견자
    단위**로 판정한다 — 발견자가 여러 명일 때 1번 발견자가 위치를 공유했다고
    2번 발견자까지 위치 없이 채팅이 열리면 안 되기 때문이다. 발견자별 상태는
    익명 쿠키(finder_token) 기준으로 auth의 발견자 세션에 기록되고, 쿠키가
    없는 연결은 이 연결 동안만 유효한 지역 변수로만 관리된다(재접속 시 다시
    공유해야 함, fail-safe). room.location_shared는 "이 방에서 위치가 한 번이라도
    공유됐는지"의 집계 기록으로만 남는다. 보호자(guardian)는 이 제약을 받지 않는다.

    WebSocketDisconnect는 호출부(chat_websocket)에서 처리하도록 전파한다.
    """
    room_id = room.id
    while True:
        # 잘못된 JSON이 오면 ValueError가 발생한다. 연결 종료 시 WebSocketDisconnect.
        try:
            payload = await websocket.receive_json()
        except ValueError:
            await websocket.send_json(
                {"type": "system", "content": "메시지 형식이 올바르지 않습니다(JSON 필요)."}
            )
            continue

        msg_type = payload.get("type", "text")

        # [Week2/GAP B 위치 거부 시 대체 경로] finder가 위치 공유를 거부하고
        # 112 신고 경로로 넘어갔음을 알리는 신호. 채팅 입력창은 열지 않고
        # (location_shared는 그대로 False), 보호자에게만 스캔 사실을 시스템
        # 메시지로 알린다(PLAN GAP B "위치 거부 시" 참고). DB에도 이력을 남긴다.
        if msg_type == "decline_location":
            if role != "finder":
                # 보호자는 위치 공유 대상이 아니므로 이 메시지를 보낼 이유가 없다.
                await websocket.send_json(
                    {"type": "system", "content": "지원하지 않는 요청입니다."}
                )
                continue

            decline_message = Message(
                room_id=room_id,
                sender_role="system",
                content="발견자가 위치 공유 없이 112 신고 경로로 안내받았습니다.",
                message_type="system",
            )
            try:
                db.add(decline_message)
                db.commit()
                db.refresh(decline_message)
            except SQLAlchemyError:
                db.rollback()
                await websocket.send_json(
                    {"type": "system", "content": "안내 기록 저장에 실패했습니다."}
                )
                continue

            # finder 본인에게는 112 안내로 전환됐음을 확인시키고, 보호자에게는
            # 같은 시스템 메시지를 브로드캐스트한다(스캔 사실만 공유, 위치 없음).
            await manager.broadcast(room_id, _message_to_dict(decline_message))
            continue

        # 메시지 타입에 따라 저장할 Message 객체를 구성한다.
        if msg_type == "text":
            # GAP B: finder는 "본인이" 위치를 공유하기 전에는 텍스트를 보낼 수 없다.
            # (다른 발견자가 공유했어도 이 발견자의 잠금은 안 풀린다 — 발견자 단위)
            if role == "finder" and not finder_location_shared:
                await websocket.send_json(
                    {
                        "type": "system",
                        "content": "먼저 위치를 공유해야 채팅을 시작할 수 있습니다.",
                    }
                )
                continue

            # [Week2] LLM 모더레이션으로 이전에 악용이 감지된 역할은 이후 text
            # 전송이 차단된다. 판정 자체는 비동기라 "이 메시지"가 아니라
            # "그 다음부터"만 막을 수 있으므로, 여기서 role별 제한 플래그를 확인한다.
            is_restricted = (
                room.finder_restricted if role == "finder" else room.guardian_restricted
            )
            if is_restricted:
                await websocket.send_json(
                    {
                        "type": "system",
                        "content": "안전을 위해 이 대화방에서의 메시지 전송이 제한되었습니다.",
                    }
                )
                continue

            content = (payload.get("content") or "").strip()
            if not content:
                continue  # 빈 메시지는 무시한다.
            message = Message(
                room_id=room_id,
                sender_role=role,  # 클라 입력 대신 서버가 아는 role을 신뢰한다.
                content=content,
                message_type="text",
            )
        elif msg_type == "location":
            coords = _parse_coordinates(payload)
            if coords is None:
                await websocket.send_json(
                    {"type": "system", "content": "위치 정보가 올바르지 않습니다."}
                )
                continue
            latitude, longitude, accuracy = coords
            # [Week3] 장소 검색(place_name)으로 지정한 위치는 GPS 오차 개념이 없으므로
            # accuracy가 안 실려온다(클라이언트가 보내지 않음). content에 장소 이름을
            # 담아 renderLocation이 "GPS 위치"와 구분해 표시할 수 있게 한다 — 위치
            # 메시지는 원래 content를 쓰지 않으므로(좌표로 표현) 이 필드를 재사용해도
            # 다른 로직과 충돌하지 않는다.
            place_name = str(payload.get("place_name", "")).strip()[:200]
            message = Message(
                room_id=room_id,
                sender_role=role,
                content=place_name,
                message_type="location",
                latitude=latitude,
                longitude=longitude,
                accuracy=accuracy,
            )
        else:
            await websocket.send_json(
                {"type": "system", "content": "지원하지 않는 메시지 형식입니다."}
            )
            continue

        try:
            db.add(message)
            # GAP B: finder가 위치를 처음 공유하면 "이 발견자의" 채팅 잠금을 해제한다.
            unlocked_now = False
            if msg_type == "location" and role == "finder":
                if not finder_location_shared:
                    finder_location_shared = True
                    unlocked_now = True
                # 쿠키가 있으면 재접속 후에도 잠금 해제가 유지되도록 세션에 기록.
                auth.mark_finder_location_shared(finder_token, room_id)
                # 집계 기록: 이 방에서 위치가 한 번이라도 공유됐는지(게이트 판정에는 안 씀).
                if not room.location_shared:
                    room.location_shared = True
            db.commit()
            db.refresh(message)
        except SQLAlchemyError:
            db.rollback()
            await websocket.send_json(
                {"type": "system", "content": "메시지 저장에 실패했습니다. 다시 시도해 주세요."}
            )
            continue

        # 저장에 성공한 메시지를 같은 방 전원(발신자 포함)에게 브로드캐스트한다.
        await manager.broadcast(room_id, _message_to_dict(message))

        # 위치 공유로 잠금이 막 풀렸다면 finder에게 채팅 가능 안내를 보낸다.
        if unlocked_now:
            await websocket.send_json(
                {"type": "system", "content": "위치가 공유되어 이제 채팅을 보낼 수 있습니다."}
            )

        # [Week2] LLM 모더레이션: 채팅 자체를 지연시키지 않기 위해 판정을
        # 백그라운드 태스크로 돌린다(fire-and-forget). 메시지는 이미 저장·전달
        # 됐으므로 이 태스크의 결과와 무관하게 채팅은 계속된다(PLAN 원칙 1).
        if msg_type == "text":
            asyncio.create_task(_run_moderation(message.id, room_id, role, content))


_MODERATION_CONTEXT_LIMIT = 5


async def _run_moderation(message_id: str, room_id: str, sender_role: str, content: str) -> None:
    """[Week2] 백그라운드에서 메시지를 모더레이션 검사하고, 위반 시 발신자를 제한한다.

    [Week3] 이번 메시지 한 줄만이 아니라, 같은 방의 최근 대화 몇 개를 함께
    LLM에 넘겨 맥락을 보고 판단하게 한다 — 개별 메시지는 평범해 보여도
    대화 흐름 전체를 보면 드러나는 패턴(서서히 개인정보를 캐내는 시도 등)을
    잡기 위함이다.

    asyncio.create_task로 fire-and-forget 실행되므로, 이 함수 안에서 발생하는
    모든 예외를 반드시 여기서 잡아야 한다 — 그렇지 않으면 예외가 아무에게도
    전달되지 못한 채 조용히 사라지거나(asyncio가 로그만 남김) 이벤트 루프의
    다른 처리에 영향을 줄 수 있다. moderation.check_message 자체는 이미
    fail-open(예외를 던지지 않음) 계약이지만, 그 이후의 DB 갱신·브로드캐스트
    단계에서 별도로 실패할 수 있으므로 이 함수 전체를 방어적으로 감싼다.

    새 DB 세션을 직접 연다 — 호출부(_receive_loop)의 세션은 WebSocket 연결과
    생명주기가 같아서 백그라운드 태스크가 오래 걸리는 동안 이미 닫혔을 수 있다.
    """
    db = SessionLocal()
    try:
        try:
            recent_messages = (
                db.query(Message)
                .filter(Message.room_id == room_id, Message.message_type == "text")
                .order_by(Message.created_at.desc())
                .limit(_MODERATION_CONTEXT_LIMIT + 1)  # 이번 메시지가 섞여 있을 수 있어 여유분.
                .all()
            )
        except SQLAlchemyError as error:
            # 맥락 조회는 부가 기능이므로 실패해도 판정 자체(맥락 없이)는 계속 진행한다.
            print(f"[모더레이션] 최근 대화 조회 실패(message_id={message_id}): {error}")
            recent_messages = []

        # 이번에 판정할 메시지 자신은 맥락에서 빼고, 오래된 → 최신 순으로 정렬한다.
        history = [
            {"role": msg.sender_role, "content": msg.content}
            for msg in reversed(recent_messages)
            if msg.id != message_id
        ][-_MODERATION_CONTEXT_LIMIT:]

        try:
            # openai 클라이언트(Upstage Solar 호환) 호출은 동기(blocking) API이므로,
            # 이벤트 루프를 막지 않도록 별도 스레드에서 실행한다(asyncio.to_thread).
            violates = await asyncio.to_thread(moderation.check_message, content, history)
        except Exception as error:  # noqa: BLE001
            # moderation.check_message는 자체적으로 fail-open이라 예외를 던지지
            # 않는 게 정상이지만, asyncio.to_thread 자체의 스케줄링 실패 등
            # 예견 못한 상황까지 대비해 최후의 방어선으로만 넓게 잡는다. 이 태스크는
            # 완전히 백그라운드이고 채팅 흐름에 영향을 주면 안 되므로(PLAN fail-open
            # 원칙), 여기서 삼키지 않으면 서버 로그에 미수집 예외로만 남고 아무도
            # 처리할 수 없다.
            print(f"[모더레이션] 백그라운드 판정 중 예상치 못한 오류(message_id={message_id}): {error}")
            return

        if not violates:
            return

        try:
            message = db.query(Message).filter(Message.id == message_id).one_or_none()
            room = db.query(ChatRoom).filter(ChatRoom.id == room_id).one_or_none()
        except SQLAlchemyError as error:
            print(f"[모더레이션] 위반 처리 중 조회 실패(message_id={message_id}): {error}")
            return

        if message is not None:
            message.flagged = True
        if room is not None:
            if sender_role == "finder":
                room.finder_restricted = True
            elif sender_role == "guardian":
                room.guardian_restricted = True

        try:
            db.commit()
        except SQLAlchemyError as error:
            db.rollback()
            print(f"[모더레이션] 위반 처리 중 저장 실패(message_id={message_id}): {error}")
            return
    finally:
        db.close()

    # 양쪽 모두에게 제한 사실을 시스템 메시지로 안내한다(어떤 메시지가
    # 위반이었는지 내용은 노출하지 않고, 제한이 걸렸다는 사실만 알린다).
    label = ROLE_LABELS.get(sender_role, "상대방")
    await manager.broadcast(
        room_id,
        {
            "type": "system",
            "content": f"{label}의 메시지에서 안전 문제가 감지되어 이후 전송이 제한되었습니다.",
        },
    )


def _parse_coordinates(payload: dict) -> tuple[float, float, float | None] | None:
    """위치 메시지 payload에서 위도/경도/오차반경을 검증해 추출한다.

    [Week2] accuracy(오차 반경, 미터)는 브라우저 Geolocation API가 항상 주는
    값이지만, 클라이언트 구버전 호환을 위해 선택 필드로 취급한다 — 없으면
    None으로 저장하고 latitude/longitude 검증은 그대로 진행한다.

    Returns:
        (latitude, longitude, accuracy) 튜플. 좌표가 없거나 범위를 벗어나면 None.
        accuracy는 없거나 음수/숫자가 아니면 None으로 대체된다(좌표 자체는 유효).
    """
    try:
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
    except (TypeError, ValueError):
        # 좌표가 누락됐거나 숫자로 변환할 수 없는 경우.
        return None

    # 지구 좌표 유효 범위 검증.
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return None

    accuracy: float | None
    try:
        accuracy = float(payload.get("accuracy"))
        if accuracy < 0:
            accuracy = None
    except (TypeError, ValueError):
        # accuracy 누락/비숫자는 좌표 자체를 무효화하지 않는다.
        accuracy = None

    return latitude, longitude, accuracy


# ---------------------------------------------------------------------------
# [Week1] QR 시리얼 claim 공용 헬퍼 (회원가입 / 대시보드 "아이 추가"에서 공통 사용)
# ---------------------------------------------------------------------------


def _claim_qr_token(db: Session, serial: str, guardian_id: str, child_name: str) -> Child:
    """[Week1] 시리얼 번호로 사전 발급된 QrToken을 조회해 새 Child에 연결(claim)한다.

    PLAN "QR 사전 인쇄 & 시리얼 매칭" 흐름의 3단계(등록/매칭)에 해당한다.
    [Week3] Child 생성과 동시에 ChildGuardian(role="primary") 연결 레코드도 만든다 —
    "누가 이 아이를 볼 수 있는가"는 이제 이 테이블로만 판정하므로, Child를 만들면서
    이 레코드를 빠뜨리면 방금 등록한 사람조차 자기 아이 방에 못 들어가게 된다.
    호출부에서 commit/rollback을 책임진다(여기서는 add/flush만 수행).

    Raises:
        LookupError: 시리얼이 존재하지 않을 때(호출부에서 404로 변환).
        ValueError: 이미 다른 아이에게 연결된(사용된) 시리얼일 때(호출부에서 409로 변환).
        SQLAlchemyError: 조회/삽입 중 DB 오류(호출부에서 500으로 변환).
    """
    qr_entry = db.query(QrToken).filter(QrToken.serial == serial).one_or_none()
    if qr_entry is None:
        raise LookupError("유효하지 않은 시리얼 번호입니다.")
    if qr_entry.child_id is not None:
        raise ValueError("이미 등록된 QR입니다.")

    child = Child(
        name=child_name,
        primary_guardian_id=guardian_id,
        status="normal",
        qr_token=qr_entry.qr_token,  # Child.qr_token에 QrToken.qr_token 값을 그대로 복사.
    )
    db.add(child)
    db.flush()  # child.id를 확보하기 위해 flush.

    db.add(ChildGuardian(child_id=child.id, guardian_id=guardian_id, role="primary"))

    qr_entry.child_id = child.id
    qr_entry.claimed_at = datetime.now(timezone.utc)
    return child


def _is_guardian_linked_to_child(db: Session, guardian_id: str, child_id: str) -> bool:
    """[Week3] 이 보호자가 이 아이에 연결(최초 등록 또는 초대로 합류)돼 있는지 조회한다.

    채팅 열람/실종 신고 토글/QR 다운로드 등 "볼 수 있는가" 판정은 전부 이 함수를
    거친다. 예전의 child.guardian_id == guardian.id 단순 비교를 대체한다.

    Raises:
        SQLAlchemyError: 조회 중 DB 오류(호출부에서 처리).
    """
    link = (
        db.query(ChildGuardian)
        .filter(ChildGuardian.child_id == child_id, ChildGuardian.guardian_id == guardian_id)
        .one_or_none()
    )
    return link is not None


def _is_guardian_primary_for_child(db: Session, guardian_id: str, child_id: str) -> bool:
    """[Week3] 이 보호자가 이 아이의 최초 등록자(role="primary")인지 조회한다.

    아이 삭제, 초대 코드 발급처럼 민감한 조작은 이 함수가 True를 반환할 때만
    허용한다 — 초대로 합류한 보호자(role="invited")는 여기서 False가 나온다.

    Raises:
        SQLAlchemyError: 조회 중 DB 오류(호출부에서 처리).
    """
    link = (
        db.query(ChildGuardian)
        .filter(
            ChildGuardian.child_id == child_id,
            ChildGuardian.guardian_id == guardian_id,
            ChildGuardian.role == "primary",
        )
        .one_or_none()
    )
    return link is not None


# ---------------------------------------------------------------------------
# [Week1] 보호자 회원가입 / 로그인 / 로그아웃
# ---------------------------------------------------------------------------


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, error: str | None = None) -> HTMLResponse:
    """[Week1] 보호자 회원가입 폼(GET). error 쿼리 파라미터로 실패 사유를 표시한다."""
    return templates.TemplateResponse(request, "register.html", {"error": error})


@app.post("/register")
async def register_submit(request: Request, db: Session = Depends(get_db)):
    """[Week1] 보호자 회원가입 처리: Guardian 신규 생성.

    [Week3] QR(태그) 등록은 더 이상 회원가입과 한 번에 처리하지 않는다. 계정만
    먼저 만들고, 로그인 후 대시보드의 "태그 추가"(POST /guardian/dashboard/children,
    guardian_add_child)에서 시리얼을 claim하도록 분리했다 — 가입 자체는 QR 없이도
    끝날 수 있어야 한다는 결정에 따른 변경.

    PLAN 변경사항 반영:
    - 전화번호가 이미 가입되어 있으면 기존 계정에 자동으로 붙이지 않고 명확히
      실패 처리한다("이미 가입된 번호입니다, 로그인해주세요") — 예전 방식(전화번호
      매칭 재사용)이 갖고 있던 "제3자가 남의 번호로 아이를 몰래 추가할 수 있는"
      보안 문제를 없애기 위한 의도적 변경이다. (계정 열거 여지는 PLAN이 감수하기로
      한 트레이드오프.)
    - PIN은 평문으로 저장하지 않고 auth.hash_pin으로 해싱해 저장한다.
    - [Week3] 개인정보 수집·이용 동의(전화번호 수집, AI의 채팅 내용 분석 등)에
      체크하지 않으면 가입 자체를 진행하지 않는다(서버에서도 재검증 — 체크박스는
      프론트 UI일 뿐이라 클라이언트 조작으로 우회될 수 있으므로).

    Raises:
        HTTPException(500): DB 오류 시.
    """
    form = await request.form()
    guardian_name = str(form.get("guardian_name", "")).strip()
    # [Week3 버그 수정] 하이픈 포함/미포함 입력이 서로 다른 계정으로 취급되지
    # 않도록 정규화해서 저장한다(auth.normalize_phone 주석 참고).
    guardian_phone = auth.normalize_phone(str(form.get("guardian_phone", "")).strip())
    guardian_pin = str(form.get("guardian_pin", "")).strip()
    privacy_consent = str(form.get("privacy_consent", "")).strip()

    if not guardian_name or not guardian_phone:
        return RedirectResponse(url="/register?error=missing_fields", status_code=303)

    if not privacy_consent:
        return RedirectResponse(url="/register?error=consent_required", status_code=303)

    # [Week1] PIN 정책(6자리 이상 숫자) 검증. 형식이 틀리면 DB에 손대지 않고 즉시 거부.
    if not auth.validate_pin_format(guardian_pin):
        return RedirectResponse(url="/register?error=invalid_pin", status_code=303)

    try:
        existing = db.query(Guardian).filter(Guardian.phone == guardian_phone).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"가입 처리 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    if existing is not None:
        # [Week1] 의도적 트레이드오프(PLAN 명시): 이미 가입된 번호임을 알려준다.
        return RedirectResponse(url="/register?error=phone_taken", status_code=303)

    guardian = Guardian(
        phone=guardian_phone,
        name=guardian_name,
        pin_hash=auth.hash_pin(guardian_pin),
    )
    try:
        db.add(guardian)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"가입 처리 중 충돌이 발생했습니다: {error}"
        ) from error
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"가입 처리 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    # 가입 성공 - 곧바로 로그인 상태로 대시보드에 진입시킨다. 태그(QR) 등록은
    # 대시보드의 "태그 추가" 폼에서 진행한다.
    session_token = auth.create_session(guardian.id)
    response = RedirectResponse(url="/guardian/dashboard", status_code=303)
    _set_session_cookie(response, session_token)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    error: str | None = None,
    minutes: int | None = None,
    reset: str | None = None,
) -> HTMLResponse:
    """[Week1] 보호자 로그인 폼(GET). 잠금 상태면 minutes로 남은 시간을 안내한다.

    [Week2] reset="success"면 PIN 재설정이 막 완료됐다는 안내를 함께 보여준다
    (reset_pin_submit이 /login?reset=success로 리다이렉트).
    """
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "minutes": minutes, "reset": reset}
    )


@app.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    """[Week1] 전화번호+PIN 로그인.

    PLAN "로그인 시도 제한": 5회 연속 실패 시 해당 전화번호 계정을 15분간 잠근다.
    존재하지 않는 전화번호와 틀린 PIN을 구분하지 않고 동일한 오류 메시지를
    반환한다(로그인 단계에서의 계정 열거 방지 — 가입 단계와는 다른 정책).

    Raises:
        HTTPException(500): DB 오류 시.
    """
    form = await request.form()
    guardian_phone = auth.normalize_phone(str(form.get("guardian_phone", "")).strip())
    guardian_pin = str(form.get("guardian_pin", "")).strip()

    try:
        guardian = db.query(Guardian).filter(Guardian.phone == guardian_phone).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"로그인 처리 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    if guardian is None:
        # 존재하지 않는 번호도 "PIN이 틀렸다"와 동일한 메시지로 응답(계정 열거 방지).
        return RedirectResponse(url="/login?error=invalid_credentials", status_code=303)

    if auth.is_locked(guardian.locked_until):
        # [버그 수정] SQLite에서 읽은 locked_until은 naive datetime이라(auth.py
        # _as_aware_utc 주석 참고) datetime.now(timezone.utc)와 직접 빼면
        # TypeError가 난다. auth.remaining_lock_seconds가 aware 보정까지 함께 처리한다.
        remaining_seconds = auth.remaining_lock_seconds(guardian.locked_until)
        remaining_minutes = max(1, int(remaining_seconds // 60) + 1)
        return RedirectResponse(
            url=f"/login?error=locked&minutes={remaining_minutes}", status_code=303
        )

    if not auth.verify_pin(guardian_pin, guardian.pin_hash):
        # [Week1] 실패 카운트 증가 + 임계치 도달 시 잠금(auth.compute_lock_until).
        guardian.failed_login_count += 1
        lock_until = auth.compute_lock_until(guardian.failed_login_count)
        if lock_until is not None:
            guardian.locked_until = lock_until
        try:
            db.commit()
        except SQLAlchemyError as error:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"로그인 처리 중 데이터베이스 오류가 발생했습니다: {error}"
            ) from error
        return RedirectResponse(url="/login?error=invalid_credentials", status_code=303)

    # 로그인 성공 - 실패 카운터/잠금을 초기화한다.
    guardian.failed_login_count = 0
    guardian.locked_until = None
    try:
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"로그인 처리 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    session_token = auth.create_session(guardian.id)
    response = RedirectResponse(url="/guardian/dashboard", status_code=303)
    _set_session_cookie(response, session_token)
    return response


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    """[Week1] 로그아웃: 세션 폐기 + 쿠키 삭제."""
    token = request.cookies.get(auth.SESSION_COOKIE_NAME)
    auth.delete_session(token)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# [Week2] PIN 재설정(OTP) 흐름
#
# PLAN.md "PIN 재설정": 로그인 화면 "PIN을 잊으셨나요?" -> 전화번호 입력 ->
# SMS OTP 발송/확인 -> PIN 재설정. 아이디(전화번호) 찾기는 스코프 밖(README/Q&A로만).
# OTP 발송 자체의 rate limit(60초 쿨다운, 1시간 5회)과 만료(5분)/1회용 제약은
# auth.py(can_request_otp/issue_otp/verify_otp)에 있다. 여기서는 그 결과에 따라
# 사용자에게 어떤 화면/에러를 보여줄지만 판단한다.
# ---------------------------------------------------------------------------


@app.get("/login/forgot-pin", response_class=HTMLResponse)
def forgot_pin_page(request: Request, error: str | None = None, sent: bool = False) -> HTMLResponse:
    """[Week2] PIN을 잊은 보호자가 전화번호를 입력해 OTP 발송을 요청하는 화면."""
    return templates.TemplateResponse(
        request, "forgot_pin.html", {"error": error, "sent": sent}
    )


@app.post("/login/forgot-pin")
async def forgot_pin_submit(request: Request, db: Session = Depends(get_db)):
    """[Week2] 전화번호로 OTP를 발송한다(목업 SMS로 콘솔에 출력).

    등록되지 않은 번호여도 발송 성공과 동일한 화면을 보여준다(계정 열거 방지 —
    /login과 동일한 정책). rate limit에 걸리면 그 사실만 안내한다.

    Raises:
        HTTPException(500): DB 오류 시.
    """
    form = await request.form()
    guardian_phone = auth.normalize_phone(str(form.get("guardian_phone", "")).strip())

    if not guardian_phone:
        return RedirectResponse(url="/login/forgot-pin?error=missing_phone", status_code=303)

    try:
        guardian = db.query(Guardian).filter(Guardian.phone == guardian_phone).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"OTP 발송 처리 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    allowed, wait_seconds = auth.can_request_otp(guardian_phone)
    if not allowed:
        if wait_seconds is not None:
            return RedirectResponse(
                url=f"/login/forgot-pin?error=cooldown&wait={wait_seconds}", status_code=303
            )
        return RedirectResponse(url="/login/forgot-pin?error=too_many", status_code=303)

    # 존재하지 않는 번호라도 OTP 발송 자체는 조용히 건너뛴다(계정 열거 방지).
    # 다만 rate limit 판정은 이미 위에서 끝냈으므로, 등록 안 된 번호로도 rate
    # limit을 소모시키지 않도록 실제 발급은 guardian이 있을 때만 수행한다.
    if guardian is not None:
        code = auth.issue_otp(guardian_phone)
        sms_message = f"[Re:Link PIN 재설정] 인증번호는 {code} 입니다. 5분 이내에 입력해 주세요."
        sms.send_sms(guardian_phone, sms_message)

    return RedirectResponse(url="/login/forgot-pin?sent=true", status_code=303)


@app.get("/login/reset-pin", response_class=HTMLResponse)
def reset_pin_page(
    request: Request, phone: str = "", error: str | None = None
) -> HTMLResponse:
    """[Week2] OTP + 새 PIN을 입력해 실제로 PIN을 재설정하는 화면."""
    return templates.TemplateResponse(
        request, "reset_pin.html", {"phone": phone, "error": error}
    )


@app.post("/login/reset-pin")
async def reset_pin_submit(request: Request, db: Session = Depends(get_db)):
    """[Week2] OTP를 검증하고 통과하면 새 PIN으로 교체한다.

    OTP는 auth.verify_otp 호출 자체로 소모된다(성공/실패 불문 1회용) — 재시도가
    필요하면 /login/forgot-pin에서 새 OTP를 다시 받아야 한다.

    Raises:
        HTTPException(500): DB 오류 시.
    """
    form = await request.form()
    guardian_phone = auth.normalize_phone(str(form.get("guardian_phone", "")).strip())
    otp_code = str(form.get("otp_code", "")).strip()
    new_pin = str(form.get("new_pin", "")).strip()

    if not guardian_phone or not otp_code or not new_pin:
        return RedirectResponse(
            url=f"/login/reset-pin?phone={guardian_phone}&error=missing_fields", status_code=303
        )

    if not auth.validate_pin_format(new_pin):
        return RedirectResponse(
            url=f"/login/reset-pin?phone={guardian_phone}&error=invalid_pin", status_code=303
        )

    if not auth.verify_otp(guardian_phone, otp_code):
        return RedirectResponse(
            url=f"/login/reset-pin?phone={guardian_phone}&error=invalid_otp", status_code=303
        )

    try:
        guardian = db.query(Guardian).filter(Guardian.phone == guardian_phone).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"PIN 재설정 처리 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    if guardian is None:
        # OTP 검증까지 통과했는데 계정이 없는 것은 비정상 상황(예: 가입 안 된
        # 번호로 억지로 OTP를 발급받으려 한 경우는 issue_otp 단계에서 이미
        # 막혔으므로 거의 발생하지 않는다). 안전하게 실패로 처리한다.
        return RedirectResponse(url="/login?error=invalid_credentials", status_code=303)

    guardian.pin_hash = auth.hash_pin(new_pin)
    # PIN 재설정 성공 시 로그인 잠금도 함께 해제한다(비밀번호를 바꿨으니 재시도 기회 부여).
    guardian.failed_login_count = 0
    guardian.locked_until = None
    try:
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"PIN 재설정 처리 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    return RedirectResponse(url="/login?reset=success", status_code=303)


# ---------------------------------------------------------------------------
# [Week1] 보호자 대시보드 (세션 기반 라우팅, URL에 토큰 없음)
# ---------------------------------------------------------------------------


@app.get("/guardian/dashboard", response_class=HTMLResponse)
def guardian_dashboard(
    request: Request,
    error: str | None = None,
    invite_code: str | None = None,
    join_error: str | None = None,
    joined: str | None = None,
    db: Session = Depends(get_db),
):
    """[Week1] 보호자 대시보드: 로그인한 보호자의 아이 목록 + 진행 중인 채팅방.

    로그인하지 않았으면 /login으로 리다이렉트한다. 예전의 /guardian/{manage_token}
    방식과 달리 URL 자체에는 어떤 식별 정보도 담지 않는다(세션 쿠키가 유일한
    접근 수단).

    [Week3] invite_code: 방금 발급된 초대 코드(guardian_create_invite가 리다이렉트로
    전달). join_error/joined: 초대 코드 입력(guardian_join_by_invite) 결과 안내.

    Raises:
        HTTPException(500): DB 오류 시.
    """
    guardian = get_current_guardian(request, db)
    if guardian is None:
        return RedirectResponse(url="/login", status_code=303)

    try:
        # [Week3] "이 보호자가 소유한 아이"가 아니라 "이 보호자가 연결된 아이"를
        # 조회한다 — 초대로 합류한 아이도 대시보드에 똑같이 보여야 하므로
        # ChildGuardian을 조인한다.
        children = (
            db.query(Child)
            .join(ChildGuardian, ChildGuardian.child_id == Child.id)
            .filter(ChildGuardian.guardian_id == guardian.id)
            .order_by(Child.created_at)
            .all()
        )
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"아이 목록 조회 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    # [Week1] 아이별로 "진행 중인 채팅방"(closed가 아닌 방)이 있는지 조회한다.
    # PLAN: "아이가 missing 상태에서 발견 신고가 들어오면 대시보드에 바로 노출".
    children_display = []
    for child in children:
        try:
            active_room = (
                db.query(ChatRoom)
                .filter(ChatRoom.child_id == child.id, ChatRoom.status != "closed")
                .order_by(ChatRoom.created_at.desc())
                .first()
            )
        except SQLAlchemyError as error:
            # 진행 중인 방 표시는 부가 기능이므로, 조회 실패 시 전체 페이지를
            # 500으로 죽이지 않고 "표시 안 함"으로 완화한다(콘솔에는 남긴다).
            print(f"[경고] 진행 중인 채팅방 조회 실패(child_id={child.id}): {error}")
            active_room = None
        children_display.append(
            {
                "id": child.id,
                "name": child.name,
                "status": child.status,
                "qr_token": child.qr_token,
                "serial": (
                    child.qr_pool_entry.serial
                    if child.qr_pool_entry is not None
                    else child.qr_token[:12]
                ),
                "active_room_id": active_room.id if active_room else None,
                # [Week3] 프론트가 "초대하기"/"삭제" 버튼을 최초 등록자에게만
                # 보여줄 수 있도록, 이 보호자가 primary인지 함께 내려준다.
                "is_primary": child.primary_guardian_id == guardian.id,
            }
        )

    return templates.TemplateResponse(
        request,
        "guardian_dashboard.html",
        {
            "guardian_name": guardian.name,
            "children": children_display,
            "error": error,
            "invite_code": invite_code,
            "join_error": join_error,
            "joined": joined,
        },
    )


@app.get("/guardian/dashboard/active-rooms")
def guardian_dashboard_active_rooms(request: Request, db: Session = Depends(get_db)) -> dict:
    """[버그 수정] 발견자가 신고를 시작해도 이미 열려 있는 대시보드에는 "채팅
    열기" 버튼이 새로고침 전까지 안 보이는 문제(박선우 리포트) 대응용 폴링
    엔드포인트. guardian_dashboard.html의 active_room_id 계산 로직과 동일하며,
    dashboard_poll.js가 몇 초 간격으로 이 값만 가볍게 물어봐서 버튼을 갱신한다.

    풀 페이지 렌더링(guardian_dashboard) 대신 별도 JSON 엔드포인트로 둔 이유:
    폴링마다 전체 HTML을 다시 그리면 낭비이기도 하고, 서버 렌더 결과를 다시
    파싱해서 필요한 부분만 갈아끼우는 것보다 필요한 데이터만 받아 클라이언트가
    직접 DOM을 갱신하는 편이 더 간단하다.

    Raises:
        HTTPException(401): 로그인하지 않았을 때.
        HTTPException(500): DB 오류 시.
    """
    guardian = get_current_guardian(request, db)
    if guardian is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    try:
        children = (
            db.query(Child)
            .join(ChildGuardian, ChildGuardian.child_id == Child.id)
            .filter(ChildGuardian.guardian_id == guardian.id)
            .all()
        )
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"아이 목록 조회 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    result = {}
    for child in children:
        if child.status != "missing":
            continue
        try:
            active_room = (
                db.query(ChatRoom)
                .filter(ChatRoom.child_id == child.id, ChatRoom.status != "closed")
                .order_by(ChatRoom.created_at.desc())
                .first()
            )
        except SQLAlchemyError as error:
            print(f"[경고] 진행 중인 채팅방 폴링 조회 실패(child_id={child.id}): {error}")
            continue
        result[child.id] = active_room.id if active_room else None

    return {"active_rooms": result}


@app.post("/guardian/dashboard/children")
async def guardian_add_child(request: Request, db: Session = Depends(get_db)):
    """[Week1] 로그인한 보호자가 새 아이를 시리얼로 claim하여 추가한다.

    PLAN 변경사항: 전화번호 자동 매칭이 아니라 반드시 로그인 세션 기준으로만
    아이가 추가된다 — 남의 전화번호를 알아도 그 계정에 아이를 못 붙인다.

    Raises:
        HTTPException(500): DB 오류 시.
    """
    guardian = get_current_guardian(request, db)
    if guardian is None:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    child_name = str(form.get("child_name", "")).strip()
    serial = str(form.get("serial", "")).strip()

    if not child_name or not serial:
        return RedirectResponse(url="/guardian/dashboard?error=missing_fields", status_code=303)

    try:
        _claim_qr_token(db, serial, guardian.id, child_name)
        db.commit()
    except LookupError:
        db.rollback()
        return RedirectResponse(url="/guardian/dashboard?error=invalid_serial", status_code=303)
    except ValueError:
        db.rollback()
        return RedirectResponse(url="/guardian/dashboard?error=serial_used", status_code=303)
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"아이 추가 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    return RedirectResponse(url="/guardian/dashboard", status_code=303)


@app.post("/guardian/dashboard/children/{child_id}/toggle")
def guardian_toggle_status(request: Request, child_id: str, db: Session = Depends(get_db)):
    """[Week1/GAP A] 아이의 실종 신고 상태(normal/missing)를 토글한다.

    [Week1/GAP B, Week3] 연결 재검증: 이 아이에 연결된 보호자 목록에 로그인한
    보호자가 없으면 403 — 다른 보호자가 child_id를 추측해서 남의 아이 상태를
    바꾸지 못하도록 막는다. 실종 신고 토글은 최초 등록자뿐 아니라 초대로 합류한
    보호자도 할 수 있다(가족 구성원 누구든 위급 상황에 신고를 켤 수 있어야
    하므로 — 아이 삭제 같은 민감 작업과는 다르게 취급한다).

    Raises:
        HTTPException(404): 존재하지 않는 아이일 때.
        HTTPException(403): 이 아이에 연결되지 않은 보호자일 때.
        HTTPException(500): DB 오류 시.
    """
    guardian = get_current_guardian(request, db)
    if guardian is None:
        return RedirectResponse(url="/login", status_code=303)

    try:
        child = db.query(Child).filter(Child.id == child_id).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"아이 조회 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    if child is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 아이입니다.")

    try:
        linked = _is_guardian_linked_to_child(db, guardian.id, child.id)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"권한 확인 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error
    if not linked:
        raise HTTPException(status_code=403, detail="본인이 연결된 아이만 관리할 수 있습니다.")

    child.status = "normal" if child.status == "missing" else "missing"
    try:
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"상태 변경 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    return RedirectResponse(url="/guardian/dashboard", status_code=303)


@app.post("/guardian/dashboard/children/{child_id}/delete")
def guardian_delete_child(request: Request, child_id: str, db: Session = Depends(get_db)):
    """[Week1/GAP B] 아이 삭제. 소유권 재검증 후 QrToken을 unclaim하고 Child를 삭제한다.

    [Week3] 삭제는 민감한 조작이라 이 아이의 최초 등록자(role="primary")만 할 수
    있다 — 초대로 합류한 보호자(role="invited")가 실수나 다툼으로 아이 데이터를
    통째로 지워버리는 사고를 막기 위한 의도적 제약이다(다른 조작인 실종 신고
    토글/채팅 열람은 연결된 모든 보호자가 할 수 있는 것과 대비된다).

    Raises:
        HTTPException(404): 존재하지 않는 아이일 때.
        HTTPException(403): 최초 등록자가 아닐 때.
        HTTPException(500): DB 오류 시.
    """
    guardian = get_current_guardian(request, db)
    if guardian is None:
        return RedirectResponse(url="/login", status_code=303)

    try:
        child = db.query(Child).filter(Child.id == child_id).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"아이 조회 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    if child is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 아이입니다.")

    try:
        is_primary = _is_guardian_primary_for_child(db, guardian.id, child.id)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"권한 확인 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error
    if not is_primary:
        raise HTTPException(status_code=403, detail="이 아이를 최초 등록한 보호자만 삭제할 수 있습니다.")

    try:
        if child.qr_pool_entry is not None:
            # 시리얼을 다시 미사용 상태로 되돌린다(QrToken이 삭제된 Child를 참조하는
            # 고아 FK가 남지 않도록). 스티커 자체는 재사용하지 않을 계획이지만,
            # 데이터 일관성을 위해 명시적으로 초기화한다.
            child.qr_pool_entry.child_id = None
            child.qr_pool_entry.claimed_at = None
        db.delete(child)
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"아이 삭제 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    return RedirectResponse(url="/guardian/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# [Week3] 가족 초대: 아이 한 명에 보호자를 추가로 연결한다.
#
# QrToken의 "짧은 코드로 매칭" 패턴을 그대로 재사용한다(팀이 이미 익숙한 구조).
# 발급은 최초 등록자만 할 수 있고, 수락은 로그인한 아무 보호자나 코드만 알면
# 할 수 있다(코드 자체가 문자/카톡으로 직접 전달되는 비밀값이라, 그 값을 아는
# 것 자체가 "초대받았다"는 증거로 취급된다 — QrToken의 시리얼과 같은 신뢰 모델).
# ---------------------------------------------------------------------------


@app.post("/guardian/dashboard/children/{child_id}/invite")
def guardian_create_invite(request: Request, child_id: str, db: Session = Depends(get_db)):
    """[Week3] 이 아이에 보호자를 추가로 연결하기 위한 1회용 초대 코드를 발급한다.

    최초 등록자(role="primary")만 발급할 수 있다 — 초대로 합류한 보호자가 또
    다른 사람을 무한정 끌어들이는 것을 막기 위한 제약이다(민감 작업 정책은
    guardian_delete_child와 동일하게 primary 전용).

    발급된 코드는 리다이렉트 쿼리 파라미터(?invite_code=...)로 대시보드에
    전달한다 — 이 코드는 평문으로 다시 볼 수 없으므로(DB에는 원문 그대로
    저장하지만 "재발급하면 이전 코드는 그대로 살아있다"는 걸 프론트가 알
    필요는 없고, 그냥 이번에 발급된 값을 1회 보여주면 된다) 이 응답에서만
    보여준다.

    Raises:
        HTTPException(404): 존재하지 않는 아이일 때.
        HTTPException(403): 최초 등록자가 아닐 때.
        HTTPException(500): DB 오류 시.
    """
    guardian = get_current_guardian(request, db)
    if guardian is None:
        return RedirectResponse(url="/login", status_code=303)

    try:
        child = db.query(Child).filter(Child.id == child_id).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"아이 조회 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    if child is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 아이입니다.")

    try:
        is_primary = _is_guardian_primary_for_child(db, guardian.id, child.id)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"권한 확인 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error
    if not is_primary:
        raise HTTPException(
            status_code=403, detail="이 아이를 최초 등록한 보호자만 초대 코드를 발급할 수 있습니다."
        )

    # [Week3] QrToken의 유일 시리얼 생성과 동일한 재시도 패턴 — 충돌은 극히
    # 드물지만(8자리, 32문자 알파벳), DB 레벨 unique 제약과 함께 애플리케이션
    # 레벨에서도 몇 번 재시도한다.
    invite = None
    last_integrity_error: IntegrityError | None = None
    for _ in range(5):
        code = auth.generate_invite_code()
        invite = InviteCode(
            code=code,
            child_id=child.id,
            created_by_guardian_id=guardian.id,
            expires_at=auth.compute_invite_code_expiry(),
        )
        try:
            db.add(invite)
            db.commit()
            break
        except IntegrityError as error:
            db.rollback()
            last_integrity_error = error
            invite = None
            continue
        except SQLAlchemyError as error:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"초대 코드 발급 중 데이터베이스 오류가 발생했습니다: {error}"
            ) from error

    if invite is None:
        raise HTTPException(
            status_code=500,
            detail=f"초대 코드 생성 중 충돌이 반복되었습니다. 다시 시도해 주세요: {last_integrity_error}",
        )

    return RedirectResponse(
        url=f"/guardian/dashboard?invite_code={invite.code}", status_code=303
    )


@app.post("/guardian/dashboard/join")
async def guardian_join_by_invite(request: Request, db: Session = Depends(get_db)):
    """[Week3] 로그인한 보호자가 초대 코드를 입력해 아이에 연결(합류)한다.

    코드는 검증 시도 자체로 소모되지 않는다(만료 전까지는 재시도 가능) — OTP와
    달리 이 코드는 "그 자리에서 한 번 틀렸다고 폐기"할 이유가 없다(오타 가능성이
    높은 8자리 수동 입력값이므로). 다만 실제로 사용(합류 성공)하면 즉시
    used_at/used_by_guardian_id가 채워져 그 순간부터 재사용이 막힌다.

    [Week3 동시성 수정] "코드 조회 -> 파이썬에서 유효성 확인 -> used_at 대입 ->
    커밋" 순서로 짜면, 서버가 여러 프로세스/인스턴스로 돌아갈 때(이중화 배포)
    서로 다른 두 보호자의 요청이 동시에 같은 코드를 "아직 안 쓴 코드"로 읽어버려
    둘 다 합류에 성공하는 경쟁 상태가 생긴다(1회용이라는 전제가 깨짐). 단일
    프로세스 + await 없는 동기 코드에서는 이벤트 루프가 우연히 직렬화해줘서
    드러나지 않지만, 실제 이중화 배포에서는 재현 가능하다. 그래서 실제 "코드를
    선점하는" 연산만은 `UPDATE ... WHERE used_at IS NULL` 조건부 갱신으로 처리한다
    — 이 한 문장이 원자적이라, 두 요청이 동시에 와도 DB가 행 잠금으로 순서를
    정해주고 오직 하나만 rowcount=1을 받는다.

    Raises:
        HTTPException(500): DB 오류 시.
    """
    guardian = get_current_guardian(request, db)
    if guardian is None:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    code = str(form.get("code", "")).strip().upper()

    if not code:
        return RedirectResponse(url="/guardian/dashboard?join_error=missing_code", status_code=303)

    try:
        invite = db.query(InviteCode).filter(InviteCode.code == code).one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"초대 코드 조회 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    if invite is None:
        return RedirectResponse(url="/guardian/dashboard?join_error=invalid_code", status_code=303)

    if not auth.is_invite_code_valid(invite.expires_at, invite.used_at):
        return RedirectResponse(url="/guardian/dashboard?join_error=expired_code", status_code=303)

    try:
        already_linked = _is_guardian_linked_to_child(db, guardian.id, invite.child_id)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=500, detail=f"권한 확인 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error
    if already_linked:
        # 이미 연결된 보호자(최초 등록자 본인이 자기 코드를 입력한 경우 등).
        # 코드를 소모시키지 않는다 — 다른 진짜 초대 대상자가 여전히 쓸 수 있어야 하므로.
        return RedirectResponse(url="/guardian/dashboard?join_error=already_linked", status_code=303)

    # [Week3 동시성 수정] 코드를 "선점"하는 순간만 원자적 조건부 UPDATE로 처리한다.
    # WHERE에 used_at IS NULL을 걸어, 동시에 도착한 다른 요청이 이미 먼저
    # 선점했다면 이 UPDATE는 0행에 매치되어 아무것도 바꾸지 않는다.
    try:
        result = db.execute(
            update(InviteCode)
            .where(InviteCode.id == invite.id, InviteCode.used_at.is_(None))
            .values(used_at=datetime.now(timezone.utc), used_by_guardian_id=guardian.id)
        )
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"초대 코드 사용 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    if result.rowcount == 0:
        # 이 시점 사이에 다른 요청이 먼저 코드를 선점했다(동시 요청 경쟁 상황).
        db.rollback()
        return RedirectResponse(url="/guardian/dashboard?join_error=expired_code", status_code=303)

    try:
        db.add(ChildGuardian(child_id=invite.child_id, guardian_id=guardian.id, role="invited"))
        db.commit()
    except IntegrityError as error:
        # UniqueConstraint(child_id, guardian_id) 충돌 — already_linked 검사와
        # 동시 요청(경쟁 상황)으로 인한 극히 드문 경우. 롤백하면 방금 선점한
        # UPDATE도 함께 취소되어 코드가 다시 미사용 상태로 남는다(정상 동작).
        db.rollback()
        return RedirectResponse(url="/guardian/dashboard?join_error=already_linked", status_code=303)
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"초대 코드 사용 중 데이터베이스 오류가 발생했습니다: {error}"
        ) from error

    return RedirectResponse(url="/guardian/dashboard?joined=success", status_code=303)

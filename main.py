"""실종아동 QR 발견-신고 채팅 서비스: FastAPI 앱 진입점.

=== [Week1] PLAN.md 반영: 로그인+PIN 인증 방식으로 전환 ===
예전에는 보호자 인증이 "관리 링크(manage_token) 소유"였다. PLAN.md 최신 버전에서
이 방식은 두 가지 문제(계정 탈취 유사 구조, 링크 분실 시 재발급 어려움) 때문에
"전화번호(ID) + PIN(비밀번호) 로그인"으로 교체되었다. 이에 따라:
- 회원가입(/register), 로그인(/login), 로그아웃(/logout)이 실제로 동작한다.
- 보호자 대시보드는 URL에 토큰을 담지 않는 세션 기반 라우팅(/guardian/dashboard)이다.
- 채팅방 접근(GET /chat/{room_id}?role=guardian, WS /ws/chat/{room_id}?role=guardian)은
  더 이상 방마다 발급되는 1회성 토큰이 아니라, 매번 "로그인 세션의 guardian_id ==
  이 방의 아이 소유자(child.guardian_id)"를 재검증한다(GAP B, 다른 보호자의 방에
  들어가지 못하게 막는 소유권 검증).

라우트 구성:
- [Week1] 보호자 회원가입/로그인/로그아웃: GET·POST /register, GET·POST /login, POST /logout
- [Week1] 보호자 대시보드(세션 기반): GET /guardian/dashboard,
  POST /guardian/dashboard/children (아이 추가), .../{child_id}/toggle, .../{child_id}/delete
- 관리자(개발용 시드 도구): 아이 등록/QR 발급/상태 전환/방 종료 (POST /admin/children 등)
- 발견자: 랜딩(GET /found/{qr_token}), 발견 신고 시작(POST /found/{qr_token}/start)
- 채팅 화면(GET /chat/{room_id}) 및 실시간 WebSocket(/ws/chat/{room_id})
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import Request

import auth  # [Week1] PIN 해싱/검증 + 세션 관리
import qr_utils
import sms
from database import SessionLocal, get_db, init_db
from models import ChatRoom, Child, Guardian, Message, QrToken
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
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

# qr_token 길이(32바이트 이상 요구사항 충족).
QR_TOKEN_BYTES = 32

# [Week1] 로그인 세션 쿠키 유지 기간(7일). 만료되면 재로그인이 필요하다.
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 7

# 유효한 발신자 역할. 서버가 쿼리 파라미터로 받은 role을 신뢰의 기준으로 삼는다.
VALID_ROLES = {"finder", "guardian"}


@app.on_event("startup")
def on_startup() -> None:
    """앱 시작 시 DB 테이블을 준비한다."""
    init_db()


@app.get("/", response_class=HTMLResponse)
def home_page(request: Request) -> HTMLResponse:
    """v0 디자인의 서비스 소개/화면 둘러보기 홈."""
    return templates.TemplateResponse(request, "home.html", {})


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
    guardian = db.query(Guardian).filter(Guardian.phone == phone).one_or_none()
    if guardian is not None:
        return guardian

    guardian = Guardian(phone=phone, name=name, pin_hash=auth.hash_pin(pin))
    db.add(guardian)
    db.flush()  # guardian.id를 확보하기 위해 flush(커밋은 호출부에서).
    return guardian


@app.post("/admin/children", response_model=ChildCreateResponse)
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
            guardian_id=guardian.id,
            status=payload.status,
            qr_token=qr_token,
        )
        db.add(child)
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


@app.post("/admin/children/{qr_token}/status")
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
def download_child_qr(qr_token: str, db: Session = Depends(get_db)) -> Response:
    """등록된 아이의 QR 이미지를 PNG로 다운로드한다.

    Raises:
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

    found_url = qr_utils.build_found_url(BASE_URL, child.qr_token)

    try:
        png_bytes = qr_utils.generate_qr_png(found_url)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=500, detail=f"QR 이미지를 생성하지 못했습니다: {error}") from error

    return Response(content=png_bytes, media_type="image/png")


@app.post("/admin/rooms/{room_id}/close")
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


@app.get("/chat/{room_id}", response_class=HTMLResponse)
def chat_page(
    request: Request,
    room_id: str,
    role: str = "finder",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """발견자·보호자 공용 채팅 화면(HTML).

    [Week1/GAP B] 보호자(role=guardian)는 더 이상 URL의 1회성 토큰이 아니라
    로그인 세션으로 인증한다. 이 방의 아이(child)가 로그인한 보호자 소유인지
    (child.guardian_id == session.guardian_id)를 반드시 재검증한다 — 그렇지
    않으면 로그인한 보호자 A가 room_id만 알아내면 보호자 B의 아이 방에 들어갈
    수 있는 문제가 생긴다(PLAN GAP B "role 판정 + 방 소유권" 참고).

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
        # [Week1/GAP B] 세션 기반 소유권 재검증. 로그인 안 됨/다른 보호자의 아이면 거부.
        guardian = get_current_guardian(request, db)
        if guardian is None or room.child.guardian_id != guardian.id:
            raise HTTPException(
                status_code=403, detail="이 채팅방에 접근할 권한이 없습니다. 로그인해주세요."
            )

    if room.status == "closed":
        raise HTTPException(status_code=410, detail="이미 종료된 채팅방입니다.")

    # [Week1] 보호자는 쿠키(세션)로 인증되므로 ws_url에 별도 토큰을 붙이지 않는다.
    # 같은 오리진의 WebSocket 핸드셰이크에는 브라우저가 쿠키를 자동으로 실어 보낸다.
    ws_url = f"/ws/chat/{room_id}?role={role}"

    return templates.TemplateResponse(
        request,
        "chat.html",
        {"room_id": room_id, "role": role, "ws_url": ws_url},
    )


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
    # SMS 발송 실패가 채팅방 생성 자체를 막지 않도록 결과만 확인하고 진행한다.
    # 보호자 전화번호는 Guardian 관계를 통해 조회한다(Child에서 분리됨).
    sms_sent = sms.send_sms(child.guardian.phone, sms_message)
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
      아이 소유자(child.guardian_id)와 일치해야만 연결을 허용한다.
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
            # GAP A/B 검증에 쓸 아이 상태·소유자를 같은 트랜잭션에서 함께 조회한다
            # (child 관계의 lazy-load도 DB 쿼리이므로 여기서 함께 감싼다).
            child_status = room.child.status if (room and room.child) else None
            child_guardian_id = room.child.guardian_id if (room and room.child) else None
        except SQLAlchemyError:
            # DB 조회 실패 시 서버 내부 오류로 간주해 연결을 거부한다.
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return

        if room is None or room.status == "closed":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 2-1) [Week1/GAP B] 보호자는 세션 쿠키의 guardian_id가 이 방의 아이
        # 소유자와 일치해야만 입장 가능(다른 보호자의 방에 room_id로 무단 접근 방지).
        if role == "guardian":
            session_token = websocket.cookies.get(auth.SESSION_COOKIE_NAME)
            session_guardian_id = auth.get_session_guardian_id(session_token)
            if session_guardian_id is None or session_guardian_id != child_guardian_id:
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
        try:
            history = (
                db.query(Message)
                .filter(Message.room_id == room_id)
                .order_by(Message.created_at)
                .all()
            )
            await websocket.send_json(
                {"type": "history", "messages": [_message_to_dict(m) for m in history]}
            )
        except SQLAlchemyError:
            # 이력 로드 실패는 실시간 채팅 자체를 막지 않는다. 이력만 생략.
            await websocket.send_json({"type": "history", "messages": []})

        # 5) 입장 시스템 메시지를 상대방에게 알린다.
        label = ROLE_LABELS.get(role, "상대방")
        await manager.broadcast(
            room_id,
            {"type": "system", "content": f"{label}가 입장했습니다."},
            exclude=websocket,
        )

        # 6) 수신 루프.
        await _receive_loop(websocket, db, room, role)

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


async def _receive_loop(websocket: WebSocket, db: Session, room: ChatRoom, role: str) -> None:
    """WebSocket 수신 루프: 메시지를 검증·저장·브로드캐스트한다.

    [GAP B] 발견자(finder)는 위치를 먼저 공유해야 텍스트 채팅이 열린다.
    room.location_shared가 False인 동안 finder의 text 메시지는 거부하고,
    finder가 위치를 보내면 location_shared를 True로 전환한다. 보호자(guardian)는
    이 제약을 받지 않는다.

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

        # 메시지 타입에 따라 저장할 Message 객체를 구성한다.
        if msg_type == "text":
            # GAP B: finder는 위치 공유 전에는 텍스트를 보낼 수 없다.
            if role == "finder" and not room.location_shared:
                await websocket.send_json(
                    {
                        "type": "system",
                        "content": "먼저 위치를 공유해야 채팅을 시작할 수 있습니다.",
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
            latitude, longitude = coords
            message = Message(
                room_id=room_id,
                sender_role=role,
                content="",  # 위치 메시지는 좌표로 표현하므로 본문은 비운다.
                message_type="location",
                latitude=latitude,
                longitude=longitude,
            )
        else:
            await websocket.send_json(
                {"type": "system", "content": "지원하지 않는 메시지 형식입니다."}
            )
            continue

        try:
            db.add(message)
            # GAP B: finder가 위치를 처음 공유하면 채팅 잠금을 해제한다.
            # 메시지 저장과 잠금 해제를 같은 트랜잭션으로 커밋한다.
            unlocked_now = False
            if msg_type == "location" and role == "finder" and not room.location_shared:
                room.location_shared = True
                unlocked_now = True
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


def _parse_coordinates(payload: dict) -> tuple[float, float] | None:
    """위치 메시지 payload에서 위도/경도를 검증해 추출한다.

    Returns:
        (latitude, longitude) 튜플. 값이 없거나 범위를 벗어나면 None.
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

    return latitude, longitude


# ---------------------------------------------------------------------------
# [Week1] QR 시리얼 claim 공용 헬퍼 (회원가입 / 대시보드 "아이 추가"에서 공통 사용)
# ---------------------------------------------------------------------------


def _claim_qr_token(db: Session, serial: str, guardian_id: str, child_name: str) -> Child:
    """[Week1] 시리얼 번호로 사전 발급된 QrToken을 조회해 새 Child에 연결(claim)한다.

    PLAN "QR 사전 인쇄 & 시리얼 매칭" 흐름의 3단계(등록/매칭)에 해당한다.
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
        guardian_id=guardian_id,
        status="normal",
        qr_token=qr_entry.qr_token,  # Child.qr_token에 QrToken.qr_token 값을 그대로 복사.
    )
    db.add(child)
    db.flush()  # child.id를 확보하기 위해 flush.

    qr_entry.child_id = child.id
    qr_entry.claimed_at = datetime.now(timezone.utc)
    return child


# ---------------------------------------------------------------------------
# [Week1] 보호자 회원가입 / 로그인 / 로그아웃
# ---------------------------------------------------------------------------


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, error: str | None = None) -> HTMLResponse:
    """[Week1] 보호자 회원가입 폼(GET). error 쿼리 파라미터로 실패 사유를 표시한다."""
    return templates.TemplateResponse(request, "register.html", {"error": error})


@app.post("/register")
async def register_submit(request: Request, db: Session = Depends(get_db)):
    """[Week1] 보호자 회원가입 처리: Guardian 신규 생성 + 최초 아이 등록(시리얼 claim).

    PLAN 변경사항 반영:
    - 전화번호가 이미 가입되어 있으면 기존 계정에 자동으로 붙이지 않고 명확히
      실패 처리한다("이미 가입된 번호입니다, 로그인해주세요") — 예전 방식(전화번호
      매칭 재사용)이 갖고 있던 "제3자가 남의 번호로 아이를 몰래 추가할 수 있는"
      보안 문제를 없애기 위한 의도적 변경이다. (계정 열거 여지는 PLAN이 감수하기로
      한 트레이드오프.)
    - PIN은 평문으로 저장하지 않고 auth.hash_pin으로 해싱해 저장한다.

    Raises:
        HTTPException(500): DB 오류 시.
    """
    form = await request.form()
    guardian_name = str(form.get("guardian_name", "")).strip()
    guardian_phone = str(form.get("guardian_phone", "")).strip()
    guardian_pin = str(form.get("guardian_pin", "")).strip()
    child_name = str(form.get("child_name", "")).strip()
    serial = str(form.get("serial", "")).strip()

    if not guardian_name or not guardian_phone or not child_name or not serial:
        return RedirectResponse(url="/register?error=missing_fields", status_code=303)

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
        db.flush()  # guardian.id 확보(아직 커밋 전).
        _claim_qr_token(db, serial, guardian.id, child_name)
        db.commit()
    except LookupError:
        db.rollback()
        return RedirectResponse(url="/register?error=invalid_serial", status_code=303)
    except ValueError:
        db.rollback()
        return RedirectResponse(url="/register?error=serial_used", status_code=303)
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

    # 가입 성공 - 곧바로 로그인 상태로 대시보드에 진입시킨다.
    session_token = auth.create_session(guardian.id)
    response = RedirectResponse(url="/guardian/dashboard", status_code=303)
    _set_session_cookie(response, session_token)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request, error: str | None = None, minutes: int | None = None
) -> HTMLResponse:
    """[Week1] 보호자 로그인 폼(GET). 잠금 상태면 minutes로 남은 시간을 안내한다."""
    return templates.TemplateResponse(request, "login.html", {"error": error, "minutes": minutes})


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
    guardian_phone = str(form.get("guardian_phone", "")).strip()
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
        remaining_seconds = (guardian.locked_until - datetime.now(timezone.utc)).total_seconds()
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
# [Week1] 보호자 대시보드 (세션 기반 라우팅, URL에 토큰 없음)
# ---------------------------------------------------------------------------


@app.get("/guardian/dashboard", response_class=HTMLResponse)
def guardian_dashboard(request: Request, error: str | None = None, db: Session = Depends(get_db)):
    """[Week1] 보호자 대시보드: 로그인한 보호자의 아이 목록 + 진행 중인 채팅방.

    로그인하지 않았으면 /login으로 리다이렉트한다. 예전의 /guardian/{manage_token}
    방식과 달리 URL 자체에는 어떤 식별 정보도 담지 않는다(세션 쿠키가 유일한
    접근 수단).

    Raises:
        HTTPException(500): DB 오류 시.
    """
    guardian = get_current_guardian(request, db)
    if guardian is None:
        return RedirectResponse(url="/login", status_code=303)

    try:
        children = (
            db.query(Child)
            .filter(Child.guardian_id == guardian.id)
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
            }
        )

    return templates.TemplateResponse(
        request,
        "guardian_dashboard.html",
        {"guardian_name": guardian.name, "children": children_display, "error": error},
    )


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

    [Week1/GAP B] 소유권 재검증: child.guardian_id가 로그인한 보호자와 다르면
    403 — 다른 보호자가 child_id를 추측해서 남의 아이 상태를 바꾸지 못하도록 막는다.

    Raises:
        HTTPException(404): 존재하지 않는 아이일 때.
        HTTPException(403): 본인이 등록한 아이가 아닐 때.
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
    if child.guardian_id != guardian.id:
        raise HTTPException(status_code=403, detail="본인이 등록한 아이만 관리할 수 있습니다.")

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

    Raises:
        HTTPException(404): 존재하지 않는 아이일 때.
        HTTPException(403): 본인이 등록한 아이가 아닐 때.
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
    if child.guardian_id != guardian.id:
        raise HTTPException(status_code=403, detail="본인이 등록한 아이만 관리할 수 있습니다.")

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

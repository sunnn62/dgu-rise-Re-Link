"""실종아동 QR 발견-신고 채팅 서비스: FastAPI 앱 진입점.

라우트 구성:
- 관리자: 아이 등록/QR 발급 (POST /admin/children, GET /admin/children/{qr_token}/qr)
- 발견자: 랜딩(GET /found/{qr_token}), 발견 신고 시작(POST /found/{qr_token}/start)
- 채팅 화면(GET /chat/{room_id}) 및 실시간 WebSocket(/ws/chat/{room_id})

보호자는 발견 신고 시 SMS(목업)로 받은 1회성 token으로만 채팅방에 입장한다.
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
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import Request

import qr_utils
import sms
from database import SessionLocal, get_db, init_db
from models import ChatRoom, Child, Message
from websocket_manager import ConnectionManager

# 이 파일이 위치한 디렉터리 기준으로 templates/static 경로를 잡는다(실행 위치 무관).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="실종아동 QR 발견-신고 채팅 서비스")

# 정적 파일(css/js) 및 Jinja2 템플릿 설정.
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# room별 실시간 커넥션을 관리하는 싱글턴. 프로세스 메모리에만 존재한다.
manager = ConnectionManager()

# QR에 담길 발견 신고 URL의 베이스. 배포 시 실제 도메인으로 교체할 것.
BASE_URL = "http://localhost:8000"

# qr_token / guardian_token 길이(32바이트 이상 요구사항 충족).
QR_TOKEN_BYTES = 32
GUARDIAN_TOKEN_BYTES = 32

# 유효한 발신자 역할. 서버가 쿼리 파라미터로 받은 role을 신뢰의 기준으로 삼는다.
VALID_ROLES = {"finder", "guardian"}


@app.on_event("startup")
def on_startup() -> None:
    """앱 시작 시 DB 테이블을 준비한다."""
    init_db()


class ChildCreateRequest(BaseModel):
    """관리자용 아이 등록 요청 바디."""

    name: str = Field(min_length=1, max_length=100)
    guardian_phone: str = Field(min_length=1, max_length=20)
    guardian_name: str = Field(min_length=1, max_length=100)


class ChildCreateResponse(BaseModel):
    """아이 등록 응답. 내부 PK(id)는 노출하지 않고 qr_token만 반환한다."""

    qr_token: str
    found_url: str
    qr_download_url: str


@app.post("/admin/children", response_model=ChildCreateResponse)
def create_child(payload: ChildCreateRequest, db: Session = Depends(get_db)) -> ChildCreateResponse:
    """아이를 등록하고 QR 토큰을 발급한다.

    Raises:
        HTTPException(500): DB 저장 실패(제약조건 위반, 연결 오류 등) 시.
    """
    # secrets.token_urlsafe(32)는 약 43자의 암호학적으로 안전한 랜덤 문자열을 생성한다.
    qr_token = secrets.token_urlsafe(QR_TOKEN_BYTES)

    child = Child(
        name=payload.name,
        guardian_phone=payload.guardian_phone,
        guardian_name=payload.guardian_name,
        qr_token=qr_token,
    )

    try:
        db.add(child)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        # qr_token unique 제약 충돌 등. 극히 드물지만 재시도 안내를 제공한다.
        raise HTTPException(
            status_code=500,
            detail="QR 토큰 생성 중 충돌이 발생했습니다. 다시 시도해 주세요.",
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
    """발견 신고 시작 응답. 발견자에게 채팅방 접속 경로를 돌려준다.

    보호자용 링크/토큰은 이 응답에 노출하지 않는다(보호자는 SMS로만 받음).
    """

    room_id: str
    chat_url: str
    ws_url: str


# 역할별 표시명. 실명·전화번호 대신 항상 이 값만 화면/시스템 메시지에 사용한다.
ROLE_LABELS = {"finder": "발견자", "guardian": "보호자"}


def _build_guardian_chat_url(room_id: str, guardian_token: str) -> str:
    """보호자용 채팅방 입장 URL을 만든다(SMS로 전송할 링크).

    room_id와 별개인 guardian_token을 쿼리로 함께 실어 URL 위조를 방지한다.
    """
    return f"{BASE_URL}/chat/{room_id}?role=guardian&token={guardian_token}"


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

    # 템플릿에는 qr_token만 넘긴다(child.name 등은 절대 전달하지 않음).
    return templates.TemplateResponse(
        request, "found_landing.html", {"qr_token": qr_token}
    )


@app.get("/chat/{room_id}", response_class=HTMLResponse)
def chat_page(
    request: Request,
    room_id: str,
    role: str = "finder",
    token: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """발견자·보호자 공용 채팅 화면(HTML).

    보호자(role=guardian)는 SMS로 받은 1회성 token 검증을 통과해야 한다.
    token 검증 로직은 5단계에서 강화하며, 여기서는 방 존재/종료 여부만 확인한다.

    Raises:
        HTTPException(404): 방이 없을 때.
        HTTPException(403): role이 잘못됐거나 보호자 토큰이 유효하지 않을 때.
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

    # 보호자는 room에 발급된 guardian_token과 일치해야 입장 가능(URL 위조 방지).
    if role == "guardian" and (token is None or token != room.guardian_token):
        raise HTTPException(status_code=403, detail="보호자 인증에 실패했습니다.")

    if room.status == "closed":
        raise HTTPException(status_code=410, detail="이미 종료된 채팅방입니다.")

    # 보호자 WebSocket도 토큰 검증을 하므로 ws_url에 token을 포함한다.
    ws_url = f"/ws/chat/{room_id}?role={role}"
    if role == "guardian":
        ws_url += f"&token={token}"

    return templates.TemplateResponse(
        request,
        "chat.html",
        {"room_id": room_id, "role": role, "ws_url": ws_url},
    )


@app.post("/found/{qr_token}/start", response_model=StartChatResponse)
def start_chat(qr_token: str, db: Session = Depends(get_db)) -> StartChatResponse:
    """발견자가 '발견했어요'를 누르면 채팅방을 생성하고 보호자에게 SMS를 보낸다.

    보호자 인증 토큰(guardian_token)을 발급하고, 이 토큰이 담긴 채팅방 링크를
    보호자 전화번호로 SMS 발송(목업)한다. 토큰은 발견자 응답에는 노출하지 않는다.

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

    guardian_token = secrets.token_urlsafe(GUARDIAN_TOKEN_BYTES)
    room = ChatRoom(child_id=child.id, status="waiting", guardian_token=guardian_token)

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

    # 보호자에게 채팅방 입장 링크를 SMS로 발송한다(현재는 콘솔 로그 목업).
    guardian_url = _build_guardian_chat_url(room.id, guardian_token)
    sms_message = (
        "[아이발견알림] 아이를 발견한 분과 실시간 채팅이 시작되었습니다. "
        f"아래 링크로 입장해 주세요(개인정보는 공개되지 않습니다): {guardian_url}"
    )
    # SMS 발송 실패가 채팅방 생성 자체를 막지 않도록 결과만 확인하고 진행한다.
    sms_sent = sms.send_sms(child.guardian_phone, sms_message)
    if not sms_sent:
        # 목업에서는 항상 True지만, 실제 API 전환 시 실패 로깅 지점으로 사용한다.
        print(f"[경고] 보호자 SMS 발송 실패(room_id={room.id}). 재발송 로직이 필요합니다.")

    return StartChatResponse(
        room_id=room.id,
        chat_url=f"/chat/{room.id}?role=finder",
        ws_url=f"/ws/chat/{room.id}?role=finder",
    )


@app.websocket("/ws/chat/{room_id}")
async def chat_websocket(
    websocket: WebSocket, room_id: str, role: str = "finder", token: str | None = None
) -> None:
    """발견자·보호자 실시간 채팅 WebSocket.

    - role 쿼리 파라미터(finder|guardian)를 서버가 신뢰의 기준으로 삼는다.
    - 보호자(role=guardian)는 guardian_token이 일치해야만 연결을 허용한다.
    - closed 상태이거나 존재하지 않는 방은 연결을 거부한다.
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
        except SQLAlchemyError:
            # DB 조회 실패 시 서버 내부 오류로 간주해 연결을 거부한다.
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return

        if room is None or room.status == "closed":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 2-1) 보호자는 guardian_token이 일치해야만 입장 가능(URL/커넥션 위조 방지).
        if role == "guardian" and (token is None or token != room.guardian_token):
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
        await _receive_loop(websocket, db, room_id, role)

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


async def _receive_loop(websocket: WebSocket, db: Session, room_id: str, role: str) -> None:
    """WebSocket 수신 루프: 메시지를 검증·저장·브로드캐스트한다.

    WebSocketDisconnect는 호출부(chat_websocket)에서 처리하도록 전파한다.
    """
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
# 보호자 자가 등록 & 대시보드 (프론트 목업 — Guardian API 완성 후 교체 예정)
# ---------------------------------------------------------------------------

# In-memory mock data for frontend development
_mock_guardians: dict = {}
_mock_children_by_guardian: dict = {}


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    """보호자 자가 등록 폼."""
    return templates.TemplateResponse(request, "register.html", {})


@app.post("/register")
async def register_submit(request: Request):
    """보호자 등록 처리 (목업: 전화번호 기반 Guardian 조회/생성)."""
    from starlette.responses import RedirectResponse

    form = await request.form()
    guardian_name = form.get("guardian_name", "보호자")
    guardian_phone = form.get("guardian_phone", "")

    # 전화번호 기반으로 기존 토큰 조회 또는 새로 생성
    if guardian_phone in _mock_guardians:
        manage_token = _mock_guardians[guardian_phone]
    else:
        manage_token = secrets.token_urlsafe(16)
        _mock_guardians[guardian_phone] = manage_token
        _mock_children_by_guardian[manage_token] = {
            "name": guardian_name,
            "children": [],
        }

    return RedirectResponse(url=f"/guardian/{manage_token}", status_code=303)


@app.get("/guardian/{manage_token}", response_class=HTMLResponse)
def guardian_dashboard(request: Request, manage_token: str):
    """보호자 관리 대시보드 (목업 데이터)."""
    # 실제 등록 데이터가 있으면 사용, 아니면 데모용 목업 데이터 표시
    if manage_token in _mock_children_by_guardian:
        data = _mock_children_by_guardian[manage_token]
        guardian_name = data["name"]
        children = data["children"]
    else:
        # 목업 데이터 (프론트엔드 개발/데모용)
        guardian_name = "김보호자"
        children = [
            {"id": "1", "name": "김민준", "status": "normal", "qr_token": "mock-token-1"},
            {"id": "2", "name": "김서연", "status": "missing", "qr_token": "mock-token-2"},
        ]

    return templates.TemplateResponse(request, "guardian_dashboard.html", {
        "manage_token": manage_token,
        "guardian_name": guardian_name,
        "children": children,
    })


@app.post("/guardian/{manage_token}/children")
async def guardian_add_child(request: Request, manage_token: str):
    """아이 추가 (목업)."""
    from starlette.responses import RedirectResponse

    form = await request.form()
    child_name = form.get("child_name", "새 아이")

    if manage_token in _mock_children_by_guardian:
        children = _mock_children_by_guardian[manage_token]["children"]
        new_id = str(len(children) + 1)
        qr_token = secrets.token_urlsafe(16)
        children.append({
            "id": new_id,
            "name": child_name,
            "status": "normal",
            "qr_token": qr_token,
        })

    return RedirectResponse(url=f"/guardian/{manage_token}", status_code=303)


@app.post("/guardian/{manage_token}/children/{child_id}/toggle")
async def guardian_toggle_status(request: Request, manage_token: str, child_id: str):
    """아이 상태 토글 (목업)."""
    from starlette.responses import RedirectResponse

    if manage_token in _mock_children_by_guardian:
        children = _mock_children_by_guardian[manage_token]["children"]
        for child in children:
            if child["id"] == child_id:
                child["status"] = "normal" if child["status"] == "missing" else "missing"
                break

    return RedirectResponse(url=f"/guardian/{manage_token}", status_code=303)


@app.post("/guardian/{manage_token}/children/{child_id}/delete")
async def guardian_delete_child(request: Request, manage_token: str, child_id: str):
    """아이 삭제 (목업)."""
    from starlette.responses import RedirectResponse

    if manage_token in _mock_children_by_guardian:
        children = _mock_children_by_guardian[manage_token]["children"]
        _mock_children_by_guardian[manage_token]["children"] = [
            c for c in children if c["id"] != child_id
        ]

    return RedirectResponse(url=f"/guardian/{manage_token}", status_code=303)

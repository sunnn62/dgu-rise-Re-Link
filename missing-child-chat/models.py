"""실종아동 QR 채팅 서비스의 SQLAlchemy ORM 모델.

보안 설계:
- Child.qr_token은 URL에 노출되는 유일한 식별자이며, 아이 이름/보호자 정보를
  역추적할 수 없는 암호학적으로 안전한 랜덤 문자열(secrets.token_urlsafe)이다.
- Child.id(내부 PK)는 절대 URL이나 API 응답에 노출하지 않는다.
- guardian_phone 등 개인정보는 데모 단계에서는 평문 저장하지만, 실서비스
  전환 시 반드시 암호화 저장(예: 애플리케이션 레벨 AES 암호화 또는 DB 컬럼
  암호화)을 적용해야 한다. 아래 각 필드에 주석으로 명시해 둔다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Child(Base):
    """등록된 아이 정보. qr_token만 외부에 노출되는 식별자다."""

    __tablename__ = "children"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(100))
    # TODO(보안): 실서비스 전환 시 guardian_phone은 반드시 암호화 저장할 것
    # (예: 애플리케이션 레벨 AES-GCM 암호화 후 저장, 조회 시 복호화).
    guardian_phone: Mapped[str] = mapped_column(String(20))
    guardian_name: Mapped[str] = mapped_column(String(100))
    # 아이 옷의 QR코드에 노출되는 값. id와 분리하여 이름/보호자 추적 불가하게 함.
    qr_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    chat_rooms: Mapped[list["ChatRoom"]] = relationship(back_populates="child")


class ChatRoom(Base):
    """발견자-보호자 간 채팅방. 24시간 경과 또는 관리자 종료 시 closed."""

    __tablename__ = "chat_rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    child_id: Mapped[str] = mapped_column(String(36), ForeignKey("children.id"))
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting|active|closed
    # 보호자 전용 1회성 인증 토큰. room_id와 별개로 발급해 URL 위조를 방지한다.
    guardian_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    child: Mapped["Child"] = relationship(back_populates="chat_rooms")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="room", order_by="Message.created_at"
    )


class Message(Base):
    """채팅방 내 메시지. 연결이 끊겨도 이력 보존을 위해 즉시 DB에 저장한다."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    room_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_rooms.id"))
    sender_role: Mapped[str] = mapped_column(String(20))  # finder|guardian|system
    content: Mapped[str] = mapped_column(Text, default="")
    message_type: Mapped[str] = mapped_column(String(20), default="text")  # text|location|system
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    room: Mapped["ChatRoom"] = relationship(back_populates="messages")

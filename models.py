"""실종아동 QR 채팅 서비스의 SQLAlchemy ORM 모델.

=== Week1 데이터 모델 (PLAN.md Week1 반영, 로그인+PIN 방식으로 개편) ===

데이터 구조:
- Guardian(보호자) 단위로 아이를 묶는다. 보호자 한 명이 여러 아이를 등록할 수 있다.
- [Week1] 보호자 인증은 "관리 링크 소유"가 아니라 "전화번호(ID) + PIN(비밀번호) 로그인"
  방식이다. 그래서 Guardian에는 manage_token이 없고, 대신 pin_hash와 로그인 시도
  제한을 위한 failed_login_count/locked_until을 둔다. (PLAN "보호자 인증 방식(로그인+PIN)")
- Child는 최초 등록자(primary_guardian_id)를 가지며, 평상시/실종 상태(status)를 가진다.
  (GAP A)
- [Week3] 아이 한 명에 보호자 여러 명이 연결될 수 있다(가족 초대 기능). Child와
  Guardian은 ChildGuardian 중간 테이블로 다대다 관계를 맺는다. Child.primary_guardian_id
  는 그대로 남겨두는데, 이건 "누가 이 아이를 소유하는가"가 아니라 "삭제처럼 민감한
  조작을 누구에게만 허용할지"를 가리키는 필드로 역할이 좁혀졌다 — 초대로 합류한
  보호자는 채팅 열람/실종 신고 토글은 가능하지만, 아이 삭제는 최초 등록자만 할 수
  있다(초대받은 사람의 실수나 다툼으로 아이 데이터가 통째로 사라지는 사고 방지).
- QrToken은 등록과 무관하게 미리 배치로 발급해두는 QR 풀이다. 로그인한 보호자가 아이를
  추가할 때 스티커의 serial을 입력해 QrToken을 아이에게 연결(claim)한다.
- [Week3] InviteCode는 "이 아이에 보호자를 추가로 연결하기 위한 1회용 짧은 코드"다.
  QrToken의 serial-매칭 패턴을 그대로 재사용한 설계다(팀이 이미 익숙한 구조).
  최초 등록자만 발급할 수 있고, 24시간 후 만료되며, 1회 사용하면 즉시 소멸한다.
- [Week1] ChatRoom에는 더 이상 guardian_token이 없다. 예전에는 "방마다 발급되는
  토큰 소유 = 보호자 인증"이었지만, 로그인 세션 도입으로 이 토큰이 없어졌다.
  대신 보호자가 채팅방에 들어올 때마다 서버가 "이 방의 아이에 연결된 보호자
  목록에 로그인한 보호자가 포함되는가?"(ChildGuardian 조회)를 매번 재검증한다(GAP B).
  [Week3] 예전에는 이 재검증이 child.guardian_id와의 단순 비교였지만, 보호자가
  여러 명일 수 있게 되면서 "포함 여부" 검사로 바뀌었다.

보안 설계:
- Child.qr_token은 URL에 노출되는 유일한 식별자이며, 아이 이름/보호자 정보를
  역추적할 수 없는 암호학적으로 안전한 랜덤 문자열(secrets.token_urlsafe)이다.
- Child.id / Guardian.id(내부 PK)는 절대 URL이나 API 응답에 노출하지 않는다.
- [Week1] Guardian.pin_hash: PIN을 평문으로 저장하지 않고 해싱해서 저장한다(auth.py).
  6자리 이상 숫자 PIN + rate limit + 해싱 세 가지가 세트로 갖춰져야 최소 조건을
  만족한다는 것이 PLAN의 명시적 결정이다. 실서비스 전환 시 2FA/표준 비밀번호
  정책으로 강화가 필요하다.
- Guardian.phone 등 개인정보는 데모 단계에서는 평문 저장하지만, 실서비스
  전환 시 반드시 암호화 저장(예: 애플리케이션 레벨 AES-GCM)을 적용해야 한다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# [Week1] 보호자 인증 방식(로그인+PIN)
class Guardian(Base):
    """보호자. 전화번호(ID) + PIN(비밀번호)으로 로그인한다.

    [Week1] 예전의 manage_token(관리 링크 소유 = 인증) 방식은 제거되었다. 이제
    접근 수단은 로그인 세션뿐이며, 로그인 성공 시 발급되는 세션 토큰은 DB가
    아니라 auth.py의 인메모리 세션 저장소에서 관리한다(3주 데모 범위).
    """

    __tablename__ = "guardians"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # TODO(보안): 실서비스 전환 시 phone은 반드시 암호화 저장할 것.
    # 로그인 ID로 쓰이므로 전화번호는 유일해야 한다(unique). 가입 시 이미 있는
    # 번호면 "이미 가입된 번호입니다, 로그인해주세요" 안내로 처리한다(main.py).
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    # [Week1] PIN(6자리 이상 숫자)의 해시값. 평문 PIN은 절대 저장하지 않는다.
    # 해싱/검증 로직은 auth.py의 hash_pin/verify_pin이 담당한다.
    pin_hash: Mapped[str] = mapped_column(String(255))
    # [Week1] 로그인 무차별 대입 방지용 실패 횟수. 성공하면 0으로 리셋(auth.py).
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # [Week1] 5회 연속 실패 시 이 시각까지 로그인 자체를 거부한다(15분 잠금).
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # [Week3] "이 보호자가 최초 등록자인 아이들"만 가리킨다. 초대로 합류한 아이는
    # 포함하지 않는다 — 그런 아이는 child_links(ChildGuardian)로 조회해야 한다.
    primary_children: Mapped[list["Child"]] = relationship(
        back_populates="primary_guardian", cascade="all, delete-orphan"
    )
    # [Week3] 이 보호자가 연결된 모든 아이(최초 등록 + 초대로 합류) 관계 레코드.
    child_links: Mapped[list["ChildGuardian"]] = relationship(
        back_populates="guardian", cascade="all, delete-orphan"
    )


class Child(Base):
    """등록된 아이. qr_token만 외부에 노출되는 식별자다.

    status:
        - "normal": 평상시. QR을 스캔해도 채팅으로 이어지지 않는다.
        - "missing": 실종 신고 상태. 이때만 QR 스캔이 채팅 시작으로 이어진다(GAP A).
    """

    __tablename__ = "children"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(100))
    # [Week3] 이 아이를 최초 등록한 보호자. "누가 이 아이를 볼 수 있는가"의 기준이
    # 아니라(그건 ChildGuardian 테이블이 담당) "삭제처럼 민감한 조작을 누구에게만
    # 허용할지"의 기준이다. 예전 필드명은 guardian_id였는데, 보호자가 여러 명일 수
    # 있게 되면서 "유일한 소유자"가 아니라 "최초 등록자"라는 의미로 이름을 바꿨다.
    primary_guardian_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("guardians.id"), index=True
    )
    # [Week1/GAP A] 평상시/실종 상태. 기본값은 normal(등록만 해두고 실종 신고는
    # 하지 않은 상태). missing일 때만 QR 스캔이 채팅으로 이어진다.
    status: Mapped[str] = mapped_column(String(20), default="normal")  # normal|missing
    # 아이 옷의 QR코드에 노출되는 값. id와 분리하여 이름/보호자 추적 불가하게 함.
    # QrToken claim 흐름에서는 QrToken.qr_token 값을 그대로 복사해 채운다.
    qr_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    primary_guardian: Mapped["Guardian"] = relationship(back_populates="primary_children")
    # [Week3] 이 아이에 연결된 모든 보호자(최초 등록자 + 초대로 합류한 사람) 관계
    # 레코드. "이 보호자가 이 아이를 볼 수 있는가"는 항상 이 테이블로 판정한다.
    guardian_links: Mapped[list["ChildGuardian"]] = relationship(
        back_populates="child", cascade="all, delete-orphan"
    )
    invite_codes: Mapped[list["InviteCode"]] = relationship(
        back_populates="child", cascade="all, delete-orphan"
    )
    chat_rooms: Mapped[list["ChatRoom"]] = relationship(
        back_populates="child", cascade="all, delete-orphan"
    )
    qr_pool_entry: Mapped["QrToken | None"] = relationship(back_populates="child")


class ChildGuardian(Base):
    """[Week3] 아이 ↔ 보호자 다대다 연결 테이블. "누가 이 아이를 볼 수 있는가"의
    유일한 근거다(main.py의 채팅 접근권한/대시보드 조회가 이 테이블을 기준으로 판정).

    role:
        - "primary": 최초 등록자. Child.primary_guardian_id와 항상 짝을 이룬다
          (아이 생성 시 이 레코드도 함께 만든다).
        - "invited": 초대 코드로 나중에 합류한 보호자.

    두 role 모두 채팅 열람, 실종 신고 토글, QR 다운로드는 동일하게 할 수 있다.
    아이 삭제와 초대 코드 발급만 role="primary"인 사람으로 제한한다(main.py 참고).
    """

    __tablename__ = "child_guardians"
    __table_args__ = (
        # 같은 보호자가 같은 아이에 중복으로 연결되는 것을 DB 레벨에서 막는다
        # (애플리케이션 로직에서도 검사하지만, 동시 요청 경쟁 상황에 대한 방어).
        UniqueConstraint("child_id", "guardian_id", name="uq_child_guardian"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    child_id: Mapped[str] = mapped_column(String(36), ForeignKey("children.id"), index=True)
    guardian_id: Mapped[str] = mapped_column(String(36), ForeignKey("guardians.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="invited")  # primary|invited
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    child: Mapped["Child"] = relationship(back_populates="guardian_links")
    guardian: Mapped["Guardian"] = relationship(back_populates="child_links")


class InviteCode(Base):
    """[Week3] 가족 초대용 1회성 코드. QrToken의 "짧은 코드로 매칭" 패턴을 재사용한다.

    - 최초 등록자(primary_guardian)만 발급할 수 있다(main.py에서 강제).
    - 24시간 후 만료(expires_at).
    - 사용되면 used_at/used_by_guardian_id가 채워지고, 그 순간부터 다시 쓸 수 없다
      (검증 로직이 used_at is not None인 코드를 항상 거부한다).
    """

    __tablename__ = "invite_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # 사람이 직접 전달하는 짧은 코드(예: 8자리 영숫자). URL에는 노출되지 않는다
    # (QR처럼 자동 스캔이 아니라, 문자/구두로 직접 전달하는 값이므로).
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    child_id: Mapped[str] = mapped_column(String(36), ForeignKey("children.id"), index=True)
    created_by_guardian_id: Mapped[str] = mapped_column(String(36), ForeignKey("guardians.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_guardian_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("guardians.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    child: Mapped["Child"] = relationship(back_populates="invite_codes")


class QrToken(Base):
    """사전 인쇄용 QR 토큰 풀.

    등록과 무관하게 배치로 미리 생성해두고(qr_token + 사람이 읽을 serial),
    로그인한 보호자가 아이를 추가할 때 serial을 입력하면 해당 아이에 연결(claim)된다.
    """

    __tablename__ = "qr_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # URL에 쓰이는 긴 랜덤 문자열(Child.qr_token과 같은 역할).
    qr_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 스티커에 인쇄할 짧은 코드. 사람이 눈으로 읽고 입력하므로 헷갈리는 문자는 제외한다.
    serial: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # 아직 등록되지 않은 토큰은 child_id가 NULL이다.
    child_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("children.id"), nullable=True, unique=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    child: Mapped["Child | None"] = relationship(back_populates="qr_pool_entry")


class ChatRoom(Base):
    """발견자-보호자 간 채팅방. 24시간 경과 또는 관리자 종료 시 closed.

    [Week1 변경] 예전에는 이 모델에 guardian_token(방마다 발급되는 1회성 인증
    토큰)이 있었다. 로그인+세션 방식으로 전환하면서 이 필드는 제거되었다 —
    보호자 접근 여부는 이제 "로그인 세션의 guardian_id가 이 방의 아이에
    연결돼(ChildGuardian) 있는가"로 매번 재검증한다(main.py의
    chat_page/chat_websocket, GAP B 참고).
    """

    __tablename__ = "chat_rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    child_id: Mapped[str] = mapped_column(String(36), ForeignKey("children.id"))
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting|active|closed
    # [Week2 역할 변경] 이 방에서 위치가 "한 번이라도" 공유됐는지의 집계 기록.
    # GAP B의 채팅 잠금 판정은 더 이상 이 필드가 아니라 발견자(익명 쿠키) 단위로
    # 한다(auth의 발견자 세션 참고) — 발견자가 여러 명일 때 1번 발견자의 공유가
    # 2번 발견자의 잠금까지 풀면 안 되기 때문. 보호자에게는 잠금 제약이 없다.
    location_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # [Week2] LLM 메시지 모더레이션(moderation.py)에서 악용 문구가 감지되면 해당
    # 역할의 "이후" text 전송을 막기 위한 플래그. 판정에 시간이 걸려 이미 보낸
    # 메시지 자체는 회수할 수 없으므로, 다음 메시지부터 차단하는 방식이다(PLAN
    # "메시지 모더레이션" 참고). 기본값은 제한 없음(False).
    finder_restricted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    guardian_restricted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    child: Mapped["Child"] = relationship(back_populates="chat_rooms")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="room", order_by="Message.created_at", cascade="all, delete-orphan"
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
    # [Week2] 위치 메시지의 오차 반경(미터, Geolocation API의 accuracy 값).
    # GPS(실외)는 5~20m로 정확하지만 실내·도심에서는 WiFi/기지국 기반으로
    # 50~100m 이상 튈 수 있어, 보호자가 이 위치를 얼마나 믿어야 하는지 판단할
    # 근거로 함께 저장한다(PLAN GAP B "위치 오차 미표시" 개선 항목).
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    # [Week2] LLM 모더레이션(moderation.py)이 이 메시지를 악용 소지가 있다고
    # 판단했는지 여부. 감사/추후 검토용 기록이며, 채팅 자체를 막지는 않는다
    # (판정은 비동기로 사후에 붙으므로 이 메시지는 이미 전달된 뒤다).
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    room: Mapped["ChatRoom"] = relationship(back_populates="messages")

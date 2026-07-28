"""SQLAlchemy DB 엔진 및 세션 설정.

개발 환경은 SQLite를 사용하며, 프로덕션에서는 DATABASE_URL 환경변수만
PostgreSQL 접속 문자열로 바꾸면 되도록 설계했다(SQLAlchemy ORM 사용).
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# [.env 주의] python-dotenv는 ".env"에 "DATABASE_URL=" 처럼 값이 빈 줄이 있으면
# 환경변수를 "빈 문자열"로 설정한다(키가 아예 없는 것과 다름). os.getenv의 기본값은
# 키가 존재하지 않을 때만 적용되므로, os.getenv(..., 기본값)만 쓰면 빈 문자열이
# 그대로 반환되어 create_engine("")이 파싱 오류를 낸다. "or"로 빈 문자열도
# 기본값으로 치환되도록 명시적으로 처리한다.
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./missing_child_chat.db"

# SQLite는 기본적으로 단일 스레드 접근만 허용하므로, FastAPI가 여러 요청을
# 동시에 처리할 때 필요한 옵션을 추가한다. PostgreSQL로 전환 시에는
# connect_args를 비워도 된다.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """모든 ORM 모델의 베이스 클래스."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 의존성 주입용 DB 세션 제공자.

    요청 처리 중 예외가 발생해도 세션이 반드시 닫히도록 finally에서 정리한다.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """테이블이 없으면 생성한다(최초 1회 호출로 충분)."""
    # models 모듈을 import해야 Base.metadata에 테이블이 등록된다.
    import models  # noqa: F401  (등록을 위한 side-effect import)

    Base.metadata.create_all(bind=engine)

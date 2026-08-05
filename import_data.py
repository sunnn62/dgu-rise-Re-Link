"""SQLite -> Postgres 데이터 이전 스크립트: export_data가 만든 JSON을 새 DB에 복원한다.

GET /admin/export-data(main.py)로 받은 JSON 파일을, DATABASE_URL 환경변수가
가리키는 DB(보통 새로 만든 Postgres)에 그대로 다시 채워 넣는다. 외래키
의존성 순서(보호자 -> 아이 -> 나머지)를 지켜서 넣어야 하므로, 테이블 순서를
고정해뒀다.

이미 존재하는 행(같은 PK)은 건너뛴다 — 재실행해도 중복 삽입되지 않는다.

사용 예 (Postgres의 External Database URL을 로컬에서 가리키게 하고 실행):
    $env:DATABASE_URL = "postgresql://user:pass@host/dbname"
    python import_data.py relink_export.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError

from database import SessionLocal, init_db
from models import ChatRoom, Child, ChildGuardian, Guardian, InviteCode, Message, QrToken

# (테이블 키, 모델, 이 테이블에서 datetime으로 되돌려야 할 컬럼) — 외래키 의존
# 순서대로 나열한다(보호자가 제일 먼저, 메시지가 제일 나중).
_TABLES = [
    ("guardians", Guardian, ("locked_until", "created_at")),
    ("children", Child, ("created_at",)),
    ("child_guardians", ChildGuardian, ("created_at",)),
    ("qr_tokens", QrToken, ("claimed_at", "created_at")),
    ("invite_codes", InviteCode, ("expires_at", "used_at", "created_at")),
    ("chat_rooms", ChatRoom, ("created_at", "closed_at")),
    ("messages", Message, ("created_at",)),
]


def _deserialize_row(row: dict, datetime_fields: tuple[str, ...]) -> dict:
    """JSON에서 문자열로 온 datetime 필드를 다시 datetime 객체로 되돌린다."""
    result = dict(row)
    for field in datetime_fields:
        if result.get(field):
            result[field] = datetime.fromisoformat(result[field])
    return result


def import_from_json(json_path: str) -> dict[str, tuple[int, int]]:
    """JSON 파일을 읽어 현재 DATABASE_URL이 가리키는 DB에 복원한다.

    Returns:
        테이블명 -> (복원된 개수, 이미 존재해서 건너뛴 개수) 매핑.

    Raises:
        FileNotFoundError: json_path가 없는 경우.
        RuntimeError: DB 저장 중 오류가 발생한 경우.
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    init_db()
    db = SessionLocal()
    summary: dict[str, tuple[int, int]] = {}
    try:
        for table_key, model, datetime_fields in _TABLES:
            rows = data.get(table_key, [])
            existing_ids = {row_id for (row_id,) in db.query(model.id).all()}

            restored = 0
            skipped = 0
            for row in rows:
                if row["id"] in existing_ids:
                    skipped += 1
                    continue
                db.add(model(**_deserialize_row(row, datetime_fields)))
                existing_ids.add(row["id"])
                restored += 1

            # 테이블 하나씩 커밋 — 뒤 테이블(외래키로 앞 테이블을 참조)이
            # 앞 테이블의 확정된 행을 바로 참조할 수 있게 한다.
            db.commit()
            summary[table_key] = (restored, skipped)
    except SQLAlchemyError as error:
        db.rollback()
        raise RuntimeError(f"복원 중 데이터베이스 오류가 발생했습니다: {error}") from error
    finally:
        db.close()

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="export_data가 만든 JSON을 새 DB로 복원")
    parser.add_argument("json_path", help="GET /admin/export-data로 받은 JSON 파일 경로")
    args = parser.parse_args(argv)

    try:
        summary = import_from_json(args.json_path)
    except FileNotFoundError:
        print(f"[실패] 파일을 찾을 수 없습니다: {args.json_path}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"[실패] {error}", file=sys.stderr)
        return 1

    print("[완료] 테이블별 복원 결과:")
    for table_key, (restored, skipped) in summary.items():
        print(f"  - {table_key}: 복원 {restored}건, 건너뜀 {skipped}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

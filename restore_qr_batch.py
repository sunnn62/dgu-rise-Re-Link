"""QR 배치 복구 스크립트: serials.csv 백업으로 QrToken을 DB에 복원한다.

배포 환경의 DB(SQLite/Postgres)가 재배포·재시작 등으로 초기화되거나 손상돼도,
미리 백업해둔 serials.csv만 있으면 이미 인쇄된 QR 스티커들이 다시 유효한
상태로 복구된다. generate_qr_batch.py / POST /admin/qr-batch가 만드는 CSV
형식(serial, qr_token, found_url, png_file 컬럼)을 그대로 읽는다.

이미 존재하는 시리얼/qr_token은 건너뛰고(중복 방지), 없는 것만 새로 추가한다.
아이에게 이미 매칭(claim)됐던 정보(child_id, claimed_at)는 CSV에 애초에
없으므로 복구되지 않는다 — 이 스크립트는 "QR 자체가 다시 유효해지는 것"만
보장하고, 어떤 보호자가 어떤 아이에 등록해뒀었는지까지는 복구하지 못한다.
그 경우 보호자가 대시보드에서 같은 시리얼로 다시 아이를 등록해야 한다.

사용 예:
    python restore_qr_batch.py qr_batch/serials.csv
"""

from __future__ import annotations

import argparse
import csv
import sys

from sqlalchemy.exc import SQLAlchemyError

from database import SessionLocal, init_db
from models import QrToken


def restore_from_csv(csv_path: str) -> tuple[int, int]:
    """CSV의 각 행을 QrToken으로 복원한다.

    Returns:
        (복원된 개수, 이미 존재해서 건너뛴 개수) 튜플.

    Raises:
        FileNotFoundError: csv_path가 없는 경우.
        RuntimeError: DB 저장 중 오류가 발생한 경우.
    """
    init_db()

    db = SessionLocal()
    restored = 0
    skipped = 0
    try:
        existing_qr_tokens = {t for (t,) in db.query(QrToken.qr_token).all()}
        existing_serials = {s for (s,) in db.query(QrToken.serial).all()}

        with open(csv_path, encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                serial = row["serial"].strip()
                qr_token = row["qr_token"].strip()

                if qr_token in existing_qr_tokens or serial in existing_serials:
                    skipped += 1
                    continue

                db.add(QrToken(qr_token=qr_token, serial=serial))
                existing_qr_tokens.add(qr_token)
                existing_serials.add(serial)
                restored += 1

        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise RuntimeError(f"복구 중 데이터베이스 오류가 발생했습니다: {error}") from error
    finally:
        db.close()

    return restored, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="serials.csv 백업으로 QrToken 복구")
    parser.add_argument("csv_path", help="백업해둔 serials.csv 경로")
    args = parser.parse_args(argv)

    try:
        restored, skipped = restore_from_csv(args.csv_path)
    except FileNotFoundError:
        print(f"[실패] 파일을 찾을 수 없습니다: {args.csv_path}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"[실패] {error}", file=sys.stderr)
        return 1

    print(f"[완료] 복구 {restored}건, 이미 존재해서 건너뜀 {skipped}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

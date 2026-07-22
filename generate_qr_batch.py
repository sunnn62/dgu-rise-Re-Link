"""QrToken 사전 발급 배치 생성 스크립트.

등록과 무관하게 QR 토큰 풀을 미리 만들어둔다. 각 QrToken은
- qr_token: URL에 쓰이는 긴 랜덤 문자열(Child.qr_token과 같은 역할)
- serial:   스티커에 인쇄할 짧은 코드(사람이 눈으로 읽고 입력)
를 가진다. 생성된 각 토큰의 발견 URL을 QR PNG로 함께 출력하고, 시리얼 목록을
CSV로 남겨 디자인팀에 전달할 수 있게 한다.

사용 예:
    python generate_qr_batch.py --count 50 --out qr_batch

주의:
- QR에 인코딩되는 URL의 도메인은 BASE_URL 환경변수로 결정된다. **배포 도메인이
  확정된 뒤** 그 도메인으로 생성해야 한다(로컬 도메인으로 만든 배치를 그대로
  인쇄하지 않도록 주의 — PLAN "발표 당일 최소 조건" 참고).
      PowerShell:  $env:BASE_URL = "https://your-domain"; python generate_qr_batch.py --count 50
- 이 값은 main.py의 BASE_URL과 반드시 일치해야 한다.
"""

from __future__ import annotations

import argparse
import csv
import os
import secrets
import sys

from sqlalchemy.exc import SQLAlchemyError

import qr_utils
from database import SessionLocal, init_db
from models import QrToken

# 헷갈리는 문자(0/O, 1/I/L)를 제외한 시리얼용 문자 집합.
_SERIAL_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# 시리얼 형식: 4자-4자(예: RL-AB2C-9KMP). 앞의 RL은 서비스 식별 접두어.
_SERIAL_PREFIX = "RL"
_SERIAL_GROUP_LEN = 4
_SERIAL_GROUPS = 2

# 배포 도메인 확정 후 그 값으로 설정할 것. main.py의 BASE_URL과 일치해야 한다.
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def _random_serial() -> str:
    """헷갈리는 문자를 제외한 랜덤 시리얼을 만든다(예: RL-AB2C-9KMP)."""
    groups = [
        "".join(secrets.choice(_SERIAL_ALPHABET) for _ in range(_SERIAL_GROUP_LEN))
        for _ in range(_SERIAL_GROUPS)
    ]
    return "-".join([_SERIAL_PREFIX, *groups])


def _generate_unique_serial(existing: set[str]) -> str:
    """이미 사용된(existing) 시리얼과 겹치지 않는 새 시리얼을 만든다.

    Raises:
        RuntimeError: 합리적인 시도 횟수 내에 유일한 시리얼을 못 만든 경우.
    """
    for _ in range(100):
        serial = _random_serial()
        if serial not in existing:
            return serial
    raise RuntimeError("유일한 시리얼 번호 생성에 반복적으로 실패했습니다.")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QrToken 배치 생성 스크립트")
    parser.add_argument("--count", type=int, default=10, help="생성할 QrToken 개수 (기본 10)")
    parser.add_argument(
        "--out", default="qr_batch", help="QR PNG와 serials.csv를 저장할 디렉터리 (기본 qr_batch)"
    )
    return parser.parse_args(argv)


def generate_batch(count: int, out_dir: str) -> int:
    """count개의 QrToken을 생성하고 PNG/CSV를 out_dir에 저장한다.

    Returns:
        실제로 생성된 QrToken 개수.

    Raises:
        ValueError: count가 1 미만인 경우.
    """
    if count < 1:
        raise ValueError("count는 1 이상이어야 합니다.")

    # 테이블이 없으면 생성한다(스크립트 단독 실행 대비).
    init_db()

    # 출력 디렉터리 준비. 이미 있으면 그대로 사용한다.
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as error:
        raise RuntimeError(f"출력 디렉터리를 만들 수 없습니다({out_dir}): {error}") from error

    db = SessionLocal()
    created: list[tuple[str, str]] = []  # (serial, qr_token)
    try:
        # 기존 DB에 있는 시리얼을 미리 읽어와 배치 내/외 중복을 모두 방지한다.
        existing_serials = {s for (s,) in db.query(QrToken.serial).all()}

        for _ in range(count):
            serial = _generate_unique_serial(existing_serials)
            existing_serials.add(serial)
            qr_token = secrets.token_urlsafe(32)
            db.add(QrToken(qr_token=qr_token, serial=serial))
            created.append((serial, qr_token))

        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise RuntimeError(f"QrToken 저장 중 데이터베이스 오류가 발생했습니다: {error}") from error
    finally:
        db.close()

    # QR PNG 일괄 생성 및 시리얼 CSV 기록.
    csv_path = os.path.join(out_dir, "serials.csv")
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["serial", "qr_token", "found_url", "png_file"])
            for serial, qr_token in created:
                found_url = qr_utils.build_found_url(BASE_URL, qr_token)
                png_name = f"{serial}.png"
                png_path = os.path.join(out_dir, png_name)

                png_bytes = qr_utils.generate_qr_png(found_url)
                with open(png_path, "wb") as png_file:
                    png_file.write(png_bytes)

                writer.writerow([serial, qr_token, found_url, png_name])
    except (OSError, ValueError, RuntimeError) as error:
        # 파일 I/O 실패 또는 QR 생성 실패. DB에는 이미 커밋됐으므로, 어떤 시리얼까지
        # PNG가 만들어졌는지 사용자에게 알리고 재실행/수동 보완을 안내한다.
        raise RuntimeError(
            f"QR 이미지/CSV 출력 중 오류가 발생했습니다(DB에는 {len(created)}건 저장됨): {error}"
        ) from error

    return len(created)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        n = generate_batch(args.count, args.out)
    except (ValueError, RuntimeError) as error:
        print(f"[실패] {error}", file=sys.stderr)
        return 1

    print(f"[완료] QrToken {n}건 생성, PNG/CSV 저장 위치: {args.out}")
    print(f"       BASE_URL={BASE_URL} (배포 도메인과 일치하는지 반드시 확인)")
    print(f"       시리얼 목록: {os.path.join(args.out, 'serials.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

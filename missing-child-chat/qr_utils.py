"""QR 코드 생성 유틸리티.

아이의 qr_token만을 인코딩한 QR 이미지를 만든다. child_id나 이름 등
개인정보는 절대 QR에 담지 않는다.
"""

from __future__ import annotations

import io

import qrcode


def build_found_url(base_url: str, qr_token: str) -> str:
    """QR에 인코딩할 발견 신고 URL을 만든다."""
    return f"{base_url.rstrip('/')}/found/{qr_token}"


def generate_qr_png(data: str) -> bytes:
    """주어진 문자열을 인코딩한 QR 코드 PNG 이미지 바이트를 생성한다.

    Raises:
        ValueError: data가 비어 있거나 QR로 인코딩할 수 없는 경우.
    """
    if not data:
        raise ValueError("QR로 인코딩할 데이터가 비어 있습니다.")

    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except (qrcode.exceptions.DataOverflowError, ValueError) as error:
        # 데이터가 QR 최대 용량을 초과하거나 인코딩할 수 없는 경우.
        raise ValueError(f"QR 코드를 생성할 수 없습니다: {error}") from error
    except OSError as error:
        # 이미지 인코딩/버퍼 쓰기 실패 등 Pillow 관련 I/O 오류.
        raise RuntimeError(f"QR 이미지를 인코딩하는 중 오류가 발생했습니다: {error}") from error

"""SMS 발송 인터페이스.

현재는 실제 발송 대신 콘솔에 로그만 남기는 목업 구현이다. 나중에 알리고·쿨SMS
등 실제 API로 교체할 때는 이 파일의 send_sms 함수 시그니처만 그대로 유지한 채
내부 구현만 바꾸면 된다(호출부 코드는 변경할 필요 없음).

TODO(보안): 실서비스 전환 시 guardian_phone 등 수신 전화번호를 로그에 그대로
남기지 말고 마스킹(예: 010-****-1234) 처리할 것.
"""

from __future__ import annotations


def send_sms(phone: str, message: str) -> bool:
    """SMS를 발송한다(현재는 콘솔 로그로 대체).

    Returns:
        발송 성공 여부. 목업 구현이므로 항상 True를 반환하지만, 실제 API로
        교체 시에는 API 응답에 따라 True/False를 반환하도록 구현해야 한다.

    Note:
        실제 API 연동 시 이 함수 내부에서 네트워크 예외(타임아웃, 인증 실패 등)를
        구체적으로 처리하고, 실패 시 False를 반환하거나 상위에 알릴 수 있도록
        예외를 다시 던지는 정책을 정해야 한다. 호출부는 send_sms가 예외를
        던지지 않는다고 가정하고 있으므로, 실 구현 시 반드시 이 계약을 지킬 것.
    """
    masked_phone = _mask_phone(phone)
    print(f"[SMS 목업 발송] to={masked_phone} message={message}")
    return True


def _mask_phone(phone: str) -> str:
    """전화번호 뒷 4자리 이전을 마스킹한다(로그 노출 최소화)."""
    if len(phone) < 4:
        return "*" * len(phone)
    return "*" * (len(phone) - 4) + phone[-4:]

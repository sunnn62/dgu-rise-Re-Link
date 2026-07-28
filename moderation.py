"""[Week2] LLM 기반 채팅 메시지 악용 탐지(모더레이션).

PLAN.md "메시지 모더레이션 (LLM 기반 악용 탐지)"에 따라, 발견자-보호자 채팅에서
개인정보 추가 유출 시도·협박·갈취 등 범죄 악용 소지가 있는 메시지를 Upstage
Solar API로 비동기 검사한다.

[전환 기록] 원래 Anthropic Claude(tool-call 구조화 응답)로 구현했으나, 비용/기업
정책상 이유로 Upstage Solar API(OpenAI SDK 호환)로 전환했다. Solar 채팅
API가 OpenAI의 response_format=json_object/tool_choice를 동일하게 지원하는지
문서상 명확하지 않으므로, tool-call 대신 "JSON만 출력하라"는 프롬프트 강제 +
방어적 파싱 방식을 쓴다. 이 방식은 tool calling 지원 여부와 무관하게 동작한다.

핵심 설계 원칙(모두 PLAN에 명시된 결정 — Solar 전환 후에도 동일하게 유지):
1. 채팅 자체를 막지 않는다 — 메시지는 항상 즉시 저장·브로드캐스트되고, 이 모듈의
   판정은 그 뒤에 백그라운드로 실행된다. 판정 대기 때문에 실시간성이 떨어지면
   안 된다(생명·안전이 걸린 채널이므로).
2. Fail-open — API 키 미설정, 네트워크 오류, 타임아웃, 응답 파싱 실패 등 어떤
   이유로든 판정을 못 하면 항상 "허용"(위반 아님)으로 처리한다. 모더레이션
   인프라 장애가 핵심 안전 기능(채팅)을 막아서는 안 된다.
3. "이 메시지"가 아니라 "다음 메시지부터" 제한 — 판정에 시간이 걸리므로 이미
   전달된 메시지 자체는 회수할 수 없다. 위반이 확인되면 main.py가 해당 역할의
   ChatRoom.finder_restricted/guardian_restricted를 True로 세팅한다(이 모듈은
   판정만 하고, DB 갱신은 main.py의 책임).

이 모듈이 담당하는 것은 오직 check_message(content) -> bool 하나다. DB 접근이나
WebSocket 브로드캐스트는 전혀 하지 않는다(관심사 분리, main.py가 오케스트레이션).
"""

from __future__ import annotations

import json
import os

import openai

# Solar Mini: 짧은 텍스트 분류에 충분하고 비용/지연이 가장 낮은 모델을 쓴다.
# (실시간 채팅 배경 검사이므로 지연이 짧을수록 "다음 메시지 차단" 반영이 빨라진다.)
# 더 정교한 판단이 필요하면 "solar-pro"로 교체 가능.
_MODEL = "solar-mini"

_BASE_URL = "https://api.upstage.ai/v1"

# 판정 자체가 채팅 지연을 유발하지 않도록 짧게 제한한다. 타임아웃이면 fail-open.
_REQUEST_TIMEOUT_SECONDS = 5.0

# Solar 채팅 API가 OpenAI의 tool_choice/response_format을 동일하게 지원하는지
# 불확실하므로, system prompt로 "JSON만 출력"을 강제하고 응답 문자열을
# json.loads로 방어적으로 파싱한다.
_SYSTEM_PROMPT = (
    "당신은 실종아동 발견자-보호자 익명 채팅의 안전 모니터링 시스템입니다. "
    "발견자와 보호자는 서로의 실명과 전화번호를 모르는 상태로 대화합니다. "
    "입력된 메시지 한 건을 검사해 아래 기준 중 하나라도 명백히 해당하면 위반으로 "
    "판정하세요:\n"
    "1) 상대방의 실명, 전화번호, 주소, 계좌번호 등 개인정보를 캐내려는 시도\n"
    "2) 금전이나 대가를 요구하는 협박·갈취\n"
    "3) 폭력, 성적 착취, 그 외 명백한 범죄 목적의 위협\n\n"
    "다급한 정상 상황 설명(예: 위치 설명, 안전 우려 표현)은 위반이 아닙니다. "
    "애매하면 위반이 아닌 것으로(false) 판정하세요 — 오탐으로 정상적인 구조 "
    "대화를 막는 것이 더 큰 문제입니다.\n\n"
    '반드시 다른 설명 없이 정확히 이 형식의 JSON 한 줄만 출력하세요: '
    '{"violates_policy": true} 또는 {"violates_policy": false}'
)


def _get_client() -> "openai.OpenAI | None":
    """API 키가 설정돼 있으면 클라이언트를, 없으면 None을 반환한다.

    [Fail-open] 키가 없는 것은 예외 상황이 아니라 "모더레이션 기능이 꺼져
    있음"을 뜻하는 정상적인 상태로 취급한다(PLAN: 배포 전 키 설정 여부를
    별도로 점검하도록 안내하되, 없다고 서버가 죽으면 안 된다).
    """
    api_key = os.getenv("UPSTAGE_API_KEY")
    if not api_key:
        return None
    return openai.OpenAI(
        api_key=api_key,
        base_url=_BASE_URL,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )


def _parse_violation(raw_text: str) -> bool:
    """모델 응답 문자열에서 violates_policy 값을 방어적으로 추출한다.

    모델이 지시를 어기고 JSON 앞뒤에 설명을 덧붙이는 경우까지 고려해, 문자열
    전체가 아니라 첫 '{'부터 마지막 '}'까지를 잘라내 파싱을 시도한다.
    """
    text = raw_text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("응답에서 JSON 객체를 찾지 못함")

    payload = json.loads(text[start : end + 1])
    return bool(payload["violates_policy"])


def check_message(content: str) -> bool:
    """메시지 내용이 악용 정책을 위반하는지 Upstage Solar API로 판정한다.

    이 함수는 절대 예외를 던지지 않는다(fail-open 계약). 어떤 이유로든 판정에
    실패하면 False(위반 아님, 허용)를 반환한다. 호출부(main.py)는 이 결과를
    그대로 믿고 asyncio.create_task 안에서 호출하면 된다.

    Returns:
        True면 위반 감지(발신자 제한 필요), False면 허용 또는 판정 불가.
    """
    if not content.strip():
        return False  # 빈 메시지는 애초에 저장되지 않지만, 방어적으로 처리.

    client = _get_client()
    if client is None:
        # API 키 미설정. fail-open.
        return False

    try:
        response = client.chat.completions.create(
            model=_MODEL,
            max_tokens=64,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
    except openai.APITimeoutError:
        # 네트워크/응답 지연. fail-open.
        print("[모더레이션] 요청 타임아웃 - fail-open으로 허용 처리")
        return False
    except openai.APIConnectionError as error:
        # 네트워크 자체가 안 되는 경우. fail-open.
        print(f"[모더레이션] 연결 실패({error}) - fail-open으로 허용 처리")
        return False
    except openai.APIStatusError as error:
        # 인증 실패(잘못된 키), rate limit, 서버 오류 등 API가 명시적으로 거부.
        print(f"[모더레이션] API 오류(status={error.status_code}) - fail-open으로 허용 처리")
        return False

    try:
        raw_text = response.choices[0].message.content
        return _parse_violation(raw_text)
    except (AttributeError, KeyError, TypeError, IndexError, ValueError, json.JSONDecodeError) as error:
        # 응답 구조가 예상과 다르거나 JSON이 아님(모델이 지시를 따르지 않음 등).
        # fail-open.
        print(f"[모더레이션] 응답 파싱 실패({error}) - fail-open으로 허용 처리")
        return False

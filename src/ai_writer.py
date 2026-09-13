"""Claude API 기반 Threads 게시글 생성.

설계 원칙
  1. Threads 는 조회가 아니라 '답글'로 도달이 결정된다.
     따라서 정보 전달형이 아니라 대화 유발형으로 쓴다.
  2. 홍보 문구를 본문에 넣지 않는다. 링크는 셀프 리플라이에 배치한다.
  3. 매번 다른 글이 나와야 한다. 템플릿 반복은 스팸으로 강등된다.
     -> 최근 발행 글을 프롬프트에 넣어 중복을 회피한다 (DB 불필요).
  4. 생성 실패는 발행 실패로 이어지면 안 된다.
     -> 실패 시 정적 텍스트 풀로 폴백한다.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass

import requests

from . import config

log = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class AiWriterError(RuntimeError):
    """생성 실패. 호출자는 정적 폴백으로 넘어가야 한다."""


@dataclass(frozen=True)
class Pillar:
    """콘텐츠 기둥. 무엇에 대해 쓸지를 결정한다."""

    key: str
    label: str
    brief: str
    seeds: tuple[str, ...]


# ---------------------------------------------------------------------------
# 콘텐츠 기둥 4종
#   BUILD  : 만드는 사람의 일상. 대화 유발력이 가장 높다.
#   MARKET : 시장을 보는 감각. 숫자·종목은 넣지 않는다.
#   STORY  : 작품 뒷이야기. 채널로 자연스럽게 연결된다.
#   PROMO  : 채널 안내. 본문은 여전히 대화형이어야 한다.
# ---------------------------------------------------------------------------

PILLARS: dict[str, Pillar] = {
    "BUILD": Pillar(
        key="BUILD",
        label="제작 일상",
        brief=(
            "혼자 자동화 시스템을 만들며 겪는 시행착오를 1인칭으로 쓴다. "
            "성공담보다 삽질과 사소한 깨달음이 좋다. "
            "기술 용어는 한두 개까지만, 비개발자도 읽히게."
        ),
        seeds=(
            "예상보다 오래 걸린 작업",
            "자동화했더니 오히려 늘어난 일",
            "새벽에 고친 버그",
            "만들기 전과 후의 생각이 달라진 지점",
            "포기하고 수동으로 남겨둔 부분",
            "이름 짓는 데 쓴 시간",
            "로그를 보다가 발견한 것",
            "직장 일과 개인 작업 사이의 전환",
        ),
    ),
    "MARKET": Pillar(
        key="MARKET",
        label="시장 관찰",
        brief=(
            "시장을 오래 지켜본 사람의 감각을 쓴다. "
            "구체적 종목, 가격, 수치, 전망은 절대 쓰지 않는다. "
            "판단이 아니라 관찰과 질문으로 끝낸다."
        ),
        seeds=(
            "모두가 같은 방향을 볼 때의 불안",
            "기록해두지 않으면 잊는 것들",
            "조용한 날에 하는 일",
            "같은 데이터, 다른 결론",
            "숫자보다 먼저 기억나는 분위기",
            "지나고 나서야 보이는 신호",
            "안 보기로 정한 지표",
            "판단을 미루는 기준",
        ),
    ),
    "STORY": Pillar(
        key="STORY",
        label="작품 뒷이야기",
        brief=(
            "시장 데이터를 히어로 배틀 만화로 옮기는 작업 이야기를 쓴다. "
            "캐릭터 설정, 각색하며 고민한 지점, 독자 반응 등. "
            "작품 홍보가 아니라 창작 과정 공유의 톤."
        ),
        seeds=(
            "캐릭터에 성격을 붙이는 기준",
            "데이터를 장면으로 바꿀 때 버리는 것",
            "의도와 다르게 읽힌 회차",
            "빌런이 이기는 날의 처리",
            "그림보다 어려운 대사",
            "연재를 하루도 안 빠뜨리는 방법",
            "가장 마음에 드는 컷",
            "설정을 바꾸고 싶어지는 순간",
        ),
    ),
    "PROMO": Pillar(
        key="PROMO",
        label="채널 안내",
        brief=(
            "매일 만화와 쇼츠를 올리고 있다는 사실을 자연스럽게 알린다. "
            "'구독해주세요' 같은 직접 요청은 절대 쓰지 않는다. "
            "왜 이걸 만들게 됐는지, 어떤 사람에게 맞는지를 담담하게 쓰고 질문으로 닫는다. "
            "링크는 본문에 넣지 않는다. 자동으로 답글에 붙는다."
        ),
        seeds=(
            "이걸 만들기 시작한 이유",
            "숫자보다 장면이 기억에 남는다는 발견",
            "매일 올리기로 정한 이유",
            "어떤 사람이 보면 좋을지",
            "혼자 만드는 것의 장단점",
            "처음과 달라진 방향",
        ),
    ),
}

# 8일 주기 기둥 배치. PROMO 25%, 나머지 75%.
# 연속으로 같은 기둥이 오지 않도록 배치했다.
PILLAR_ROTATION: tuple[str, ...] = (
    "BUILD", "MARKET", "PROMO", "STORY",
    "BUILD", "MARKET", "PROMO", "STORY",
)


SYSTEM_PROMPT = """당신은 한국어로 Threads(스레드)에 글을 쓰는 사람입니다.

# 화자 설정
- 15년차 금융권 백엔드 개발자. 낮에는 회사 일을 하고, 밤과 주말에 개인 프로젝트를 합니다.
- 개인 프로젝트: 미국 시장 데이터를 슈퍼히어로 배틀 만화로 바꿔 매일 발행하는 자동화 시스템.
- 성격: 담담하고 솔직함. 과장하지 않음. 자기 삽질을 숨기지 않음.

# Threads 플랫폼 특성 (반드시 지킬 것)
- 도달은 조회수가 아니라 '답글'로 결정됩니다. 정보 전달이 아니라 대화의 첫 문장을 쓰세요.
- 방송하듯 쓰면 실패합니다. 옆자리 동료에게 말 거는 톤으로 쓰세요.
- 매번 같은 형식으로 쓰면 스팸으로 강등됩니다. 문장 구조와 길이를 매번 바꾸세요.

# 형식
- 한국어. 존댓말(~습니다/~요 혼용 가능).
- 2~4문장. 전체 300자 이내. 짧을수록 좋습니다.
- 줄바꿈으로 호흡을 나눕니다.
- 마지막은 질문으로 닫습니다. 단, 매번 같은 형태의 질문은 금지.
- 해시태그, 이모지, 링크, URL을 절대 쓰지 않습니다.

# 절대 금지
- 투자 조언성 표현: 매수, 매도, 목표가, 추천주, 종목추천, 손절, 익절, 수익보장, 리딩
- 구체적 종목명, 가격, 수익률, 시장 전망
- 참여 유도 미끼: "댓글 남기면", "좋아요 누르면", "팔로우하면", "선착순", "1번 2번 골라"
- 직접 홍보 요청: "구독해주세요", "보러오세요", "많은 관심 부탁"
- AI가 썼다는 티가 나는 표현: "여러분", "~하는 것은 어떨까요", 과도한 대구법

# 출력
JSON 한 개만 출력합니다. 다른 말은 붙이지 마세요.
{"text": "본문"}"""


def _build_user_prompt(
    pillar: Pillar, seed: str, recent_texts: list[str]
) -> str:
    parts = [
        f"# 오늘의 주제 영역: {pillar.label}",
        pillar.brief,
        "",
        f"# 소재 힌트\n{seed}",
        "",
        "이 힌트는 방향만 잡는 용도입니다. 그대로 제목처럼 쓰지 말고,"
        " 구체적인 한 장면이나 한 순간으로 풀어내세요.",
    ]

    if recent_texts:
        joined = "\n".join(f"- {t[:80]}" for t in recent_texts[:8])
        parts += [
            "",
            "# 최근에 이미 발행한 글 (표현·구조·소재가 겹치지 않게 할 것)",
            joined,
        ]

    parts += ["", "위 조건으로 글 한 개를 써서 JSON으로만 출력하세요."]
    return "\n".join(parts)


def _extract_json(raw: str) -> dict:
    """모델 응답에서 JSON 객체를 뽑아낸다.

    본문에 줄바꿈이 들어가는 것이 정상이므로(시스템 프롬프트가 그렇게 지시한다),
    문자열 안의 raw 제어문자를 허용해야 한다. strict=True 로 파싱하면
    "Invalid control character" 로 실패한다.

    파싱 실패는 반드시 AiWriterError 로 감싼다. 그래야 호출자가 재시도하고,
    끝내 실패하면 정적 텍스트로 폴백할 수 있다.
    """
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1]
        if text.startswith("json"):
            text = text[4:]

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AiWriterError(f"JSON 형식이 아닙니다: {raw[:200]}")

    payload = text[start : end + 1]

    try:
        # strict=False : 문자열 안의 줄바꿈·탭 등 제어문자를 허용한다.
        return json.loads(payload, strict=False)
    except json.JSONDecodeError as exc:
        # 이스케이프되지 않은 따옴표 등으로 여전히 실패할 수 있다.
        # 마지막 수단으로 text 값만 정규식으로 회수한다.
        recovered = _recover_text_field(payload)
        if recovered:
            log.warning("JSON 파싱 실패 — text 필드만 회수했습니다: %s", exc)
            return {"text": recovered}
        raise AiWriterError(f"JSON 파싱 실패: {exc} / 원문: {payload[:200]}") from exc


def _recover_text_field(payload: str) -> str:
    """깨진 JSON 에서 text 값만 건져낸다.

    모델이 따옴표를 이스케이프하지 않는 경우가 있어 최후 수단으로 둔다.
    """
    match = re.search(r'"text"\s*:\s*"(.*)"\s*}\s*$', payload, re.DOTALL)
    if not match:
        return ""
    value = match.group(1)
    # 이스케이프 시퀀스를 실제 문자로 되돌린다.
    value = value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    return value.strip()


def generate(
    api_key: str,
    pillar_key: str,
    seed: str,
    recent_texts: list[str] | None = None,
    model: str | None = None,
) -> str:
    """Claude 로 게시글 본문을 생성한다. 실패 시 AiWriterError."""
    pillar = PILLARS.get(pillar_key)
    if pillar is None:
        raise AiWriterError(f"알 수 없는 기둥: {pillar_key}")

    payload = {
        "model": model or config.CLAUDE_MODEL,
        "max_tokens": 1000,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": _build_user_prompt(pillar, seed, recent_texts or []),
            }
        ],
    }

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            timeout=config.HTTP_TIMEOUT_SEC * 2,
        )
    except requests.RequestException as exc:
        raise AiWriterError(f"API 호출 실패: {exc}") from exc

    if resp.status_code != 200:
        raise AiWriterError(f"API {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    chunks = [
        block.get("text", "")
        for block in body.get("content", [])
        if block.get("type") == "text"
    ]
    if not chunks:
        raise AiWriterError(f"텍스트 블록 없음: {body}")

    parsed = _extract_json("".join(chunks))
    text = str(parsed.get("text", "")).strip()
    if not text:
        raise AiWriterError("본문이 비어 있습니다.")
    return text


def pick_seed(pillar_key: str, day_index: int) -> str:
    """날짜 기반으로 소재를 고른다. 같은 날 재실행 시 동일 결과(멱등)."""
    pillar = PILLARS[pillar_key]
    rng = random.Random(f"{pillar_key}-{day_index}")
    return rng.choice(pillar.seeds)


def pick_pillar(day_index: int) -> str:
    return PILLAR_ROTATION[day_index % len(PILLAR_ROTATION)]

"""무상태 콘텐츠 선택.

DB를 쓰지 않으므로 순번 컬럼 대신 날짜 결정론으로 로테이션한다.
같은 날 재실행하면 같은 결과가 나오므로 멱등하다.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import ai_writer, config

VERSION = "1.1.1"   # v1.1.1: lint_chat 일반어 오차단 수정

_log = logging.getLogger(__name__)


class PostKind(StrEnum):
    PROMO = "promo"
    OBSERVATION = "observation"


class ContentPolicyError(ValueError):
    """금칙어 또는 길이 위반."""


# ---------------------------------------------------------------------------
# 텍스트 풀
#  - 시장 수치는 넣지 않는다. 데이터 소스가 없는 상태에서 숫자를 쓰면 허위가 된다.
#  - 종목/목표가/매매권유 표현을 넣지 않는다.
#  - 방송형 문장 대신 대화의 첫 문장이 되도록 질문으로 닫는다.
# ---------------------------------------------------------------------------

PROMO_TEXTS: tuple[str, ...] = (
    "매일 미국 시장을 히어로 배틀 서사로 바꿔서 기록하고 있습니다.\n"
    "숫자만 보면 안 남는데, 캐릭터로 보면 이상하게 기억에 남더군요.\n"
    "같은 방식으로 시장 보시는 분 계신가요?",

    "시장 데이터를 캐릭터 대결로 옮기는 작업을 계속하고 있습니다.\n"
    "쓰다 보니 지표보다 서사가 먼저 기억에 남습니다.\n"
    "여러분은 시장을 어떤 방식으로 기록하시나요?",

    "장이 흔들린 날의 기록을 만화로 남기고 있습니다.\n"
    "차트는 지나가면 잊히는데 장면은 남습니다.\n"
    "기록 방식에 대한 의견 있으시면 듣고 싶습니다.",
)

OBSERVATION_TEXTS: tuple[str, ...] = (
    "지표가 전부 같은 방향을 가리키는 날이 제일 불안합니다.\n"
    "다들 편할 때가 오히려 이상하더군요.\n"
    "이런 날 뭘 먼저 확인하시나요?",

    "같은 데이터를 봐도 사람마다 다른 결론이 나옵니다.\n"
    "결론이 갈리는 지점은 대개 데이터가 아니라 전제였습니다.\n"
    "본인 전제를 어떻게 점검하시는지 궁금합니다.",

    "자동화를 붙일수록 판단이 편해질 줄 알았는데 반대였습니다.\n"
    "볼 게 늘어나니 뭘 안 볼지가 더 어려워졌습니다.\n"
    "정보량 줄이는 본인만의 기준이 있으신가요?",

    "장 끝나고 그날 판단을 다시 읽어보면 절반은 민망합니다.\n"
    "그래도 안 적어두면 같은 실수를 반복하더군요.\n"
    "기록 남기시는 분들은 어떤 형식으로 쓰시나요?",

    "숫자보다 그날의 분위기가 먼저 기억나는 날이 있습니다.\n"
    "그게 기억에는 좋은데 판단에는 나쁩니다.\n"
    "분위기와 데이터를 어떻게 분리하시나요?",

    "시장이 조용한 날에 뭘 해야 할지가 제일 어렵습니다.\n"
    "아무것도 안 하는 게 정답인 날이 분명히 있는데 그게 잘 안 됩니다.\n"
    "쉬는 날을 어떻게 정하시나요?",
)


@dataclass(frozen=True)
class PostPlan:
    kind: PostKind
    text: str
    image_url: str
    reply_text: str
    pillar: str = ""
    seed: str = ""
    source: str = "static"   # "ai" | "static"


def _day_index(today: dt.date) -> int:
    """날짜만의 인덱스. 하위 호환용으로 남긴다."""
    return today.timetuple().tm_yday


def run_index(today: dt.date, discriminator: int = 0) -> int:
    """콘텐츠 선택 인덱스.

    날짜 하나만 축으로 쓰면 같은 날 두 번 발행할 때 이미지·기둥·소재가
    전부 같아진다. 실행 구분자를 곱해 축을 하나 더 만든다.

    같은 날 같은 구분자로 재실행하면 동일 결과가 나온다(멱등성 유지).

    곱셈이 아니라 덧셈을 쓴다. day * N + disc 형태는 N 이 로테이션 길이의
    배수일 때 나머지가 항상 같아져, 한 슬롯이 같은 기둥만 뽑는 사고가 난다.
    덧셈은 로테이션 길이와 무관하게 안전하고, 고정 슬롯에서 하루 한 칸씩
    전진하는 기존 동작도 그대로 유지된다.
    """
    if not 0 <= discriminator < config.DISCRIMINATOR_MAX:
        raise ValueError(
            f"discriminator 는 0 이상 {config.DISCRIMINATOR_MAX} 미만이어야 합니다: "
            f"{discriminator}"
        )
    return _day_index(today) + discriminator


def discriminator_from_env() -> int:
    """환경변수에서 실행 구분자를 읽는다.

    정기 발행은 SLOT(A/B/C), 이벤트 발행은 EVENT_RUN=true 로 구분한다.
    """
    if os.environ.get("EVENT_RUN", "").strip().lower() in ("true", "1", "yes"):
        return config.DISCRIMINATOR_EVENT
    slot = os.environ.get("SLOT", "").strip().upper()
    return config.DISCRIMINATOR_BY_SLOT.get(slot, 0)


def pick_kind(today: dt.date, discriminator: int = 0) -> PostKind:
    """홍보형 1 : 관찰형 3 비율을 상태 없이 강제한다."""
    if run_index(today, discriminator) % config.PROMO_CYCLE == 0:
        return PostKind.PROMO
    return PostKind.OBSERVATION


def pick_text(kind: PostKind, today: dt.date, discriminator: int = 0) -> str:
    pool = PROMO_TEXTS if kind is PostKind.PROMO else OBSERVATION_TEXTS
    return pool[run_index(today, discriminator) % len(pool)]


def list_asset_names(assets_dir: Path) -> list[str]:
    names = sorted(
        p.name
        for p in assets_dir.iterdir()
        if p.suffix.lower() in (".png", ".jpg", ".jpeg")
    )
    if not names:
        raise FileNotFoundError(f"이미지 자산이 없습니다: {assets_dir}")
    return names


def order_asset_candidates(assets: list[str], day_index: int) -> list[str]:
    """오늘의 1순위부터 시작해 나머지를 순환 배열한다.

    1순위가 깨져 있어도 다음 후보로 넘어갈 수 있게 한다.
    """
    if not assets:
        return []
    start = day_index % len(assets)
    return assets[start:] + assets[:start]


def build_image_url(raw_base_url: str, asset_name: str) -> str:
    """레포의 raw URL을 그대로 쓴다. 별도 스토리지 비용 0원."""
    return f"{raw_base_url.rstrip('/')}/{asset_name}"


def build_reply_text() -> str:
    """링크는 본문이 아니라 셀프 리플라이에 배치한다."""
    return (
        "매일 올리는 곳입니다.\n"
        f"YouTube: {config.YOUTUBE_URL}\n"
        f"X: {config.X_URL}"
    )


def lint(text: str) -> None:
    """발행 직전 정책 검사. 위반 시 발행하지 않는다."""
    if len(text) > config.TEXT_MAX_LEN:
        raise ContentPolicyError(
            f"본문 {len(text)}자 — 상한 {config.TEXT_MAX_LEN}자 초과"
        )

    hit_advice = [t for t in config.FORBIDDEN_ADVICE_TERMS if t in text]
    if hit_advice:
        raise ContentPolicyError(f"투자조언성 금칙어 검출: {hit_advice}")

    hit_bait = [t for t in config.FORBIDDEN_BAIT_TERMS if t in text]
    if hit_bait:
        raise ContentPolicyError(f"인게이지먼트 베이트 표현 검출: {hit_bait}")


_DIGIT = re.compile(r"[0-9０-９]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z&.\-]*")


def _entity_suffix_re() -> re.Pattern[str]:
    """앞 글자(한글·영문·숫자)에 붙은 접미만 잡는다. '삼성전자'는 잡고 '전자 쪽'은 통과."""
    alt = "|".join(re.escape(s) for s in config.CHAT_ENTITY_SUFFIXES)
    return re.compile(rf"[가-힣A-Za-z0-9]+(?:{alt})")


def _entity_word_re() -> re.Pattern[str]:
    """뒤에 한글 2자 이상이 이어지면 다른 단어로 본다. '메타가' 차단, '메타버스' 통과."""
    alt = "|".join(re.escape(w) for w in config.CHAT_ENTITY_WORDS)
    return re.compile(rf"(?:{alt})(?![가-힣]{{2}})")


def lint_chat(text: str) -> None:
    """CHAT 전용 추가 검사. lint() 를 먼저 통과해야 한다.

    잡담은 '오늘 시장'을 말하므로 REG-03(수치·전망)·REG-04(기업·인물) 경계에
    가장 가깝다. 모델 지시만으로는 보장되지 않으므로 발행 직전에 기계적으로 막는다.
    """
    lint(text)

    if len(text) > config.CHAT_TEXT_MAX_LEN:
        raise ContentPolicyError(
            f"CHAT 본문 {len(text)}자 — 상한 {config.CHAT_TEXT_MAX_LEN}자 초과"
        )

    if _DIGIT.search(text):
        raise ContentPolicyError("CHAT 본문에 숫자 포함 (REG-03)")

    hit_forecast = [t for t in config.CHAT_FORECAST_TERMS if t in text]
    if hit_forecast:
        raise ContentPolicyError(f"시장 전망성 표현 검출: {hit_forecast}")

    hit_entity = [t for t in config.CHAT_ENTITY_TERMS if t in text]
    hit_entity += [m.group(0) for m in _entity_suffix_re().finditer(text)]
    hit_entity += [m.group(0) for m in _entity_word_re().finditer(text)]
    if hit_entity:
        raise ContentPolicyError(f"기업·인물명 검출 (REG-04): {hit_entity}")

    # 영문 단어는 허용목록(Fed, FOMC, CPI 등)만 통과. 티커·영문 기업명을 막는다.
    allow = {a.upper() for a in config.CHAT_THEME_ALLOWLIST}
    foreign = [w for w in _LATIN_WORD.findall(text) if w.upper() not in allow]
    if foreign:
        raise ContentPolicyError(f"허용목록 밖 영문 단어 검출: {foreign}")


def _generate_with_ai(
    api_key: str,
    pillar_key: str,
    seed: str,
    recent_texts: list[str],
    facts_block: str = "",
) -> str:
    """AI 생성 + 린트. 린트 실패 시 재시도. 모두 실패하면 예외."""
    last_error: Exception | None = None

    for attempt in range(1, config.AI_MAX_RETRY + 1):
        try:
            text = ai_writer.generate(
                api_key, pillar_key, seed, recent_texts, facts_block=facts_block
            )
            lint(text)
            return text
        except ContentPolicyError as exc:
            last_error = exc
            _log.warning("생성문 린트 실패 (%d/%d): %s",
                         attempt, config.AI_MAX_RETRY, exc)
        except ai_writer.AiWriterError as exc:
            last_error = exc
            _log.warning("생성 실패 (%d/%d): %s",
                         attempt, config.AI_MAX_RETRY, exc)

    raise ai_writer.AiWriterError(f"재시도 소진: {last_error}")


def build_plan(
    today: dt.date,
    assets_dir: Path,
    raw_base_url: str,
    *,
    claude_api_key: str = "",
    recent_texts: list[str] | None = None,
    facts_block: str = "",
    episode_block: str = "",
    discriminator: int | None = None,
) -> PostPlan:
    """오늘 발행할 게시물을 구성한다.

    AI 키가 있으면 생성문을, 없거나 실패하면 정적 텍스트 풀을 쓴다.
    어느 경로든 린트를 통과한 텍스트만 반환한다.
    """
    if discriminator is None:
        discriminator = discriminator_from_env()

    idx = run_index(today, discriminator)
    pillar_key = ai_writer.pick_pillar(idx)
    seed = ai_writer.pick_seed(pillar_key, idx)
    kind = PostKind.PROMO if pillar_key == "PROMO" else PostKind.OBSERVATION

    source = "static"
    text = ""

    if config.AI_ENABLED and claude_api_key:
        try:
            # 기둥마다 근거 종류가 다르다.
            #   BUILD -> 커밋 로그 / STORY -> 회차 기록 / 나머지 -> 근거 없음
            # 기둥마다 근거 종류가 다르다.
            #   STORY -> Notion 트래커 회차 기록 / 나머지 -> 근거 없음
            #   (BUILD 기둥 제거로 커밋 근거 경로는 현재 미사용)
            evidence = {"STORY": episode_block}.get(pillar_key, "")
            text = _generate_with_ai(
                claude_api_key, pillar_key, seed, recent_texts or [], evidence
            )
            source = "ai"
        except ai_writer.AiWriterError as exc:
            _log.warning("AI 생성 포기 — 정적 텍스트로 폴백: %s", exc)

    if not text:
        text = pick_text(kind, today, discriminator)
        lint(text)

    assets = list_asset_names(assets_dir)
    asset = assets[idx % len(assets)]
    reply_text = build_reply_text()
    lint(reply_text)

    return PostPlan(
        kind=kind,
        text=text,
        image_url=build_image_url(raw_base_url, asset),
        reply_text=reply_text,
        pillar=pillar_key,
        seed=seed,
        source=source,
    )

"""글·답글 문체 축 (v1.3.0).

같은 화자가 매번 같은 길이·같은 줄바꿈·같은 끝맺음으로 쓰면 그 자체가 봇 신호다.
글마다 문체 축을 결정론적으로 뽑아 프롬프트에 '# 이번 글 형식' 블록으로 넣는다.

  - 결정론: key(날짜·실행 구분자·댓글 ID 등) 해시로 뽑는다. 같은 실행 재시도는 같은 형식.
  - 무상태: 저장하지 않는다.
  - 어투는 존댓말 고정(마스터 결정 전 기본값 유지). 축은 길이·줄바꿈·마무리·되묻기뿐.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from . import config

VERSION = "1.0.0"

# 마무리 4종. ai_writer 의 CLOSING_* 와 같은 값.
QUESTION = "question"
STATEMENT = "statement"
TRAIL = "trail"
ASIDE = "aside"
CLOSINGS = (QUESTION, STATEMENT, TRAIL, ASIDE)

_LENGTH_TEXT = {
    "one": "한 문장으로 짧게 씁니다. 80자 이내.",
    "short": "두 문장으로 씁니다. 150자 이내.",
    "normal": "세 문장 안팎으로 씁니다.",
}
_LAYOUT_TEXT = {
    "lines": "문장마다 줄을 바꿉니다.",
    "block": "줄바꿈 없이 한 덩어리로 씁니다.",
    "mixed": "첫 문장 뒤에서만 한 번 줄을 바꿉니다.",
}
_REPLY_LENGTH_TEXT = {
    "tiny": "한 마디로 답합니다. 40자 이내.",
    "one": "한 문장으로 답합니다.",
    "two": "두 문장으로 답합니다.",
}


def _hash(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def weighted_pick(key: str, weights: tuple[tuple[str, int], ...]) -> str:
    """(이름, 가중치) 목록에서 key 해시로 하나를 고른다. 결정론."""
    total = sum(w for _, w in weights)
    if total <= 0:
        raise ValueError("가중치 합이 0 이하입니다.")
    point = _hash(key) % total
    for name, weight in weights:
        if point < weight:
            return name
        point -= weight
    return weights[-1][0]   # 도달 불가(방어)


@dataclass(frozen=True)
class PostStyle:
    length: str
    layout: str

    def block(self) -> str:
        # 한 문장이면 줄바꿈 지시는 의미가 없다.
        lines = ["# 이번 글 형식", _LENGTH_TEXT[self.length]]
        if self.length != "one":
            lines.append(_LAYOUT_TEXT[self.layout])
        return "\n".join(lines)


def pick_post_style(key: str, *, chat: bool = False) -> PostStyle:
    length_weights = config.CHAT_STYLE_LENGTH_WEIGHTS if chat else config.STYLE_LENGTH_WEIGHTS
    return PostStyle(
        length=weighted_pick(f"{key}::len", length_weights),
        layout=weighted_pick(f"{key}::layout", config.STYLE_LAYOUT_WEIGHTS),
    )


@dataclass(frozen=True)
class ReplyStyle:
    length: str
    may_ask: bool

    def block(self) -> str:
        ask = (
            "필요하면 짧게 되물어도 됩니다."
            if self.may_ask
            else "되묻지 않습니다. 물음표를 쓰지 않습니다."
        )
        return "\n".join(["# 이번 답글 형식", _REPLY_LENGTH_TEXT[self.length], ask])


def pick_reply_style(key: str) -> ReplyStyle:
    return ReplyStyle(
        length=weighted_pick(f"{key}::rlen", config.REPLY_LENGTH_WEIGHTS),
        may_ask=_hash(f"{key}::ask") % 100 < config.REPLY_ASK_PCT,
    )


# ---------------------------------------------------------------------------
# 반복 판정 (무상태 — 최근 글 목록은 Threads 에서 조회한 것을 받는다)
# ---------------------------------------------------------------------------

_TRAILING = re.compile(r"[\s.?!…~,·\"'”’)\]]+$")
_WORD = re.compile(r"\S+")


def first_word(text: str) -> str:
    """첫 어절. 구두점은 뗀다."""
    match = _WORD.search(text or "")
    if not match:
        return ""
    return _TRAILING.sub("", match.group(0))


def ending(text: str, size: int = 3) -> str:
    """끝맺음 서명: 끝 구두점을 뗀 마지막 size 글자."""
    stripped = _TRAILING.sub("", (text or "").strip())
    return stripped[-size:]


def repetition_issue(text: str, recent_texts: list[str]) -> str | None:
    """최근 글과 시작·끝이 반복되면 사유 문자열. 없으면 None."""
    recent = [t for t in recent_texts if t and t.strip()][: config.REPETITION_LOOKBACK]
    if not recent:
        return None
    head = first_word(text)
    if head and any(first_word(t) == head for t in recent):
        return f"첫 어절 반복 '{head}'"
    tail = ending(text)
    if tail:
        same = sum(1 for t in recent if ending(t) == tail)
        if same > config.REPETITION_ENDING_MAX:
            return f"끝맺음 반복 '{tail}' ({same}건)"
    return None

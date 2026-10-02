"""글·답글 문체 축 (v1.3.0).

같은 화자가 매번 같은 길이·같은 줄바꿈·같은 끝맺음으로 쓰면 그 자체가 봇 신호다.
글마다 문체 축을 결정론적으로 뽑아 프롬프트에 '# 이번 글 형식' 블록으로 넣는다.

  - 결정론: key(날짜·실행 구분자·댓글 ID 등) 해시로 뽑는다. 같은 실행 재시도는 같은 형식.
  - 무상태: 저장하지 않는다.
  - 어투는 존댓말 고정(마스터 결정 전 기본값 유지). 축은 길이·줄바꿈·마무리·되묻기뿐.

v1.1.0 (답글 고도화)
  - 답글 형식은 댓글 '내용'으로 범주를 먼저 정한다(리액션·짧은 댓글·질문·긴 댓글·보통).
    범주 안에서만 댓글 ID 해시로 길이·되묻기를 바꾼다. 같은 댓글은 항상 같은 형식(결정론).
  - 답글 길이의 기준은 이 모듈의 '# 이번 답글 형식' 블록 하나다(시스템 프롬프트는 이를 참조).
    REPLY_MAX_LEN(200)은 린트의 절대 상한(안전망)일 뿐 형식 기준이 아니다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from . import config

VERSION = "1.1.0"   # v1.1.0: 댓글 내용 기반 답글 형식(범주 → 해시)

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
# v1.1.0: 답글 길이 기준(자). 형식 블록 문구가 이 값에서 나온다. react 만 compose 가 강제한다.
REPLY_LENGTH_LIMITS: dict[str, int] = {
    "react": config.REPLY_REACTION_MAX_LEN,
    "tiny": 40,
    "one": 80,
    "two": 150,
}
_REPLY_LENGTH_TEXT = {
    "react": f"짧은 한 마디로 답합니다. {REPLY_LENGTH_LIMITS['react']}자 이내.",
    "tiny": f"한 마디로 답합니다. {REPLY_LENGTH_LIMITS['tiny']}자 이내.",
    "one": f"한 문장으로 답합니다. {REPLY_LENGTH_LIMITS['one']}자 이내.",
    "two": f"두 문장으로 답합니다. {REPLY_LENGTH_LIMITS['two']}자 이내.",
}

# 답글 범주(댓글 내용으로 결정)
KIND_REACTION = "reaction"   # ㅋㅋ·ㄹㅇ·대박 등 (reply_engine.is_reaction)
KIND_SHORT = "short"         # 문자 REPLY_SHORT_COMMENT_CHARS 이하
KIND_QUESTION = "question"   # 물음표 또는 의문 어미
KIND_LONG = "long"           # 문자 REPLY_LONG_COMMENT_CHARS 이상
KIND_NORMAL = "normal"
REPLY_KINDS = (KIND_REACTION, KIND_SHORT, KIND_QUESTION, KIND_LONG, KIND_NORMAL)

_KIND_TEXT = {
    KIND_REACTION: "가벼운 리액션 댓글입니다. 받아주는 한 마디만 씁니다.",
    KIND_SHORT: "짧은 댓글이니 짧게 받습니다.",
    KIND_QUESTION: "질문에 먼저 답합니다. 첫 문장이 곧 답이어야 합니다.",
    KIND_LONG: "댓글에서 가장 와닿은 한 가지에만 반응합니다.",
    KIND_NORMAL: "",
}
# 되묻기를 허용할 수 있는 범주. 나머지는 항상 되묻지 않는다.
_ASKABLE_KINDS = (KIND_NORMAL, KIND_LONG)


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
    kind: str = ""

    def block(self) -> str:
        ask = (
            "필요하면 짧게 되물어도 됩니다."
            if self.may_ask
            else "되묻지 않습니다. 물음표를 쓰지 않습니다."
        )
        lines = ["# 이번 답글 형식", _REPLY_LENGTH_TEXT[self.length]]
        if _KIND_TEXT.get(self.kind):
            lines.append(_KIND_TEXT[self.kind])
        lines.append(ask)
        return "\n".join(lines)


_CORE_STRIP = re.compile(r"[\s\W_]+")
# 의문 어미. 물음표 없이 끝나는 질문("어떻게 하시나요", "궁금하네요")을 잡는다. 보수적으로 둔다.
_QUESTION_TAIL = re.compile(
    r"(까요|나요|가요|는지요?|던가요?|인가요?|건가요?|을까|를까|할까|어때요?|뭔가요|"
    r"궁금(해요|합니다|하네요|하다|해서요)?)$"
)


def core_text(text: str) -> str:
    """공백·기호·이모지를 뺀 '문자'만. 길이 판정의 기준."""
    return _CORE_STRIP.sub("", text or "")


def is_question(text: str) -> bool:
    """물음표가 있거나 의문 어미로 끝나면 질문으로 본다."""
    raw = (text or "").strip()
    if "?" in raw or "？" in raw:
        return True
    return bool(_QUESTION_TAIL.search(_TRAILING.sub("", raw)))


def reply_kind(text: str, *, reaction: bool = False) -> str:
    """댓글 내용으로 답글 범주를 정한다. 우선순위: 리액션 > 질문 > 짧음 > 김 > 보통.

    질문은 길이보다 우선한다. "왜요?" 처럼 짧아도 답이 필요하다.
    """
    if reaction:
        return KIND_REACTION
    if is_question(text):
        return KIND_QUESTION
    size = len(core_text(text))
    if size <= config.REPLY_SHORT_COMMENT_CHARS:
        return KIND_SHORT
    if size >= config.REPLY_LONG_COMMENT_CHARS:
        return KIND_LONG
    return KIND_NORMAL


def pick_reply_style(key: str, comment_text: str = "", *, reaction: bool = False) -> ReplyStyle:
    """댓글 내용으로 범주를 정하고, 범주 안에서만 key(댓글 ID) 해시로 변주한다.

    같은 댓글(같은 ID·같은 본문)은 재시도해도 같은 형식이다(결정론).
    """
    kind = reply_kind(comment_text, reaction=reaction)
    length = weighted_pick(f"{key}::rlen", config.REPLY_LENGTH_WEIGHTS_BY_KIND[kind])
    may_ask = kind in _ASKABLE_KINDS and _hash(f"{key}::ask") % 100 < config.REPLY_ASK_PCT
    return ReplyStyle(length=length, may_ask=may_ask, kind=kind)


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

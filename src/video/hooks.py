"""오프닝 훅 유형 (0~3초에 스크롤을 멈추게 하는 첫 문장).

유형·길이 제약·연출 지시는 investment_comic_tube src/hooks.py(운영 중)와 같은 체계다.
차이: threads-promo 규제 원칙(REG-02·REG-03)을 따른다.
  - 숫자·지표값을 넣지 않는다(YouTube 폴백은 VIX·금리 수치를 썼지만 여기서는 쓰지 않는다).
  - 매매를 부추기는 경고(예: 계좌 손실 단정)는 쓰지 않는다. A 유형 지시문을 그에 맞게 고쳤다.
"""

from __future__ import annotations

import logging

VERSION = "1.2.0"   # v1.8.4: 모든 훅 낭독을 빠르게 · v1.8.3: 빠른 훅(8~14자)·분위기 훅

log = logging.getLogger(__name__)

HOOK_A = "A"  # 경고형
HOOK_B = "B"  # 빌런 대결형
HOOK_C = "C"  # 반전/팩트형
HOOK_D = "D"  # 긴급 속보형
HOOK_E = "E"  # 질문형(v1.8.3 — 차분한 날의 훅 변화)
HOOK_TYPES: tuple[str, ...] = (HOOK_A, HOOK_B, HOOK_C, HOOK_D, HOOK_E)

# v1.8.3 마스터 요청 "빠른 훅": 8~14자(낭독 약 1.2~2초). 이전 12~18자(약 2.6초, YouTube hooks.py 기준).
HOOK_MIN_CHARS = 8
HOOK_MAX_CHARS = 14
URGENT_PREFIX = "[긴급]"

HOOK_SPECS: dict[str, dict[str, str]] = {
    HOOK_A: {
        "name": "경고형",
        "guide": "시장의 긴장 신호를 짧게 경고한다. 매매를 권하거나 손실을 단정하지 않는다.",
        "example": "경고등이 켜졌다",
        "tts_tone": "빠르고 또렷하게, 높은 에너지로 경고하듯 짧게 끊어 외치는 톤",
        "sfx": "hook_a",
    },
    HOOK_B: {
        "name": "빌런 대결형",
        "guide": "빌런이 방금 나타나 방어선이 뚫리기 직전인 충돌 상황을 선언한다.",
        "example": "방어선이 뚫리기 직전이다",
        "tts_tone": "빠르고 또렷하게, 묵직한 저음으로 결전을 알리듯 짧게 끊는 톤",
        "sfx": "hook_b",
    },
    HOOK_C: {
        "name": "반전/팩트형",
        "guide": "대중의 상식을 뒤엎는 한 줄. 궁금증을 남기고 답을 주지 않는다.",
        "example": "모두가 놓친 신호",
        "tts_tone": "빠르고 또렷하게, 마지막 어절을 강하게 찍는 톤",
        "sfx": "hook_c",
    },
    HOOK_D: {
        "name": "긴급 속보형",
        "guide": "유니버스 긴급 상황 선언. 반드시 '[긴급]' 으로 시작한다.",
        "example": "[긴급] 방어선 위기",
        "tts_tone": "빠르고 또렷하게, 속보 아나운서처럼 높은 긴장감으로",
        "sfx": "hook_d",
    },
    # v1.8.3: 차분한 날(안도·낙관)에 A·D 를 빼면 GOC 는 C 만 남아 하루 내내 같은 훅이 됐다(리뷰). 질문형을 더한다.
    #   효과음 파일(hook_e)이 따로 없으므로 기존 전환음(hook_c)을 쓴다.
    HOOK_E: {
        "name": "질문형",
        "guide": "짧은 질문 하나로 궁금증을 연다. 답·전망·매매 권유는 넣지 않는다.",
        "example": "지금 뭘 지켜야 할까",
        "tts_tone": "빠르고 또렷하게, 끝을 살짝 올려 묻는 톤",
        "sfx": "hook_c",
    },
}

# 빌런 → 1순위 훅 유형 (YouTube 와 같은 대응)
VILLAIN_PREFERRED_HOOK = {
    "Chaos Reaper": HOOK_A,
    "Debt Titan": HOOK_B,
    "Bull Brute": HOOK_C,
}

# 포맷 → 1순위 훅 유형. F1 은 빌런 기준, 나머지는 포맷 성격 기준.
FORMAT_PREFERRED_HOOK = {"F2": HOOK_C, "F3": HOOK_D}


# GOC 영상에는 빌런이 등장하지 않으므로 빌런 대결형(B)을 쓰지 않는다.
NO_VILLAIN_HOOK_TYPES: tuple[str, ...] = (HOOK_A, HOOK_C, HOOK_D, HOOK_E)

# v1.8.3 운영 베타 C1: 분위기가 안도·낙관인데 경고형 훅("경고등이 켜졌다")이 나와 본문과 모순됐다.
#   차분한 분위기에서는 경고·긴급 유형을 고르지 않는다(분위기 어휘는 mood_source.MOOD_WORDS).
CALM_MOODS: tuple[str, ...] = ("안도", "낙관")
ALARM_HOOK_TYPES: tuple[str, ...] = (HOOK_A, HOOK_D)


def select_hook_type(
    fmt: str,
    villain: str | None,
    avoid: set[str] | None = None,
    allowed: tuple[str, ...] = HOOK_TYPES,
    mood_word: str = "",
) -> str:
    """훅 유형 선택. avoid(같은 날 이미 쓴 유형)와 겹치면 다음 유형으로 민다. allowed 밖 유형은 고르지 않는다.

    mood_word 가 차분한 분위기(CALM_MOODS)면 경고·긴급 유형(ALARM_HOOK_TYPES)을 허용 목록에서 뺀다.
    """
    if mood_word in CALM_MOODS:
        allowed = tuple(t for t in allowed if t not in ALARM_HOOK_TYPES) or allowed
    chosen = FORMAT_PREFERRED_HOOK.get(fmt) or VILLAIN_PREFERRED_HOOK.get(villain or "", HOOK_A)
    used = avoid or set()
    idx = HOOK_TYPES.index(chosen)
    picked = None
    for step in range(len(HOOK_TYPES)):
        candidate = HOOK_TYPES[(idx + step) % len(HOOK_TYPES)]
        if candidate in allowed and candidate not in used:
            picked = candidate
            break
    if picked is None:   # 허용 유형을 이미 다 썼으면 중복을 허용한다(허용 범위는 지킨다)
        picked = next(t for t in HOOK_TYPES[idx:] + HOOK_TYPES[:idx] if t in allowed)
    chosen = picked
    log.info("훅 유형 %s(%s) fmt=%s villain=%s 분위기=%s", chosen, HOOK_SPECS[chosen]["name"], fmt, villain,
             mood_word or "-")
    return chosen


def hook_issue(line: str, hook_type: str) -> str | None:
    """훅 문장 제약 위반 사유. 없으면 None."""
    text = (line or "").strip()
    if not text:
        return "훅 문장 없음"
    if not HOOK_MIN_CHARS <= len(text) <= HOOK_MAX_CHARS:
        return f"훅 {len(text)}자 — {HOOK_MIN_CHARS}~{HOOK_MAX_CHARS}자 필요"
    if hook_type == HOOK_D and not text.startswith(URGENT_PREFIX):
        return f"D 유형은 '{URGENT_PREFIX}' 로 시작해야 함"
    return None

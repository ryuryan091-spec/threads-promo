"""숏폼 대본 생성 — Claude(CLAUDE_AI_KEY) · JSON 계약 · 기계 검증.

구성: 훅 1 + 본문 7 + 정리 1 = 9 비트(약 55~60초). 이미지 5장을 비트에 재사용한다.
검증은 발행물 기준(content.lint_shorts: REG-02·REG-03·REG-04·링크·영문 허용목록)과
길이 제약으로 한다. 실패하면 재생성, 끝내 실패하면 그 편은 만들지 않는다(정적 폴백 없음).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import requests

from .. import ai_writer, config, content, mood_source
from . import hooks

VERSION = "1.0.0"

log = logging.getLogger(__name__)

BEAT_COUNT = 9                      # 0=훅, 1~7=본문, 8=정리
BODY_BEATS = 7
# 비트 → 이미지 슬롯(0~4). 같은 이미지는 Ken Burns 줌 방향을 바꿔 재사용한다.
BEAT_IMAGE_SLOT: tuple[int, ...] = (0, 1, 1, 2, 2, 3, 3, 4, 4)
BODY_MIN_CHARS, BODY_MAX_CHARS = 25, 48
CLOSING_MIN_CHARS, CLOSING_MAX_CHARS = 15, 35
TOTAL_MIN_CHARS, TOTAL_MAX_CHARS = 300, 400   # 낭독 초당 6~7자 추정 기준 약 50~60초
CAPTION_MIN_CHARS = 40
CAPTION_BODY_MAX = 200

VILLAINS = ("Debt Titan", "Chaos Reaper", "Bull Brute")
VILLAIN_KR = {"Debt Titan": "뎁트타이탄", "Chaos Reaper": "카오스리퍼", "Bull Brute": "불브루트"}

# 캐릭터별 설정. Facebook 영상은 GOC 만 등장(마스터 결정 2026-10-04), Threads 단독 영상은 EDT.
#   GOC 외형 묘사는 investment_comic_tube image_generator.py GOC 트랙 문구와 같은 내용이다.
CHARACTER_PROFILES: dict[str, dict] = {
    config.CHARACTER_EDT: {
        "intro": ("주인공은 'EDT' — 체인소를 든 의인화 호랑이 히어로. 빌런: 뎁트타이탄(금리·부채 압박), "
                  "카오스리퍼(변동성·공포), 불브루트(과열된 상승)."),
        "uses_villain": True,
        "forbidden_names": (config.CHARACTER_GOC,),
        "formats": {
            "F1": ("EDT 시장 서사",
                   "호랑이 히어로 EDT 가 오늘 시장 분위기를 상징하는 빌런과 맞선다. "
                   "본문은 대결 장면 묘사와 화제 테마의 의미를 번갈아 말한다. 결말은 여운을 남긴다."),
            "F2": ("개념 해설",
                   "화제 테마 중 하나의 개념을 EDT 가 쉽게 풀어 준다. 정의 → 왜 지금 화제인지 → 일상 비유 순서. "
                   "가르치려 들지 말고 옆에서 설명하듯."),
            "F3": ("이번 주 관전 포인트",
                   "화제 테마들 가운데 이번 주에 지켜볼 흐름을 EDT 가 짚는다. 날짜·수치는 쓰지 않는다. "
                   "무엇을 볼지만 말하고 어떻게 될지는 말하지 않는다."),
        },
    },
    config.CHARACTER_GOC: {
        "intro": ("주인공은 'GOC'(Guardian of Capital) — 자본을 지키는 수호자 히로인. 금발에 푸른 눈, "
                  "흰색과 금색의 장식 갑옷, 커다란 흰 깃털 날개, 짙은 붉은 망토. "
                  "이 영상에는 GOC 혼자만 등장한다. EDT 와 빌런(뎁트타이탄·카오스리퍼·불브루트)은 "
                  "이름도 모습도 나오지 않는다. 싸움이 아니라 지키고 살피는 시선으로 말한다."),
        "uses_villain": False,
        "forbidden_names": (config.CHARACTER_EDT, *VILLAIN_KR.values()),
        "formats": {
            "F1": ("GOC 수호 서사",
                   "GOC 가 오늘 시장 분위기를 높은 곳에서 내려다보며 자본을 지키는 자세를 이야기한다. "
                   "본문은 수호 장면 묘사와 화제 테마의 의미를 번갈아 말한다. 결말은 여운을 남긴다."),
            "F2": ("개념 해설",
                   "화제 테마 중 하나의 개념을 GOC 가 쉽게 풀어 준다. 정의 → 왜 지금 화제인지 → 일상 비유 순서. "
                   "가르치려 들지 말고 옆에서 설명하듯."),
            "F3": ("이번 주 관전 포인트",
                   "화제 테마들 가운데 이번 주에 지켜볼 흐름을 GOC 가 짚는다. 날짜·수치는 쓰지 않는다. "
                   "무엇을 볼지만 말하고 어떻게 될지는 말하지 않는다."),
        },
    },
}
# 하위 호환(기존 참조): EDT 포맷 안내
FORMAT_GUIDES = CHARACTER_PROFILES[config.CHARACTER_EDT]["formats"]


def system_prompt(character: str = config.CHARACTER_EDT) -> str:
    profile = CHARACTER_PROFILES[character]
    return f"""당신은 60초 세로 숏폼의 한국어 내레이션 작가입니다.
{profile["intro"]}

# 절대 규칙 (어기면 폐기됩니다)
- 숫자(아라비아 숫자)를 쓰지 않습니다. 가격·지수·퍼센트·날짜 모두 금지.
- 실존 기업·인물·브랜드·티커를 쓰지 않습니다.
- 매수·매도·목표가·종목추천 등 투자 조언, 시장 방향 예측(오른다/떨어진다)을 쓰지 않습니다.
- 링크·해시태그·@계정·이모지를 쓰지 않습니다.
- 영문은 {character} 와 다음 단어만 허용: {", ".join(config.CHAT_THEME_ALLOWLIST)}
- 다음 이름은 쓰지 않습니다: {", ".join(profile["forbidden_names"])}
- '여러분', 줄표(—)를 쓰지 않습니다.
- 근거로 준 테마 밖의 사건을 지어내지 않습니다.

# 출력
JSON 하나만 출력합니다. 다른 텍스트 금지.
{{"hook": "...", "body": ["...", ... 7개], "closing": "...",
  "image_prompts": ["영문 장면 묘사", ... 5개], "post_caption": "..."}}
- hook: {hooks.HOOK_MIN_CHARS}~{hooks.HOOK_MAX_CHARS}자 한 문장
- body: 정확히 {BODY_BEATS}개, 각 {BODY_MIN_CHARS}~{BODY_MAX_CHARS}자, 말로 읽기 좋은 한 문장
- closing: {CLOSING_MIN_CHARS}~{CLOSING_MAX_CHARS}자
- 내레이션 전체(hook+body+closing) {TOTAL_MIN_CHARS}~{TOTAL_MAX_CHARS}자
- image_prompts: 정확히 5개, 영어, 장면 묘사만(글자·숫자·로고 요구 금지){"" if profile["uses_villain"] else ", 등장인물은 GOC 한 명뿐"}
- post_caption: 게시글 설명 {CAPTION_MIN_CHARS}~{CAPTION_BODY_MAX}자, 담담한 말투, 질문으로 끝내지 않아도 됨
"""


SYSTEM_PROMPT = system_prompt(config.CHARACTER_EDT)


class ScriptError(RuntimeError):
    """대본 생성 실패(재시도 소진)."""


@dataclass(frozen=True)
class Beat:
    narration: str
    tone: str = ""
    is_hook: bool = False
    sfx: str = ""


@dataclass(frozen=True)
class Script:
    content_id: str
    fmt: str
    villain: str | None                # GOC 영상은 None(빌런 없음)
    hook_type: str
    beats: tuple[Beat, ...]
    image_prompts: tuple[str, ...]
    caption: str                       # AI 고지 포함 최종 캡션
    themes: tuple[str, ...] = field(default_factory=tuple)
    character: str = config.CHARACTER_EDT

    def to_dict(self) -> dict:
        return {
            "content_id": self.content_id,
            "character": self.character,
            "fmt": self.fmt,
            "villain": self.villain,
            "hook_type": self.hook_type,
            "beats": [b.__dict__ for b in self.beats],
            "image_prompts": list(self.image_prompts),
            "caption": self.caption,
            "themes": list(self.themes),
        }


def select_villain(mood: mood_source.Mood) -> str:
    """시장 분위기 → 빌런. 대응 규칙은 설계 결정(DESIGN_V17_SHORTS.md §4)이다.

    금리·국채·채권 테마가 있으면 뎁트타이탄, 긴장·경계면 카오스리퍼, 낙관·안도면 불브루트,
    그 밖(관망·혼조·근거 없음)은 뎁트타이탄.
    """
    if any(t in ("금리", "국채", "채권") for t in mood.themes):
        return "Debt Titan"
    if mood.mood_word in ("긴장", "경계"):
        return "Chaos Reaper"
    if mood.mood_word in ("낙관", "안도"):
        return "Bull Brute"
    return "Debt Titan"


def _user_prompt(fmt: str, villain: str | None, hook_type: str, mood: mood_source.Mood,
                 avoid_hooks: list[str], character: str = config.CHARACTER_EDT) -> str:
    name, guide = CHARACTER_PROFILES[character]["formats"][fmt]
    spec = hooks.HOOK_SPECS[hook_type]
    lines = [
        f"# 포맷: {name}",
        guide,
        f"# 오늘의 빌런: {VILLAIN_KR[villain]}" if villain else "# 빌런 없음 — GOC 혼자 등장",
        f"# 훅 유형: {spec['name']} — {spec['guide']} (예: {spec['example']})",
        mood.to_prompt_block() or "# 근거 없음 — 특정 사건을 지어내지 말고 시장을 보는 태도만 말한다.",
    ]
    if avoid_hooks:
        lines.append("# 같은 날 이미 쓴 훅(겹치지 않게): " + " / ".join(avoid_hooks))
    return "\n".join(lines)


def _call_claude(api_key: str, user_prompt: str, character: str = config.CHARACTER_EDT) -> dict:
    payload = {
        "model": config.CLAUDE_MODEL,
        "max_tokens": 1500,
        "system": system_prompt(character),
        "messages": [{"role": "user", "content": user_prompt}],
    }
    try:
        resp = requests.post(
            ai_writer.ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ai_writer.ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            timeout=config.HTTP_TIMEOUT_SEC * 3,
        )
    except requests.RequestException as exc:
        raise ScriptError(f"Claude 호출 실패: {exc}") from exc
    if resp.status_code != 200:
        raise ScriptError(f"Claude API {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    try:
        return ai_writer._extract_json(text)
    except ai_writer.AiWriterError as exc:
        raise ScriptError(str(exc)) from exc


# 이미지 프롬프트(영문)에서 막을 다른 캐릭터 표현. GOC 영상에 EDT·호랑이·빌런이 그려지지 않게 한다.
IMAGE_FORBIDDEN = {
    config.CHARACTER_EDT: ("goc", "guardian of capital"),
    config.CHARACTER_GOC: ("edt", "tiger", "chainsaw", *(v.lower() for v in VILLAINS)),
}


def validate(raw: dict, hook_type: str, used_captions: set[str] | None = None,
             character: str = config.CHARACTER_EDT) -> list[str]:
    """대본 JSON 위반 목록. 빈 목록이면 통과.

    used_captions: 같은 날 앞 편의 캡션(본문). 같으면 게시 단계 중복 검사에 걸려 그 편이 빠지므로 여기서 막는다.
    character: 다른 캐릭터 이름(대사·캡션)과 다른 캐릭터 묘사(이미지 프롬프트)를 막는다.
    """
    profile = CHARACTER_PROFILES[character]
    issues: list[str] = []
    hook = str(raw.get("hook", "")).strip()
    body = [str(x).strip() for x in (raw.get("body") or [])]
    closing = str(raw.get("closing", "")).strip()
    prompts = [str(x).strip() for x in (raw.get("image_prompts") or [])]
    caption = str(raw.get("post_caption", "")).strip()

    issue = hooks.hook_issue(hook, hook_type)
    if issue:
        issues.append(issue)
    if len(body) != BODY_BEATS:
        issues.append(f"본문 {len(body)}개 — {BODY_BEATS}개 필요")
    for i, line in enumerate(body, start=1):
        if not BODY_MIN_CHARS <= len(line) <= BODY_MAX_CHARS:
            issues.append(f"본문{i} {len(line)}자 — {BODY_MIN_CHARS}~{BODY_MAX_CHARS}자 필요")
    if not CLOSING_MIN_CHARS <= len(closing) <= CLOSING_MAX_CHARS:
        issues.append(f"정리 {len(closing)}자 — {CLOSING_MIN_CHARS}~{CLOSING_MAX_CHARS}자 필요")
    total = len(hook) + sum(len(x) for x in body) + len(closing)
    if not TOTAL_MIN_CHARS <= total <= TOTAL_MAX_CHARS:
        issues.append(f"내레이션 합계 {total}자 — {TOTAL_MIN_CHARS}~{TOTAL_MAX_CHARS}자 필요")
    if len(prompts) != config.SHORTS_IMAGE_COUNT or any(not p for p in prompts):
        issues.append(f"이미지 프롬프트 {len(prompts)}개 — {config.SHORTS_IMAGE_COUNT}개 필요")
    if not CAPTION_MIN_CHARS <= len(caption) <= CAPTION_BODY_MAX:
        issues.append(f"캡션 {len(caption)}자 — {CAPTION_MIN_CHARS}~{CAPTION_BODY_MAX}자 필요")
    if caption and caption in (used_captions or set()):
        issues.append("캡션이 같은 날 다른 편과 같음")
    texts = [hook, *body, closing, caption]
    for name in profile["forbidden_names"]:
        if any(name in t for t in texts):
            issues.append(f"{character} 영상에 다른 캐릭터 이름 '{name}' 포함")
    for word in IMAGE_FORBIDDEN[character]:
        if any(word in p.lower() for p in prompts):
            issues.append(f"{character} 영상 이미지 프롬프트에 '{word}' 포함")

    for label, line, max_len in (
        [("훅", hook, hooks.HOOK_MAX_CHARS)]
        + [(f"본문{i}", x, BODY_MAX_CHARS) for i, x in enumerate(body, start=1)]
        + [("정리", closing, CLOSING_MAX_CHARS), ("캡션", caption, CAPTION_BODY_MAX)]
    ):
        if not line:
            continue
        try:
            content.lint_shorts(line, max_len=max(max_len, len(line)), label=label,
                                extra_latin=(character,))
        except content.ContentPolicyError as exc:
            issues.append(f"{label} 정책 위반: {exc}")
    return issues


def build_caption(post_caption: str) -> str:
    """게시 캡션 = 본문 + AI 고지. 고지는 검증된 고정 문구다."""
    return f"{post_caption.strip()}\n\n{config.SHORTS_AI_NOTICE}"


def write_script(
    api_key: str,
    *,
    content_id: str,
    fmt: str,
    mood: mood_source.Mood,
    used_hook_types: set[str] | None = None,
    used_hooks: list[str] | None = None,
    used_captions: set[str] | None = None,
    extra_instruction: str = "",
    character: str = config.CHARACTER_EDT,
) -> Script:
    """대본 생성. 검증 실패 시 위반 사유를 붙여 재시도. 소진하면 ScriptError.

    extra_instruction: 렌더 길이 초과 등 앞 단계 실패 사유(재생성 지시)를 프롬프트 끝에 붙인다.
    """
    if character not in CHARACTER_PROFILES:
        raise ScriptError(f"알 수 없는 캐릭터: {character}")
    profile = CHARACTER_PROFILES[character]
    if fmt not in profile["formats"]:
        raise ScriptError(f"알 수 없는 포맷: {fmt}")
    villain = select_villain(mood) if profile["uses_villain"] else None
    allowed = hooks.HOOK_TYPES if profile["uses_villain"] else hooks.NO_VILLAIN_HOOK_TYPES
    hook_type = hooks.select_hook_type(fmt, villain, used_hook_types, allowed)
    base_prompt = _user_prompt(fmt, villain, hook_type, mood, list(used_hooks or []), character)
    if extra_instruction:
        base_prompt += f"\n\n# 추가 지시(앞 시도 실패)\n{extra_instruction}"
    prompt = base_prompt
    last: list[str] = []
    for attempt in range(1, config.SHORTS_SCRIPT_ATTEMPTS + 1):
        try:
            raw = _call_claude(api_key, prompt, character)
        except ScriptError as exc:
            last = [str(exc)]
            log.warning("대본 생성 실패 (%d/%d): %s", attempt, config.SHORTS_SCRIPT_ATTEMPTS, exc)
            continue
        last = validate(raw, hook_type, used_captions, character)
        if not last:
            spec = hooks.HOOK_SPECS[hook_type]
            beats = [Beat(str(raw["hook"]).strip(), spec["tts_tone"], True, spec["sfx"])]
            beats += [Beat(str(x).strip()) for x in raw["body"]]
            beats.append(Beat(str(raw["closing"]).strip()))
            if used_captions is not None:
                used_captions.add(str(raw["post_caption"]).strip())
            log.info("대본 생성 완료 id=%s 캐릭터=%s fmt=%s 빌런=%s 훅=%s 시도=%d",
                     content_id, character, fmt, villain or "-", hook_type, attempt)
            return Script(
                content_id=content_id,
                fmt=fmt,
                villain=villain,
                hook_type=hook_type,
                beats=tuple(beats),
                image_prompts=tuple(str(p).strip() for p in raw["image_prompts"]),
                caption=build_caption(str(raw["post_caption"])),
                themes=mood.themes,
                character=character,
            )
        log.warning("대본 검증 실패 (%d/%d): %s", attempt, config.SHORTS_SCRIPT_ATTEMPTS,
                    json.dumps(last, ensure_ascii=False))
        prompt = base_prompt + "\n\n# 직전 출력의 위반 사항(모두 고칠 것)\n- " + "\n- ".join(last)
    raise ScriptError(f"대본 재시도 소진: {last}")

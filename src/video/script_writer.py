"""숏폼 대본 생성 — Claude(CLAUDE_AI_KEY) · JSON 계약 · 기계 검증.

구성: 훅 1 + 본문 7 + 정리 1 = 9 비트(약 55~60초). 이미지 5장을 비트에 재사용한다.
검증은 발행물 기준(content.lint_shorts: REG-02·REG-03·REG-04·링크·영문 허용목록)과
길이 제약으로 한다. 실패하면 재생성, 끝내 실패하면 그 편은 만들지 않는다(정적 폴백 없음).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field

import requests

from .. import ai_writer, config, content, face_story, mood_source
from . import hooks

VERSION = "1.4.0"   # v1.8.5: 1인칭·전언형 오탐 축소·마지막 시도 문체 경고 통과·호출 사용량 로그 · v1.8.4: 이미지 7장·교차 배치·3인칭·전언형 금지 · v1.8.3: 결과 단정 금지·요일 맥락·주말 표현 검사·장면 다양화 · v1.8.2: max_tokens · v1.8.0: Facebook 연속성 계약(continuity) — 요청이 없으면 v1.0.0 과 같은 계약

log = logging.getLogger(__name__)

BEAT_COUNT = 9                      # 0=훅, 1~7=본문, 8=정리
BODY_BEATS = 7
# 비트 → 이미지 슬롯. v1.8.4(Q2-a·b): 이미지 7장, 비트마다 그림이 바뀌게 교차 배치한다.
#   이전 (0,1,1,2,2,3,3,4,4) 은 같은 그림이 연속 두 비트(약 12~14초) 이어졌다(운영 베타 2026-10-04).
#   다시 쓰는 그림(2·4)은 서로 떨어진 비트에만 나온다.
BEAT_IMAGE_SLOT: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 2, 4)
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
                   "호랑이 히어로 EDT 가 요즘 시장 분위기를 상징하는 빌런과 맞선다. "
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
                   "GOC 가 요즘 시장 분위기를 높은 곳에서 내려다보며 자본을 지키는 자세를 이야기한다. "
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
- 근거 테마는 '요즘 화제'일 뿐 결과가 아닙니다. 시장·지표의 결과를 사실로 단정하지 않습니다
  (금지 예: '고용 지표가 안정적으로 나왔다', '오늘 시장은 잔잔했다', '금리가 올랐다').
  상황은 {character} 의 시선·태도·질문으로만 그립니다.
- 훅은 오늘 분위기와 어긋나지 않게 씁니다(차분한 날에 경고·긴급처럼 쓰지 않습니다).
- '~라는 이야기가 들린다', '~라는 말이 들려온다', '~라고 한다'처럼 전해 들은 말로 시장 반응·결과를
  암시하지 않습니다(단정 금지를 돌려 말하는 것도 금지).
- 내레이션은 3인칭 관찰자 시점으로 통일합니다('나는·나의·내가·저는·제가' 금지).
  {character} 의 말이 꼭 필요하면 큰따옴표 대사 한 문장만 씁니다.

# 출력
JSON 하나만 출력합니다. 다른 텍스트 금지.
{{"hook": "...", "body": ["...", ... 7개], "closing": "...",
  "image_prompts": ["영문 장면 묘사", ... {config.SHORTS_IMAGE_COUNT}개], "post_caption": "..."}}
- hook: {hooks.HOOK_MIN_CHARS}~{hooks.HOOK_MAX_CHARS}자 한 문장. 짧고 빠르게 — 첫 1~2초 안에 끝나는 말
- body: 정확히 {BODY_BEATS}개, 각 {BODY_MIN_CHARS}~{BODY_MAX_CHARS}자, 말로 읽기 좋은 한 문장
- closing: {CLOSING_MIN_CHARS}~{CLOSING_MAX_CHARS}자
- 내레이션 전체(hook+body+closing) {TOTAL_MIN_CHARS}~{TOTAL_MAX_CHARS}자
- image_prompts: 정확히 {config.SHORTS_IMAGE_COUNT}개, 영어, 장면 묘사만(글자·숫자·로고 요구 금지).
  {config.SHORTS_IMAGE_COUNT}장은 구도·자세·배경이 확연히 달라야 한다(예: 얼굴 클로즈업 / 아주 먼 원경 / 날아오르는 동작 /
  뒷모습 / 낮은 각도 전신). 서 있는 정면 전신을 반복하지 않는다{"" if profile["uses_villain"] else ", 등장인물은 GOC 한 명뿐"}
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
    continuity: dict | None = None     # v1.8.0 연속성 계약을 건 편만(검증 통과한 원문)
    warnings: tuple[str, ...] = ()     # v1.8.5 마지막 시도에서 소프트 규칙만 남아 통과한 경우의 위반 내용

    def to_dict(self) -> dict:
        out = {
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
        if self.continuity is not None:
            out["continuity"] = dict(self.continuity)
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


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


WEEKDAY_KR = ("월", "화", "수", "목", "금", "토", "일")
# v1.8.4 Q4: 전해 들은 말로 시장 반응을 암시하는 표현(운영 베타: "…가볍게 느껴졌다는 이야기가 들린다").
#   v1.8.5 사전 점검: '~다고 한다'는 인물 의지("지키겠다고 한다")까지 걸려 뺐다.
#   '소식'은 '들린다/들려온다'와 붙을 때만 잡는다("소식이 나온다 해도"는 정상 문장).
HEARSAY_PATTERN = re.compile(
    r"(?:이야기|얘기|말|소문)(?:이|가|도)?\s*(?:들린다|들려온다|들려|돈다|나온다)"
    r"|소식(?:이|도)?\s*(?:들린다|들려온다)"
    r"|다는\s*후문"
)
# v1.8.4 Q3: 1인칭(따옴표 밖). '빛나는'·'내일'처럼 낱말 안에 든 경우는 잡지 않는다(앞이 공백·문장 시작).
#   v1.8.5 사전 점검: 동사 '나다'("빛이 나는", "땀이 나도")는 앞에 주격 조사 '이·가'와 공백이 오므로 '나는/나도'에서 제외한다.
#   '도·만'은 제외하지 않는다 — "다음 한 주에도 나는"(실제 운영 대본의 1인칭)을 놓친다(리뷰 v1.8.5 재검증).
#   그래서 "냄새도 나는"·"빛 나는"은 여전히 걸린다 — 소프트 규칙이라 재시도/경고로 끝나고 편은 빠지지 않는다.
FIRST_PERSON_PATTERN = re.compile(
    r"(?:^|[\s,.!?…])(?:(?<![이가]\s)(?<![이가]\s\s)(?:나는|나도)|나의|내가|나를|나에게|저는|제가|저의)"
    r"(?=$|[\s,.!?…])"
)
# v1.8.5: 문체 규칙(1인칭·전언형)은 '소프트' — 시도를 다 써도 통과 못 하면, 이것만 위반한 첫 시도를 경고와 함께 쓴다.
#   숫자·실존명·다른 캐릭터·주말 표현·길이 등 나머지는 '하드' — 끝까지 막는다.
SOFT_ISSUE_MARKERS = ("1인칭 서술", "전언형 암시")


def split_issues(issues: list[str]) -> tuple[list[str], list[str]]:
    """검증 위반을 (하드, 소프트)로 나눈다."""
    soft = [i for i in issues if any(m in i for m in SOFT_ISSUE_MARKERS)]
    hard = [i for i in issues if i not in soft]
    return hard, soft
_QUOTED = re.compile(r"[\"“][^\"”]*[\"”]")
# v1.8.3 운영 베타 C2: 일요일(휴장)에 "오늘 시장 … 잔잔했다"를 사실처럼 말했다. 주말에는 아래 표현을 기계적으로 막는다.
#   한국·미국 공휴일 달력은 코드에 없다(추측해 넣지 않는다) — 주말(토·일)만 판정한다.
#   단순 부분 문자열이면 '오늘 장면·오늘 장마'까지 걸려(리뷰 v1.8.3) 정규식으로 범위를 좁힌다.
WEEKEND_BANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("오늘 시장", re.compile(r"오늘\s*(?:은|의)?\s*(?:시장|증시)")),
    ("오늘 장", re.compile(r"오늘\s*장(?=$|[\s이은을에도의,.!?]|\s*마감)")),
)


def is_weekend(day: dt.date | None) -> bool:
    return day is not None and day.weekday() >= 5


def day_context(day: dt.date | None) -> str:
    """대본 프롬프트의 날짜 맥락 한 줄. 날짜를 모르면 빈 문자열."""
    if day is None:
        return ""
    label = f"{WEEKDAY_KR[day.weekday()]}요일"   # 숫자 금지 규칙이 있어 날짜 숫자는 넣지 않는다
    if is_weekend(day):
        return (f"# 오늘: {label} — 주말, 시장이 열리지 않는 날. '오늘 시장/오늘 증시/오늘 장' 표현 금지. "
                "지난 한 주의 결과를 정리하지 말고, 화제였던 주제를 어떤 마음가짐으로 바라볼지만 쓴다.")
    return (f"# 오늘: {label} — 아침 발행이라 오늘 장의 결과는 아직 없다. "
            "결과를 말하지 말고 화제와 태도만 쓴다.")


def _user_prompt(fmt: str, villain: str | None, hook_type: str, mood: mood_source.Mood,
                 avoid_hooks: list[str], character: str = config.CHARACTER_EDT,
                 day: dt.date | None = None) -> str:
    name, guide = CHARACTER_PROFILES[character]["formats"][fmt]
    spec = hooks.HOOK_SPECS[hook_type]
    lines = [
        f"# 포맷: {name}",
        guide,
        f"# 오늘의 빌런: {VILLAIN_KR[villain]}" if villain else "# 빌런 없음 — GOC 혼자 등장",
        f"# 훅 유형: {spec['name']} — {spec['guide']} (예: {spec['example']})",
        mood.to_prompt_block() or "# 근거 없음 — 특정 사건을 지어내지 말고 시장을 보는 태도만 말한다.",
    ]
    context = day_context(day)
    if context:
        lines.append(context)
    if avoid_hooks:
        lines.append("# 같은 날 이미 쓴 훅(겹치지 않게): " + " / ".join(avoid_hooks))
    return "\n".join(lines)


def _call_claude(api_key: str, user_prompt: str, character: str = config.CHARACTER_EDT) -> dict:
    payload = {
        "model": config.CLAUDE_MODEL,
        "max_tokens": config.SHORTS_SCRIPT_MAX_TOKENS,
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
            timeout=config.SHORTS_SCRIPT_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        raise ScriptError(f"Claude 호출 실패: {exc}") from exc
    if resp.status_code != 200:
        raise ScriptError(f"Claude API {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    usage = body.get("usage") or {}
    # v1.8.5: 성공 호출도 사용량을 남겨 재시도 비용을 운영 중에 확인한다.
    log.info("대본 호출 사용량 input_tokens=%s output_tokens=%s stop_reason=%s",
             usage.get("input_tokens"), usage.get("output_tokens"), body.get("stop_reason"))
    blocks = body.get("content", []) or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text.strip():
        # 운영 베타 2026-10-04: 원인 판별에 필요한 값(stop_reason·블록 종류·사용량)을 남긴다.
        raise ScriptError(
            f"text 블록 없음 — stop_reason={body.get('stop_reason')} "
            f"blocks={[b.get('type') for b in blocks]} output_tokens={usage.get('output_tokens')} "
            f"max_tokens={config.SHORTS_SCRIPT_MAX_TOKENS} model={body.get('model') or config.CLAUDE_MODEL}")
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
             character: str = config.CHARACTER_EDT,
             continuity: face_story.ContinuityRequest | None = None,
             day: dt.date | None = None) -> list[str]:
    """대본 JSON 위반 목록. 빈 목록이면 통과.

    used_captions: 같은 날 앞 편의 캡션(본문). 같으면 게시 단계 중복 검사에 걸려 그 편이 빠지므로 여기서 막는다.
    character: 다른 캐릭터 이름(대사·캡션)과 다른 캐릭터 묘사(이미지 프롬프트)를 막는다.
    continuity: v1.8.0 연속성 계약. 주면 continuity 객체의 구조·행동 규칙·린트·금지 이름을 함께 본다.
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
    extra = face_story.continuity_texts(raw.get("continuity")) if continuity is not None else []
    if continuity is not None:
        issues += face_story.validate_continuity(raw.get("continuity"), continuity)
    texts = [hook, *body, closing, caption, *(t for _, t, _ in extra)]
    for name in profile["forbidden_names"]:
        if any(name in t for t in texts):
            issues.append(f"{character} 영상에 다른 캐릭터 이름 '{name}' 포함")
    narration = [hook, *body, closing]
    for label, line in zip(["훅", *(f"본문{i}" for i in range(1, len(body) + 1)), "정리"], narration, strict=False):
        if FIRST_PERSON_PATTERN.search(_QUOTED.sub("", line)):
            issues.append(f"{label} 1인칭 서술 — 3인칭 관찰자로 쓴다(대사는 큰따옴표 한 문장만)")
    for label, line in zip(["훅", *(f"본문{i}" for i in range(1, len(body) + 1)), "정리", "캡션"],
                           [*narration, caption], strict=False):
        if HEARSAY_PATTERN.search(line):
            issues.append(f"{label} 전언형 암시('~라는 이야기가 들린다' 등) — 시장 반응을 돌려 말하지 않는다")
    if is_weekend(day):
        for label, pattern in WEEKEND_BANNED_PATTERNS:
            if any(pattern.search(t) for t in texts):
                issues.append(f"주말인데 '{label}' 표현 — 휴장일에 오늘 시장을 말하지 않는다")
    for word in IMAGE_FORBIDDEN[character]:
        if any(word in p.lower() for p in prompts):
            issues.append(f"{character} 영상 이미지 프롬프트에 '{word}' 포함")

    for label, line, max_len in (
        [("훅", hook, hooks.HOOK_MAX_CHARS)]
        + [(f"본문{i}", x, BODY_MAX_CHARS) for i, x in enumerate(body, start=1)]
        + [("정리", closing, CLOSING_MAX_CHARS), ("캡션", caption, CAPTION_BODY_MAX)]
        + extra
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
    continuity: face_story.ContinuityRequest | None = None,
) -> Script:
    """대본 생성. 검증 실패 시 위반 사유를 붙여 재시도. 소진하면 ScriptError.

    extra_instruction: 렌더 길이 초과 등 앞 단계 실패 사유(재생성 지시)를 프롬프트 끝에 붙인다.
    continuity: v1.8.0 연속성 계약(Facebook 회차 원장). None 이면 v1.7.0 과 같은 프롬프트·검증.
    """
    if character not in CHARACTER_PROFILES:
        raise ScriptError(f"알 수 없는 캐릭터: {character}")
    profile = CHARACTER_PROFILES[character]
    if fmt not in profile["formats"]:
        raise ScriptError(f"알 수 없는 포맷: {fmt}")
    villain = select_villain(mood) if profile["uses_villain"] else None
    allowed = hooks.HOOK_TYPES if profile["uses_villain"] else hooks.NO_VILLAIN_HOOK_TYPES
    hook_type = hooks.select_hook_type(fmt, villain, used_hook_types, allowed, mood.mood_word)
    day = face_story.content_date(content_id)   # 회차 날짜(KST)는 content_id 에서만 온다
    base_prompt = _user_prompt(fmt, villain, hook_type, mood, list(used_hooks or []), character, day)
    if continuity is not None:
        base_prompt += "\n\n" + continuity.prompt_block
    if extra_instruction:
        base_prompt += f"\n\n# 추가 지시(앞 시도 실패)\n{extra_instruction}"
    prompt = base_prompt
    last: list[str] = []
    fallback: tuple[dict, list[str], int] | None = None   # v1.8.5: 하드 위반 없이 문체 위반만 있던 첫 시도

    def _accept(raw: dict, attempt: int, warnings: tuple[str, ...] = ()) -> Script:
        spec = hooks.HOOK_SPECS[hook_type]
        beats = [Beat(str(raw["hook"]).strip(), spec["tts_tone"], True, spec["sfx"])]
        beats += [Beat(str(x).strip()) for x in raw["body"]]
        beats.append(Beat(str(raw["closing"]).strip()))
        if used_captions is not None:
            used_captions.add(str(raw["post_caption"]).strip())
        log.info("대본 생성 완료 id=%s 캐릭터=%s fmt=%s 빌런=%s 훅=%s 시도=%d 문체경고=%d",
                 content_id, character, fmt, villain or "-", hook_type, attempt, len(warnings))
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
            continuity=dict(raw["continuity"]) if continuity is not None else None,
            warnings=warnings,
        )

    for attempt in range(1, config.SHORTS_SCRIPT_ATTEMPTS + 1):
        try:
            raw = _call_claude(api_key, prompt, character)
        except ScriptError as exc:
            last = [str(exc)]
            log.warning("대본 생성 실패 (%d/%d): %s", attempt, config.SHORTS_SCRIPT_ATTEMPTS, exc)
            continue
        last = validate(raw, hook_type, used_captions, character, continuity, day)
        if not last:
            return _accept(raw, attempt)
        hard, soft = split_issues(last)
        if not hard and fallback is None:
            fallback = (raw, soft, attempt)
        log.warning("대본 검증 실패 (%d/%d): %s", attempt, config.SHORTS_SCRIPT_ATTEMPTS,
                    json.dumps(last, ensure_ascii=False))
        prompt = base_prompt + "\n\n# 직전 출력의 위반 사항(모두 고칠 것)\n- " + "\n- ".join(last)
    if fallback is not None:
        # v1.8.5: 문체(1인칭·전언형) 위반만 있던 시도가 있으면 편을 버리지 않고 경고와 함께 넘긴다(미리보기에서 사람이 확인).
        raw, soft, attempt = fallback
        log.warning("대본 문체 경고와 함께 통과 (시도 %d/%d 결과 사용): %s", attempt, config.SHORTS_SCRIPT_ATTEMPTS,
                    json.dumps(soft, ensure_ascii=False))
        return _accept(raw, attempt, tuple(soft))
    raise ScriptError(f"대본 재시도 소진: {last}")

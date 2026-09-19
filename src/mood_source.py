"""CHAT 기둥 근거 — 오늘 아침 시장 분위기 수집.

소스
  rss  뉴스 RSS 헤드라인 (URL 은 Variables MOOD_RSS_URLS)
  web  Claude API 웹 검색 서버 도구 (CLAUDE_AI_KEY 재사용)

프롬프트에 넣는 것은 '허용목록 안의 테마 키워드'와 '고정 어휘의 분위기 한 단어'뿐이다.
헤드라인 원문·기업명·인물명·수치는 넣지 않는다(REG-03·REG-04).
모델이 근거를 넘어 지어내지 못하도록 입력 자체를 좁히는 설계다.

실패 정책
  한 소스가 실패하면 다른 소스를 시도하고, 둘 다 실패하면 근거 없음(source=none)으로
  돌려준다. 예외를 호출자에게 전파하지 않는다. 근거 수집 실패가 발행을 막지 않는다.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import requests

from . import ai_writer, config

VERSION = "1.0.0"

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

MAX_THEMES = 5

# 분위기 어휘는 고정한다. 모델이 자유 서술하면 전망성 표현이 섞인다.
MOOD_WORDS: tuple[str, ...] = ("긴장", "관망", "안도", "낙관", "혼조", "경계")


class MoodSourceError(RuntimeError):
    """근거 수집 실패. collect() 안에서만 쓰이고 밖으로 나가지 않는다."""


@dataclass(frozen=True)
class Mood:
    source: str                       # "rss" | "web" | "none"
    themes: tuple[str, ...] = field(default_factory=tuple)
    mood_word: str = ""

    @property
    def has_evidence(self) -> bool:
        return bool(self.themes)

    def to_prompt_block(self) -> str:
        if not self.has_evidence:
            return ""
        lines = ["# 오늘 아침 시장 분위기 근거"]
        lines.append(f"- 화제 테마: {', '.join(self.themes)}")
        if self.mood_word:
            lines.append(f"- 전반적 분위기: {self.mood_word}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 살균 · 테마 추출
# ---------------------------------------------------------------------------

_URL = re.compile(r"https?://\S+")
_BRACKET = re.compile(r"[\[【(（][^\]】)）]{0,20}[\]】)）]")
_NUMERIC = re.compile(r"[+\-]?\d[\d,.]*\s*(%|％|bp|p|원|달러|엔|위안|조|억|만|배|년|월|일|시)?")
_CURRENCY = re.compile(r"[$₩€¥%％]")
_SOURCE_SUFFIX = re.compile(r"\s+[-–—|]\s+[^-–—|]{1,30}$")


def sanitize_headline(title: str) -> str:
    """헤드라인에서 수치·통화·URL·출처 꼬리·말머리를 제거한다."""
    text = (title or "").strip()
    text = _URL.sub(" ", text)
    text = _SOURCE_SUFFIX.sub("", text)      # "제목 - 언론사"
    text = _BRACKET.sub(" ", text)           # [속보] (종합) 등
    text = _NUMERIC.sub(" ", text)
    text = _CURRENCY.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_themes(texts: list[str]) -> tuple[str, ...]:
    """허용목록 용어의 등장 빈도로 테마를 뽑는다. 허용목록 밖 용어는 버린다."""
    counter: Counter[str] = Counter()
    for text in texts:
        for term in config.CHAT_THEME_ALLOWLIST:
            if term in text:
                counter[term] += 1
    ranked = [term for term, _ in counter.most_common()]
    # "실적" 과 "실적 시즌" 처럼 포함 관계인 용어는 긴 쪽만 남긴다.
    kept: list[str] = []
    for term in ranked:
        if any(term != k and term in k for k in kept):
            continue
        kept = [k for k in kept if not (k != term and k in term)]
        kept.append(term)
    return tuple(kept[:MAX_THEMES])


# ---------------------------------------------------------------------------
# rss
# ---------------------------------------------------------------------------


def _parse_pub_date(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def parse_rss(xml_text: str, now: dt.datetime) -> list[str]:
    """RSS 2.0 item 의 title 을 신선도 필터 후 돌려준다. 파싱 실패는 MoodSourceError."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MoodSourceError(f"RSS 파싱 실패: {exc}") from exc

    limit = dt.timedelta(hours=config.MOOD_RSS_MAX_AGE_HOURS)
    titles: list[str] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        published = _parse_pub_date(item.findtext("pubDate") or "")
        # 발행 시각을 모르면 신선도를 보장할 수 없으므로 버린다.
        if published is None or now - published > limit:
            continue
        titles.append(title)
        if len(titles) >= config.MOOD_RSS_MAX_ITEMS:
            break
    return titles


def fetch_rss(urls: tuple[str, ...], now: dt.datetime) -> Mood:
    if not urls:
        raise MoodSourceError("MOOD_RSS_URLS 미설정")

    titles: list[str] = []
    errors: list[str] = []
    for url in urls:
        try:
            resp = requests.get(url, timeout=config.HTTP_TIMEOUT_SEC)
        except requests.RequestException as exc:
            errors.append(f"{url[:60]}: {exc}")
            continue
        if resp.status_code != 200:
            errors.append(f"{url[:60]}: HTTP {resp.status_code}")
            continue
        try:
            titles += parse_rss(resp.text, now)
        except MoodSourceError as exc:
            errors.append(f"{url[:60]}: {exc}")

    if not titles:
        raise MoodSourceError("신선한 헤드라인 없음 " + "; ".join(errors)[:300])

    themes = extract_themes([sanitize_headline(t) for t in titles])
    if not themes:
        raise MoodSourceError(f"헤드라인 {len(titles)}건에서 허용 테마 0건")
    log.info("RSS 헤드라인 %d건 → 테마 %s", len(titles), ", ".join(themes))
    return Mood(source="rss", themes=themes)


# ---------------------------------------------------------------------------
# web (Claude 웹 검색 서버 도구)
# ---------------------------------------------------------------------------

WEB_SYSTEM_PROMPT = """당신은 시장 분위기 조사원입니다. 웹 검색으로 오늘 아침 기준 최근 24시간의
미국·한국 금융시장 분위기를 파악합니다.

출력은 JSON 한 개만 씁니다. 다른 말은 붙이지 않습니다.
{"themes": ["테마1", "테마2"], "mood": "분위기"}

규칙
- themes: 아래 허용목록에 있는 단어만, 화제성이 큰 순서로 최대 5개.
- mood: 아래 분위기 어휘 중 하나만.
- 숫자, 기업명, 인물명, 가격, 전망은 쓰지 않습니다.

허용목록: {allow}
분위기 어휘: {moods}"""


def _web_payload(today: dt.date, messages: list[dict]) -> dict:
    return {
        "model": config.CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": WEB_SYSTEM_PROMPT.replace(
            "{allow}", ", ".join(config.CHAT_THEME_ALLOWLIST)
        ).replace("{moods}", ", ".join(MOOD_WORDS)),
        "tools": [
            {
                "type": config.MOOD_WEB_TOOL_TYPE,
                "name": "web_search",
                "max_uses": config.MOOD_WEB_MAX_USES,
                "user_location": {
                    "type": "approximate",
                    "country": "KR",
                    "timezone": "Asia/Seoul",
                },
            }
        ],
        "messages": messages,
    }


def _post_messages(api_key: str, payload: dict) -> dict:
    try:
        resp = requests.post(
            ai_writer.ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ai_writer.ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            timeout=config.HTTP_TIMEOUT_SEC * 4,
        )
    except requests.RequestException as exc:
        raise MoodSourceError(f"웹 검색 호출 실패: {exc}") from exc
    if resp.status_code != 200:
        raise MoodSourceError(f"웹 검색 API {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def parse_web_response(body: dict) -> Mood:
    """최종 응답에서 JSON 을 뽑고 허용목록·고정 어휘로 검증한다."""
    blocks = body.get("content", []) or []

    searched = any(b.get("type") == "web_search_tool_result"
                   and isinstance(b.get("content"), list) for b in blocks)
    errors = [
        (b.get("content") or {}).get("error_code", "")
        for b in blocks
        if b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), dict)
    ]
    if not searched:
        # 검색 결과 없이 모델 지식만으로 답하면 '오늘' 근거가 아니다.
        raise MoodSourceError(f"웹 검색 결과 없음 errors={errors}")

    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    try:
        parsed = ai_writer._extract_json(text)
    except ai_writer.AiWriterError as exc:
        raise MoodSourceError(f"웹 검색 응답 JSON 아님: {exc}") from exc

    raw_themes = parsed.get("themes") or []
    if not isinstance(raw_themes, list):
        raise MoodSourceError("themes 가 배열이 아닙니다")
    allow = set(config.CHAT_THEME_ALLOWLIST)
    themes = tuple(dict.fromkeys(str(t).strip() for t in raw_themes if str(t).strip() in allow))
    themes = themes[:MAX_THEMES]
    if not themes:
        raise MoodSourceError(f"허용 테마 0건 (응답={raw_themes!r:.120})")

    mood = str(parsed.get("mood", "")).strip()
    if mood not in MOOD_WORDS:
        mood = ""   # 어휘 밖이면 버린다. 추측해서 매핑하지 않는다.
    return Mood(source="web", themes=themes, mood_word=mood)


def fetch_web(api_key: str, today: dt.date) -> Mood:
    if not api_key:
        raise MoodSourceError("CLAUDE_AI_KEY 없음")

    messages: list[dict] = [{
        "role": "user",
        "content": (
            f"오늘은 {today.isoformat()} (한국시간)입니다. "
            "최근 24시간 시장 분위기를 검색해 규칙대로 JSON 으로만 답하세요."
        ),
    }]

    body = _post_messages(api_key, _web_payload(today, messages))
    # pause_turn: 긴 검색 턴이 중단된 경우. 응답을 그대로 이어 보내면 재개된다(공식 문서).
    for _ in range(2):
        if body.get("stop_reason") != "pause_turn":
            break
        messages = [*messages, {"role": "assistant", "content": body.get("content", [])}]
        body = _post_messages(api_key, _web_payload(today, messages))

    mood = parse_web_response(body)
    log.info("웹 검색 → 테마 %s 분위기=%s", ", ".join(mood.themes), mood.mood_word or "-")
    return mood


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def collect(first: str, *, api_key: str, today: dt.date, now: dt.datetime) -> Mood:
    """first 소스부터 시도하고 실패 시 다른 소스, 둘 다 실패하면 근거 없음."""
    if first == "none":
        return Mood(source="none")

    order = [first, "web" if first == "rss" else "rss"]
    for name in order:
        try:
            if name == "rss":
                return fetch_rss(config.MOOD_RSS_URLS, now)
            return fetch_web(api_key, today)
        except MoodSourceError as exc:
            log.warning("근거 소스 %s 실패: %s", name, exc)
        except Exception as exc:  # noqa: BLE001 — 근거 실패가 발행을 막지 않게 한다
            log.warning("근거 소스 %s 예기치 못한 실패: %s", name, exc)

    log.info("근거 소스 전부 실패 — 근거 없음 모드로 진행합니다.")
    return Mood(source="none")

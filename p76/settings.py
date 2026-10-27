"""
p76/settings.py
===============
환경변수 → 상수. 모든 설정값은 이 파일에서만 읽는다 (DSN-P76-OPS-001 §3).

접두어: FACE_PAGE76_ · PAGE76_. 다른 프로젝트의 키와 공유하지 않는다.
테스트는 reload() 로 환경변수를 다시 읽는다.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

MODES = ("dry_run", "shadow", "live")


def _env(name: str, default: str = "") -> str:
    """빈 값도 미설정으로 본다 — 워크플로는 등록되지 않은 Variable 을 빈 문자열로 넘긴다."""
    return (os.getenv(name) or "").strip() or default


def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() == "true"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        return default
    return min(max(value, lo), hi)


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(v.strip() for v in _env(name, default).split(",") if v.strip())


def parse_anchor(raw: str | None) -> tuple[int, date] | None:
    """'21@2026-10-26' → (21, 2026-10-26). 형식이 틀리면 None."""
    try:
        no, day = (raw or "").strip().split("@")
        return int(no), date.fromisoformat(day.strip())
    except ValueError:
        return None


def parse_date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError:
        return default
    return min(max(value, lo), hi)


def parse_mode(raw: str | None) -> tuple[str, bool]:
    """STD-MODE-01·06 단일 파서. (모드, 경고 여부). 빈 값·'-' 은 dry_run, 그 밖 미지정 값은 dry_run + 경고."""
    value = (raw or "").strip().lower()
    if value in ("", "-"):
        return "dry_run", False
    if value in MODES:
        return value, False
    return "dry_run", True


def reload() -> None:
    mod = sys.modules[__name__]
    for name, value in _load().items():
        setattr(mod, name, value)


def _load() -> dict:
    mode, mode_invalid = parse_mode(os.getenv("PAGE76_MODE"))
    graph_version = _env("PAGE76_GRAPH_VERSION", "v25.0")
    return {
        # ── 운영 스위치 (기본값: 아무것도 쓰지 않음) ──────────
        "ENABLED": _env_bool("PAGE76_ENABLED"),
        "MODE": mode,
        "MODE_INVALID": mode_invalid,
        "HIATUS_PUBLISH_ENABLED": _env_bool("PAGE76_HIATUS_PUBLISH_ENABLED"),
        "INSIGHTS_ENABLED": _env_bool("PAGE76_INSIGHTS_ENABLED"),
        "HIATUS_DAYS": _env_int("PAGE76_HIATUS_DAYS", 7, 1, 21),
        # 운영 계획 기준점 "회차@KST날짜" (예: 21@2026-10-26). DB-05 에 발행 기록이 없을 때 쓴다(반수동 운영).
        "SCHEDULE_ANCHOR": parse_anchor(_env("PAGE76_SCHEDULE_ANCHOR")),
        "SCHEDULE_ANCHOR_INVALID": bool(_env("PAGE76_SCHEDULE_ANCHOR"))
        and parse_anchor(_env("PAGE76_SCHEDULE_ANCHOR")) is None,
        "CALENDAR_HORIZON_DAYS": _env_int("PAGE76_CALENDAR_HORIZON_DAYS", 21, 7, 60),
        # ── Graph ────────────────────────────────────────────
        # v25.0: 만료 2028-07-29 (fb-page-publisher 와 동일)
        "GRAPH_VERSION": graph_version,
        "GRAPH_BASE": f"https://graph.facebook.com/{graph_version}",
        "PAGE_ID": _env("FACE_PAGE76_ID"),
        "PAGE_TOKEN": _env("FACE_PAGE76_TOKEN"),
        "APP_ID": _env("PAGE76_APP_ID"),
        "APP_SECRET": _env("PAGE76_APP_SECRET"),
        # 토큰에 있어야 하는 권한(정확히 이 집합 — 추가·누락 모두 경고, SEC C-01).
        # 2026-10-28 회장 확인 권한 + public_profile(앱 기본 권한, 실측). read_insights 등 추가 발급 시 Variable 로 갱신.
        "EXPECTED_SCOPES": _env_list(
            "PAGE76_EXPECTED_SCOPES",
            "pages_show_list,business_management,pages_read_engagement,pages_manage_posts,public_profile",
        ),
        # 게시물 인사이트 지표 (read_insights 필요). 비어 있으면 /insights 호출 생략.
        "INSIGHTS_METRICS": _env_list("PAGE76_INSIGHTS_METRICS"),
        "HTTP_TIMEOUT_SEC": 60,
        "UPLOAD_TIMEOUT_SEC": 300,
        # ── Notion ───────────────────────────────────────────
        "NOTION_TOKEN": _env("PAGE76_NOTION_TOKEN"),
        "NOTION_VERSION": "2022-06-28",  # DB 데이터 소스 1개일 때만 동작 (F-09)
        "NOTION_BASE": "https://api.notion.com/v1",
        "DB05_ID": _env("PAGE76_DB05_ID"),
        "DB08_ID": _env("PAGE76_DB08_ID"),
        "DB09_ID": _env("PAGE76_DB09_ID"),
        "DB11_ID": _env("PAGE76_DB11_ID"),
        # ── 알림 ─────────────────────────────────────────────
        "TELEGRAM_BOT_TOKEN": _env("PAGE76_TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID": _env("PAGE76_TELEGRAM_CHAT_ID"),
        # ── 사건 소설 B형 (DSN-P76-NOVEL-002) ─────────────────
        "NOVEL_ENABLED": _env_bool("PAGE76_NOVEL_ENABLED"),
        "NOVEL_PUBLISH_ENABLED": _env_bool("PAGE76_NOVEL_PUBLISH_ENABLED"),
        "NOVEL_START": parse_date(_env("PAGE76_NOVEL_START", "2026-11-29")),
        "NOVEL_BUDGET_USD": _env_float("PAGE76_NOVEL_BUDGET_USD", 20.0, 1.0, 30.0),
        "CLAUDE_KEY": _env("PAGE76_CLAUDE_KEY"),
        "NOVEL_TG_BOT_TOKEN": _env("PAGE76_NOVEL_TG_BOT_TOKEN"),
        "NOVEL_TG_CHAT_ID": _env("PAGE76_NOVEL_TG_CHAT_ID"),
        "DB12_ID": _env("PAGE76_DB12_ID"),
        "NOVEL_PROMPT_PAGE_ID": _env("PAGE76_NOVEL_PROMPT_PAGE_ID"),
        "NOVEL_MEMO_PAGE_ID": _env("PAGE76_NOVEL_MEMO_PAGE_ID"),
        # 금칙어(DB-06 대체) — 비공개 Secret 으로만 받는다. 로그·API 요청에 넣지 않는다.
        "BANNED_TERMS": _env_list("PAGE76_BANNED_TERMS"),
        # ── 실행 정보 ────────────────────────────────────────
        "RUN_ID": _env("GITHUB_RUN_ID", "local"),
        "CHROME_BIN": _env("PAGE76_CHROME_BIN"),
        "LOG_LEVEL": _env("LOG_LEVEL", "INFO"),
    }


# ── 고정 상수 ────────────────────────────────────────────
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # Page Photos: "Files can not exceed 10MB"
CARD_SIZE = (1080, 1350)            # 4:5 (STD-QUAL-02 HTML 렌더)
HIATUS_DAILY_CAP = 1                # SEC C-22
SNAPSHOT_DAYS = (1, 3, 7)

# DB-05 회차 원장 (읽기 전용 — SEC C-06)
D5_TITLE, D5_NO, D5_STATE, D5_REVIEWED = "회차키", "일지번호", "게시상태", "검수완료"
D5_SCHEDULE, D5_PUBLISHED_AT, D5_FEED_ID, D5_REEL_ID = "예약일시", "발행일시", "FB피드ID", "FB릴스ID"
DB05_REQUIRED = {
    D5_TITLE: "title", D5_NO: "number", D5_STATE: "select", D5_REVIEWED: "checkbox",
    D5_SCHEDULE: "date", D5_PUBLISHED_AT: "date", D5_FEED_ID: "rich_text", D5_REEL_ID: "rich_text",
}

# DB-09 운영 캘린더
C_KEY, C_DATE, C_KIND, C_SEASON, C_NO = "캘린더키", "날짜", "구분", "시즌", "회차번호"
C_HIATUS_DAY, C_CONTENT, C_BODY, C_IMAGE = "휴재일차", "콘텐츠유형", "본문", "이미지"
C_REVIEWED, C_STATE, C_FEED_ID, C_PUBLISHED_AT, C_RESULT = "검수완료", "게시상태", "FB피드ID", "발행일시", "결과코드"
DB09_REQUIRED = {
    C_KEY: "title", C_DATE: "date", C_KIND: "select", C_SEASON: "number", C_NO: "number",
    C_HIATUS_DAY: "number", C_CONTENT: "select", C_BODY: "rich_text", C_IMAGE: "files",
    C_REVIEWED: "checkbox", C_STATE: "select", C_FEED_ID: "rich_text", C_PUBLISHED_AT: "date", C_RESULT: "rich_text",
}
KIND_EPISODE, KIND_HIATUS_CONTENT, KIND_HIATUS = "본편", "휴재콘텐츠", "휴재"
CONTENT_SUMMARY, CONTENT_VOTE, CONTENT_TEASER = "요약·흔적해설", "투표", "티저"

# 게시상태 (DB-05 와 같은 의미)
S_DRAFT, S_APPROVED, S_RUNNING = "초안", "승인", "발행중"
S_DONE, S_PARTIAL, S_FAILED, S_CHECK = "발행완료", "부분완료", "실패", "확인필요"
PUBLISHED_STATES = (S_DONE, S_PARTIAL)

# DB-08 성과 원장
M_KEY, M_NO, M_SEASON, M_POINT = "스냅샷키", "일지번호", "시즌", "시점"
M_VIEWS, M_VIEWS_UNIQUE, M_REACTIONS, M_CLICKS = "피드_조회", "피드_조회고유", "피드_반응", "피드_클릭"
M_COMMENTS, M_SHARES, M_REEL_VIEWS, M_REEL_AVG = "댓글수", "공유수", "릴스_조회", "릴스_평균시청초"
M_FOLLOWERS, M_COLLECTED_AT, M_RESULT = "페이지_팔로워", "수집일시", "수집결과"
DB08_REQUIRED = {
    M_KEY: "title", M_NO: "number", M_SEASON: "number", M_POINT: "select",
    M_VIEWS: "number", M_VIEWS_UNIQUE: "number", M_REACTIONS: "number", M_CLICKS: "number",
    M_COMMENTS: "number", M_SHARES: "number", M_REEL_VIEWS: "number", M_REEL_AVG: "number",
    M_FOLLOWERS: "number", M_COLLECTED_AT: "date", M_RESULT: "rich_text",
}
# 인사이트 지표 → DB-08 속성 (F-03)
INSIGHT_METRIC_PROPS = {
    "post_media_view": M_VIEWS,
    "post_total_media_view_unique": M_VIEWS_UNIQUE,
    "post_clicks": M_CLICKS,
}

# DB-11 실행 기록 (STD-OBS-05)
R_RUN_ID, R_PIPELINE, R_VERSION, R_MODE, R_STATUS = "run_id", "파이프라인", "버전", "모드", "상태"
R_ERROR, R_WRITES, R_START, R_END = "error_code", "쓰기건수", "시작", "종료"
DB11_REQUIRED = {
    R_RUN_ID: "title", R_PIPELINE: "select", R_VERSION: "rich_text", R_MODE: "select", R_STATUS: "select",
    R_ERROR: "rich_text", R_WRITES: "number", R_START: "date", R_END: "date",
}

# DB-12 소설 원고 (DSN-P76-NOVEL-002 §3)
N_KEY, N_KIND, N_SEASON, N_EPISODE, N_STATE, N_STOP = "키", "구분", "소설시즌", "화", "상태", "중단"
N_DUE, N_NOTIFIED_AT, N_REGISTER_AT, N_VERDICT, N_REVIEW_CODES = "게시예정일", "알림시각", "등록예정시각", "검수판정", "검수코드"
N_MODEL, N_PROMPT_VER, N_HASH, N_CHARS, N_COST = "모델", "프롬프트버전", "원고해시", "글자수", "비용USD"
N_ATTEMPTS, N_QUOTED, N_FB_ID, N_PUBLISHED_AT, N_RESULT = "시도", "인용일지", "FB게시ID", "게시일시", "결과코드"
DB12_REQUIRED = {
    N_KEY: "title", N_KIND: "select", N_SEASON: "number", N_EPISODE: "number", N_STATE: "select",
    N_STOP: "checkbox", N_DUE: "date", N_NOTIFIED_AT: "date", N_REGISTER_AT: "date", N_VERDICT: "select",
    N_REVIEW_CODES: "rich_text", N_MODEL: "rich_text", N_PROMPT_VER: "rich_text", N_HASH: "rich_text",
    N_CHARS: "number", N_COST: "number", N_ATTEMPTS: "number", N_QUOTED: "rich_text", N_FB_ID: "rich_text",
    N_PUBLISHED_AT: "date", N_RESULT: "rich_text",
}
NK_BIBLE, NK_EPISODE, NK_CONTROL = "바이블", "회차", "제어"
NS_GENERATING, NS_CHECK_FAILED, NS_HELD, NS_REFUSED = "생성중", "검사실패", "보류", "거절확인필요"
NS_IMPLICIT, NS_EXPLICIT, NS_CANON = "묵시승인대기", "명시승인대기", "정본"
NS_REVIEW, NS_APPROVED, NS_PUBLISHED, NS_CHECK = "검토대기", "승인", "게시완료", "확인필요"
NS_STOPPED, NS_DISCARDED = "중단", "폐기"

NOVEL_EPISODES = 7
NOVEL_BIBLE_LEAD_DAYS = 20       # 시즌 제1화 게시일 20일 전부터 바이블 생성
NOVEL_EPISODE_LEAD_DAYS = 14     # 회차 게시일 D-14 부터 원고 생성
NOVEL_BUFFER_EPISODES = 3        # 시즌 개시 전 1~3화는 제1화 D-14 부터 연속 생성
NOVEL_IMPLICIT_HOURS = 4         # D-N7 묵시 승인 대기
NOVEL_NOTIFY_HOURS = (9, 18)     # 묵시 승인 알림은 KST 09:00~17:59 에만
NOVEL_STALE_HOURS = 6            # 직전 실행이 이보다 오래면 묵시 등록 연기
NOVEL_PUBLISH_HOUR = 10          # 게시는 KST 10시 이후
NOVEL_REMIND_HOUR = 20           # 미승인 리마인드는 20시대 실행에서
NOVEL_MAX_ATTEMPTS = 3
NOVEL_RUN_CAP_USD = 3.0
NOVEL_LAW_MAX_AGE_DAYS = 90
NOVEL_STYLE_SAMPLES = (5, 7, 21)  # A형 수사일지 번호 (문체 견본)
NOVEL_JOURNAL_CANDIDATES = 6

# Claude API (DSN-P76-NOVEL-002 §8). 가격: 1M 토큰당 USD (2026-10 기준, 바뀌면 이 표와 날짜를 함께 고친다)
MODEL_WRITER, MODEL_REVIEWER = "claude-opus-5-5", "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MODEL_PRICES = {  # (입력, 출력, 캐시 읽기)
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
}
MODEL_PRICE_UNKNOWN = (10.0, 50.0, 1.0)  # fallback 등 표에 없는 모델은 보수적으로
CACHE_WRITE_FACTOR = 1.25

reload()

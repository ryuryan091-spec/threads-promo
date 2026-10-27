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

reload()

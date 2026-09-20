"""Live 전환 점검 — 읽기 전용.

실행
  Actions → Threads Go-Live Check → Run workflow
  로컬: python scripts/golive_check.py [--web-search]

원칙
  - Threads 에 아무것도 쓰지 않는다. GET 과 Claude 최소 호출만 한다.
  - 토큰·키 값은 출력하지 않는다(src 패키지 import 시 로그 마스킹이 설치된다).
  - FAIL 이 하나라도 있으면 종료코드 1. WARN 은 live 가능하나 확인이 필요한 항목.

검사 항목
  C1  필수 Secret / Variable 존재
  C2  운영 Variables 값 해석 (DRY_RUN, CHAT_*, REPLY_*, EVENT_STORY_*, 로테이션)
  C3  토큰 만료 잔여일 (TOKEN_ISSUED_AT / TOKEN_REFRESHED_AT)
  C4  토큰 유효성 (GET /me)
  C5  발행·답글 쿼터 조회
  C6  게시물 목록 media_type 반환 여부 (CHAT 판정 전제, v1.1.0 미검증 항목)
  C7  대화 조회 (threads_read_replies)
  C8  사용자 인사이트 조회 (threads_manage_insights)
  C9  Claude API 키 유효성 (max_tokens 8 호출)
  C10 Claude 웹 검색 (옵션 --web-search, 검색 1회 과금)
  C11 뉴스 RSS 수신 (MOOD_RSS_URLS 설정 시)
  C12 Notion 회차 조회 (NOTION_TOKEN 설정 시)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys
from collections import Counter
from zoneinfo import ZoneInfo

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from src import (  # noqa: E402
    ai_writer,
    chat_plan,
    config,
    mood_source,
    notion_source,
    token_manager,
)
from src.redact import redact  # noqa: E402
from src.threads_client import ThreadsApiError, ThreadsClient, fetch_user_id  # noqa: E402

VERSION = "1.0.0"
KST = ZoneInfo("Asia/Seoul")

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, cid: str, name: str, status: str, detail: str) -> None:
        self.rows.append((cid, name, status, redact(detail)))
        print(f"[{status:<4}] {cid:<4} {name} — {redact(detail)}")

    @property
    def failed(self) -> bool:
        return any(r[2] == FAIL for r in self.rows)

    def markdown(self) -> str:
        lines = ["| # | 항목 | 결과 | 내용 |", "|---|---|---|---|"]
        for cid, name, status, detail in self.rows:
            lines.append(f"| {cid} | {name} | {status} | {detail.replace('|', '/')} |")
        counts = Counter(r[2] for r in self.rows)
        lines.append("")
        lines.append(" · ".join(f"{k} {counts.get(k, 0)}" for k in (OK, WARN, FAIL, SKIP)))
        return "\n".join(lines)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


# ---------------------------------------------------------------------------
# C1 ~ C3 : 설정
# ---------------------------------------------------------------------------


def check_env(r: Report) -> None:
    required = ("THREADS_APP_ID", "THREADS_APP_SECRET", "THREADS_LONG_LIVED_TOKEN",
                "CLAUDE_AI_KEY", "YOUTUBE_URL", "X_URL")
    missing = [n for n in required if not _env(n)]
    r.add("C1", "필수 Secret/Variable", FAIL if missing else OK,
          f"누락 {missing}" if missing else f"{len(required)}개 존재")

    bad_links = [n for n in ("YOUTUBE_URL", "X_URL") if _env(n) and not _env(n).startswith("https://")]
    if bad_links:
        r.add("C1", "링크 형식", FAIL, f"https:// 아님: {bad_links}")

    optional = {
        "TELEGRAM_BOT_TOKEN": "실패 알림", "TELEGRAM_ALERT_CHAT_ID": "실패 알림",
        "GH_PAT_SECRETS_WRITE": "토큰 갱신값 저장", "TOKEN_ISSUED_AT": "만료 판정",
    }
    missing_opt = [f"{k}({v})" for k, v in optional.items() if not _env(k)]
    r.add("C1", "권장 Secret/Variable", WARN if missing_opt else OK,
          f"누락 {missing_opt}" if missing_opt else "전부 존재")


def check_variables(r: Report) -> None:
    dry = _env("DRY_RUN").lower() or "true"
    r.add("C2", "DRY_RUN (스케줄 실행)", OK if dry in ("false", "0", "no") else WARN,
          f"값={dry!r} — live 는 false 여야 스케줄 실행이 실제 발행한다")

    r.add("C2", "CHAT_ENABLED", OK if config.CHAT_ENABLED else WARN,
          f"{config.CHAT_ENABLED}" + ("" if config.CHAT_ENABLED else " — CHAT 은 발행되지 않는다"))

    low, high = chat_plan.daily_bounds()
    raw = (config.CHAT_DAILY_MIN, config.CHAT_DAILY_MAX)
    clamped = raw != (low, high)
    r.add("C2", "CHAT_DAILY_MIN/MAX", WARN if clamped else OK,
          f"설정 {raw} → 적용 {low}~{high}" + (" (트리거 수로 보정됨)" if clamped else ""))

    r.add("C2", "CHAT_SOURCE_MODE", OK if config.CHAT_SOURCE_MODE in ("mix", "rss", "web", "none")
          else FAIL, f"{config.CHAT_SOURCE_MODE!r}")

    r.add("C2", "REPLY_ENABLED", OK if config.REPLY_ENABLED else WARN, f"{config.REPLY_ENABLED}")
    r.add("C2", "EVENT_STORY_ENABLED", OK, f"{config.EVENT_STORY_ENABLED} (선택 기능)")
    r.add("C2", "ADAPTIVE_WEIGHTS_ENABLED", OK,
          f"{config.ADAPTIVE_WEIGHTS_ENABLED} (30일 데이터 이후 권장)")

    raw_override = _env("PILLAR_ROTATION_OVERRIDE")
    if raw_override and not config.PILLAR_ROTATION_OVERRIDE:
        r.add("C2", "PILLAR_ROTATION_OVERRIDE", WARN,
              f"자리표시자 {raw_override!r} → 미설정으로 해석. Variable 삭제 권장")
    elif config.PILLAR_ROTATION_OVERRIDE:
        parsed = [p.strip().upper() for p in config.PILLAR_ROTATION_OVERRIDE.split(",") if p.strip()]
        bad = [p for p in parsed if p not in ai_writer.PILLARS]
        r.add("C2", "PILLAR_ROTATION_OVERRIDE", FAIL if bad else OK,
              f"알 수 없는 기둥 {bad}" if bad else f"수동 로테이션 {parsed}")
    else:
        r.add("C2", "PILLAR_ROTATION_OVERRIDE", OK, "미설정 — 기본 로테이션")


def check_token_expiry(r: Report) -> None:
    today = dt.datetime.now(KST).date()
    issued = token_manager.effective_issue_date(config.TOKEN_ISSUED_AT, config.TOKEN_REFRESHED_AT)
    a = token_manager.assess_expiry(today, issued)
    status = {"ok": OK, "warn": WARN, "unknown": WARN}.get(a.level, FAIL)
    r.add("C3", "토큰 만료", status, a.message)


# ---------------------------------------------------------------------------
# C4 ~ C8 : Threads API (GET 만)
# ---------------------------------------------------------------------------


def check_threads(r: Report) -> None:
    token = _env("THREADS_LONG_LIVED_TOKEN")
    if not token:
        r.add("C4", "토큰 유효성", SKIP, "토큰 없음")
        return

    try:
        user_id, username = fetch_user_id(token)
        r.add("C4", "토큰 유효성 (GET /me)", OK, f"@{username}")
    except ThreadsApiError as exc:
        r.add("C4", "토큰 유효성 (GET /me)", FAIL, str(exc)[:200])
        return

    client = ThreadsClient(user_id, token)
    try:
        post_q, reply_q = client.get_post_quota(), client.get_reply_quota()
        r.add("C5", "쿼터", OK,
              f"발행 {post_q.used}/{post_q.total}, 답글 {reply_q.used}/{reply_q.total}")
    except ThreadsApiError as exc:
        r.add("C5", "쿼터", FAIL, str(exc)[:200])

    try:
        posts = client.get_my_posts(25)
    except ThreadsApiError as exc:
        r.add("C6", "게시물 목록", FAIL, str(exc)[:200])
        return

    types = Counter(str(p.get("media_type") or "(없음)") for p in posts)
    if not posts:
        r.add("C6", "media_type 반환", WARN, "게시물 0건 — 판정 불가")
    elif "(없음)" in types:
        r.add("C6", "media_type 반환", WARN,
              f"{dict(types)} — 일부/전부 누락. CHAT 판정이 시간창만으로 동작한다")
    else:
        r.add("C6", "media_type 반환", OK, f"{dict(types)}")
        if chat_plan.CHAT_MEDIA_TYPE not in types:
            r.add("C6", "TEXT_POST 값 관측", WARN,
                  "텍스트 게시물이 아직 없어 'TEXT_POST' 실값 미관측. CHAT 첫 발행 후 재점검")

    latest = next((str(p.get("id")) for p in posts if p.get("id")), "")
    if latest:
        try:
            items = client.get_conversation(latest, 5)
            r.add("C7", "대화 조회 (threads_read_replies)", OK, f"최신 글 대화 {len(items)}건")
        except ThreadsApiError as exc:
            r.add("C7", "대화 조회 (threads_read_replies)", FAIL, str(exc)[:200])

    try:
        client.get_user_insights(("followers_count",))
        r.add("C8", "인사이트 (threads_manage_insights)", OK, "조회 가능")
    except ThreadsApiError as exc:
        r.add("C8", "인사이트 (threads_manage_insights)", WARN,
              f"insights 워크플로우만 영향 — {str(exc)[:160]}")


# ---------------------------------------------------------------------------
# C9 ~ C12 : 외부 소스
# ---------------------------------------------------------------------------


def check_claude(r: Report, *, web_search: bool) -> None:
    key = _env("CLAUDE_AI_KEY")
    if not key:
        r.add("C9", "Claude API 키", FAIL, "CLAUDE_AI_KEY 없음 — CHAT·답글 생성 불가")
        return
    try:
        resp = requests.post(
            ai_writer.ANTHROPIC_API_URL,
            headers={"x-api-key": key, "anthropic-version": ai_writer.ANTHROPIC_VERSION,
                     "content-type": "application/json"},
            json={"model": config.CLAUDE_MODEL, "max_tokens": 8,
                  "messages": [{"role": "user", "content": "OK 라고만 답하세요."}]},
            timeout=config.HTTP_TIMEOUT_SEC * 2,
        )
    except requests.RequestException as exc:
        r.add("C9", "Claude API 키", FAIL, str(exc)[:200])
        return
    if resp.status_code == 200:
        r.add("C9", "Claude API 키", OK, f"모델 {config.CLAUDE_MODEL} 응답 정상")
    else:
        r.add("C9", "Claude API 키", FAIL, f"HTTP {resp.status_code}: {resp.text[:160]}")
        return

    if not web_search:
        r.add("C10", "Claude 웹 검색", SKIP, "--web-search 미지정 (검색 1회 과금)")
        return
    try:
        mood = mood_source.fetch_web(key, dt.datetime.now(KST).date())
        r.add("C10", "Claude 웹 검색", OK, f"테마 {', '.join(mood.themes)} / {mood.mood_word or '-'}")
    except mood_source.MoodSourceError as exc:
        status = WARN if config.MOOD_RSS_URLS else FAIL
        r.add("C10", "Claude 웹 검색", status,
              f"{str(exc)[:160]}" + (" (RSS 로 폴백 가능)" if config.MOOD_RSS_URLS
                                   else " — RSS 도 없어 CHAT 은 근거 없음 모드로만 동작"))


def check_rss(r: Report) -> None:
    if not config.MOOD_RSS_URLS:
        r.add("C11", "뉴스 RSS", SKIP, "MOOD_RSS_URLS 미설정 — 웹 검색만 사용")
        return
    try:
        mood = mood_source.fetch_rss(config.MOOD_RSS_URLS, dt.datetime.now(dt.UTC))
        r.add("C11", "뉴스 RSS", OK, f"{len(config.MOOD_RSS_URLS)}개 URL → 테마 {', '.join(mood.themes)}")
    except mood_source.MoodSourceError as exc:
        r.add("C11", "뉴스 RSS", WARN, f"{str(exc)[:160]} (웹 검색으로 폴백)")


def check_notion(r: Report) -> None:
    token = _env("NOTION_TOKEN")
    if not token or not config.NOTION_DB_ID:
        r.add("C12", "Notion 회차", SKIP, "NOTION_TOKEN/NOTION_DB_ID 미설정 — STORY 근거 없음")
        return
    episodes = notion_source.fetch_episodes(token, config.NOTION_DB_ID, 3)
    r.add("C12", "Notion 회차", OK if episodes else WARN,
          f"{len(episodes)}건 조회" if episodes else "0건 — 권한·DB ID 확인")


def main() -> int:
    parser = argparse.ArgumentParser(description="Live 전환 점검 (읽기 전용)")
    parser.add_argument("--web-search", action="store_true", help="Claude 웹 검색 1회 실제 호출")
    args = parser.parse_args()

    print(f"[GoLiveCheck] v{VERSION} 시작")
    r = Report()
    check_env(r)
    check_variables(r)
    check_token_expiry(r)
    check_threads(r)
    check_claude(r, web_search=args.web_search)
    check_rss(r)
    check_notion(r)

    summary = r.markdown()
    print("\n" + summary)
    path = _env("GITHUB_STEP_SUMMARY")
    if path:
        with pathlib.Path(path).open("a", encoding="utf-8") as fh:
            fh.write("## Go-Live Check\n\n" + summary + "\n")

    if r.failed:
        print("\nFAIL 항목이 있습니다. live 전환 전에 조치하십시오.")
        return 1
    print("\nFAIL 없음. WARN 항목을 확인한 뒤 live 로 전환하십시오.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

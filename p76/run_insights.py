"""
p76/run_insights.py
===================
발행 후 D1·D3·D7 성과를 DB-08 에 저장한다 (FR-08, DSN §6-4). Graph 는 읽기만, 쓰기는 노션뿐.

현재 토큰 권한(pages_read_engagement)으로 모이는 값
  피드_반응 · 공유수 · 댓글수   GET /{post-id}?fields=reactions.summary…,shares / comments.summary…
  페이지_팔로워                GET /{page-id}?fields=followers_count
read_insights 가 생기면 Variable PAGE76_INSIGHTS_METRICS 로 조회·고유조회·클릭(F-03)이 추가된다.
릴스 지표는 엔드포인트 미확정(F-04)이라 빈칸 + 결과 코드.

  대상  : 페이지 게시물 목록(GET /{page-id}/posts)에서 게시 후 1·3·7일인 피드 게시물.
          반수동 운영이라 DB-05 에 발행일시·피드 ID 가 없어 원장 대신 페이지를 기준으로 삼는다.
          게시일(KST) → 회차는 DB-09 캘린더 행, 없으면 운영 계획 기준점(PAGE76_SCHEDULE_ANCHOR)으로 맞춘다.
          같은 날 피드 게시물이 여럿이면 가장 이른 것, 영상(릴스) 게시물은 대상에서 뺀다.
  키    : #022-D3 (본편) · H2026-11-10-D1 (휴재 콘텐츠) · P2026-10-27-D1 (회차를 못 맞춘 게시물)
  없는 값 : 빈칸(0 으로 채우지 않음, STD-DATA-02) + 수집결과에 코드

사용: python -m p76.run_insights
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from p76 import calendar_plan, cli, graph, ledger, notion, safety, settings
from p76.notify import send
from p76.runlog import RunLog

VERSION = "1.1.1"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    key: str
    number: int | None
    season: int | None
    point: str
    feed_id: str
    has_reel: bool = False
    codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DayPlan:
    kind: str
    season: int | None
    number: int | None


def today_kst() -> date:
    return datetime.now(notion.KST).date()


def _kst_midnight(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=notion.KST).timestamp())


def day_plans(since: date, until: date) -> dict[date, DayPlan]:
    """게시일 → 회차. DB-09 행이 우선(회장 수정 반영), 빈 날은 운영 계획 기준점으로 계산."""
    out: dict[date, DayPlan] = {}
    if settings.SCHEDULE_ANCHOR is not None:
        no, day = settings.SCHEDULE_ANCHOR
        if day <= until:
            horizon = (until - max(since, day)).days + 1
            for d, e in calendar_plan.plan(no, day, max(since, day), horizon, settings.HIATUS_DAYS,
                                           clamp=False).items():
                out[d] = DayPlan(e.kind, e.season, e.number)
    for page in ledger.calendar_rows(since, until).values():
        d = notion.kst_date(page, settings.C_DATE)
        if d is not None:
            out[d] = DayPlan(notion.select(page, settings.C_KIND), notion.number(page, settings.C_SEASON),
                             notion.number(page, settings.C_NO))
    return out


def _created_kst(post: dict) -> datetime | None:
    try:
        return datetime.strptime(str(post.get("created_time")), "%Y-%m-%dT%H:%M:%S%z").astimezone(notion.KST)
    except ValueError:
        return None


def _log_posts(posts: list[dict], by_day: dict, since: date, today: date) -> None:
    """진단 로그: 날짜별 피드·영상 건수와 게시 유형 값만 (ID·본문 없음 — 공개 로그)."""
    kinds = sorted({str(p.get("status_type") or "-") for p in posts})
    days = " ".join(
        f"{d.strftime('%m-%d')}(D{(today - d).days}):피드{sum('video' not in str(p.get('status_type', '')) for _, p in v)}"
        f"·영상{sum('video' in str(p.get('status_type', '')) for _, p in v)}"
        for d, v in sorted(by_day.items())
    )
    logger.info(f"[Insights] 게시물 {len(posts)}건 ({since.isoformat()}~{(today - timedelta(days=1)).isoformat()}) "
                f"· 유형 {','.join(kinds) or '-'} · {days or '날짜 없음'}")


def targets(today: date, log: RunLog | None = None) -> list[Target]:
    since = today - timedelta(days=max(settings.SNAPSHOT_DAYS))
    ok, posts, reason = graph.page_posts(_kst_midnight(since), _kst_midnight(today))
    if not ok:
        if log is not None:
            log.degrade(f"posts:{reason}")
        return []
    by_day: dict[date, list[tuple[datetime, dict]]] = {}
    for post in posts:
        created = _created_kst(post)
        if created is not None:
            by_day.setdefault(created.date(), []).append((created, post))
    _log_posts(posts, by_day, since, today)
    plans = day_plans(since, today)
    out = []
    for day, items in sorted(by_day.items()):
        age = (today - day).days
        if age not in settings.SNAPSHOT_DAYS:
            continue
        feeds = sorted((c, p) for c, p in items if "video" not in str(p.get("status_type", "")))
        if not feeds:
            continue
        codes = (f"multi_post:{len(feeds)}",) if len(feeds) > 1 else ()
        has_reel = len(feeds) < len(items)
        feed_id = str(feeds[0][1]["id"])
        plan = plans.get(day)
        if plan and plan.kind == settings.KIND_EPISODE and plan.number:
            out.append(Target(f"#{plan.number:03d}-D{age}", plan.number, plan.season, f"D{age}", feed_id, has_reel, codes))
        elif plan and plan.kind == settings.KIND_HIATUS_CONTENT:
            out.append(Target(f"H{day.isoformat()}-D{age}", None, plan.season, f"D{age}", feed_id, has_reel, codes))
        else:
            out.append(Target(f"P{day.isoformat()}-D{age}", None, plan.season if plan else None, f"D{age}", feed_id,
                              has_reel, (*codes, "unmapped")))
    return out


def collect(t: Target, followers: int | None) -> tuple[dict, list[str]]:
    values: dict[str, float | int | None] = {settings.M_FOLLOWERS: followers}
    codes: list[str] = list(t.codes)
    if t.feed_id:
        eng, c = graph.post_engagement(t.feed_id)
        values[settings.M_REACTIONS] = eng["reactions"]
        values[settings.M_SHARES] = eng["shares"]
        values[settings.M_COMMENTS] = eng["comments"]
        codes += c
        if settings.INSIGHTS_METRICS and not safety.tripped():
            metrics, c = graph.post_insights(t.feed_id, settings.INSIGHTS_METRICS)
            for name, value in metrics.items():
                prop = settings.INSIGHT_METRIC_PROPS.get(name)
                if prop:
                    values[prop] = value
            codes += c
        else:
            codes.append("insights:skipped")
    else:
        codes.append("feed:no_id")
    if t.has_reel:
        codes.append("reel:not_collected")
    return values, codes


def props_of(t: Target, values: dict, codes: list[str]) -> dict:
    props = {
        settings.M_KEY: notion.v_title(t.key),
        settings.M_NO: notion.v_number(t.number),
        settings.M_SEASON: notion.v_number(t.season),
        settings.M_POINT: notion.v_select(t.point),
        settings.M_COLLECTED_AT: notion.v_date(datetime.now(UTC)),
        settings.M_RESULT: notion.v_rich(";".join(codes) or "ok"),
    }
    for prop, value in values.items():
        props[prop] = notion.v_number(value)
    return props


def existing_snapshot(key: str) -> dict | None:
    body = {"filter": {"property": settings.M_KEY, "title": {"equals": key}}, "page_size": 1}
    return next(iter(notion.query(settings.DB08_ID, body, max_pages=1)), None)


def body(log: RunLog, today: date | None = None) -> None:
    if not settings.INSIGHTS_ENABLED:
        logger.info("[Insights] PAGE76_INSIGHTS_ENABLED=false — 종료")
        return
    day0 = today or today_kst()
    todo = targets(day0, log)
    if not todo:
        logger.info("[Insights] 오늘 수집 대상 없음")
        return
    ok, page, reason = graph.page_info()
    followers = page.get("followers_count") if ok else None
    if not ok:
        log.degrade(f"followers:{reason}")
    if safety.tripped():
        return
    saved = 0
    for t in todo:
        values, codes = collect(t, followers if isinstance(followers, int) else None)
        if safety.tripped():
            break
        if any(not c.startswith(("insights:skipped", "reel:", "multi_post")) for c in codes):
            log.degrade(f"{t.key}:partial")
        logger.info(f"[Insights] {t.key} 수집 ({len([v for v in values.values() if v is not None])}개 값)")
        if not safety.notion_write_allowed():
            continue
        current = existing_snapshot(t.key)
        props = props_of(t, values, codes)
        if current is None:
            notion.create_page(settings.DB08_ID, props)
        else:
            notion.update_page(current["id"], props)
        log.writes += 1
        saved += 1
    logger.info(f"[Insights] 대상 {len(todo)}건 · 저장 {saved}건 · mode={settings.MODE}")
    if log.codes:
        send(f"성과 수집 {day0.isoformat()}: 일부 지표 누락 {len(log.codes)}건 — DB-08 수집결과 확인")


def main() -> int:
    cli.setup_logging()
    return cli.execute("insights", VERSION, body)


if __name__ == "__main__":
    sys.exit(main())

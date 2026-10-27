"""
p76/run_insights.py
===================
발행 후 D1·D3·D7 성과를 DB-08 에 저장한다 (FR-08, DSN §6-4). Graph 는 읽기만, 쓰기는 노션뿐.

현재 토큰 권한(pages_read_engagement)으로 모이는 값
  피드_반응 · 공유수 · 댓글수   GET /{post-id}?fields=reactions.summary…,shares / comments.summary…
  페이지_팔로워                GET /{page-id}?fields=followers_count
read_insights 가 생기면 Variable PAGE76_INSIGHTS_METRICS 로 조회·고유조회·클릭(F-03)이 추가된다.
릴스 지표는 엔드포인트 미확정(F-04)이라 빈칸 + 결과 코드.

  대상  : DB-05 발행완료·부분완료 중 발행 후 1·3·7일이 오늘인 회차 + DB-09 휴재콘텐츠 발행완료
  키    : #022-D3 (본편) · H2026-11-10-D1 (휴재 콘텐츠) — 같은 키는 갱신(멱등)
  없는 값 : 빈칸(0 으로 채우지 않음, STD-DATA-02) + 수집결과에 코드

사용: python -m p76.run_insights
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from p76 import cli, graph, ledger, notion, safety, settings
from p76.notify import send
from p76.runlog import RunLog
from p76.season import locate

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    key: str
    number: int | None
    season: int | None
    point: str
    feed_id: str
    reel_id: str


def today_kst() -> date:
    return datetime.now(notion.KST).date()


def targets(today: date) -> list[Target]:
    since = today - timedelta(days=max(settings.SNAPSHOT_DAYS))
    out = []
    for ep in ledger.published_episodes(since):
        age = (today - ep.day).days
        if age in settings.SNAPSHOT_DAYS:
            out.append(Target(f"#{ep.number:03d}-D{age}", ep.number, locate(ep.number)[0], f"D{age}",
                              ep.feed_id, ep.reel_id))
    for key, page in ledger.calendar_rows(since, today).items():
        if notion.select(page, settings.C_KIND) != settings.KIND_HIATUS_CONTENT:
            continue
        if notion.select(page, settings.C_STATE) != settings.S_DONE:
            continue
        published = notion.kst_date(page, settings.C_PUBLISHED_AT)
        age = (today - published).days if published else -1
        if age in settings.SNAPSHOT_DAYS:
            out.append(Target(f"H{key}-D{age}", None, notion.number(page, settings.C_SEASON), f"D{age}",
                              notion.plain(page, settings.C_FEED_ID), ""))
    return out


def collect(t: Target, followers: int | None) -> tuple[dict, list[str]]:
    values: dict[str, float | int | None] = {settings.M_FOLLOWERS: followers}
    codes: list[str] = []
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
    if t.reel_id:
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
    todo = targets(day0)
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
        if codes and any(not c.startswith(("insights:skipped", "reel:")) for c in codes):
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

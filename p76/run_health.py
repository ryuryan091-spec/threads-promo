"""
p76/run_health.py
=================
매일 점검 (FR-07, DSN §6-6). 읽기만 한다. PAGE76_ENABLED=false 여도 실행한다(토큰 무효를 24시간 안에 탐지).

  H1 페이지 접근        GET /{page-id}                      실패 → FAIL(최우선, 두 레포 토큰 교체 안내)
  H2 토큰 정보          debug_token: 유효·PAGE·만료 없음·권한  권한 누락·추가 모두 WARN (SEC C-01)
  H3 인사이트 지표      PAGE76_INSIGHTS_METRICS 가 있을 때만   read_insights 없으면 SKIP
  H4 댓글 권한          (모더레이션 미구현 — 권한 발급 후)     SKIP
  H5 노션 스키마        DB-05(읽기 속성)·DB-08·DB-09·DB-11      불일치 → FAIL
  H6 휴재 중 본편 승인  오늘 DB-09 ≠ 본편 AND DB-05 승인 행      WARN
  H7 예약 작업 누락     오늘 DB-09 = 본편 #N AND DB-05 에 #N 없음 WARN

사용: python -m p76.run_health
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime

from p76 import cli, graph, ledger, notion, settings
from p76.notify import send
from p76.runlog import RunLog

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    hid: str
    level: str
    code: str = ""


def today_kst() -> date:
    return datetime.now(notion.KST).date()


def h1_page() -> Check:
    if not settings.PAGE_ID or not settings.PAGE_TOKEN:
        return Check("H1", FAIL, "not_configured")
    ok, _, reason = graph.page_info()
    return Check("H1", OK) if ok else Check("H1", FAIL, reason)


def h2_token() -> list[Check]:
    ok, data, reason = graph.debug_token()
    if not ok:
        return [Check("H2", FAIL, f"debug_token:{reason}")]
    out = []
    if not data.get("is_valid"):
        out.append(Check("H2", FAIL, "invalid"))
    if str(data.get("type", "")).upper() != "PAGE":
        out.append(Check("H2", WARN, f"type:{data.get('type')}"))
    if data.get("expires_at") not in (0, None):
        out.append(Check("H2", WARN, "expires"))
    scopes = set(data.get("scopes") or [])
    expected = set(settings.EXPECTED_SCOPES)
    missing, extra = sorted(expected - scopes), sorted(scopes - expected)
    if missing:
        out.append(Check("H2", WARN, "scope_missing:" + "+".join(missing)))
    if extra:
        out.append(Check("H2", WARN, "scope_extra:" + "+".join(extra)))
    return out or [Check("H2", OK)]


def h3_insights(latest_feed_id: str) -> Check:
    if not settings.INSIGHTS_METRICS:
        return Check("H3", SKIP, "no_metrics(read_insights)")
    if not latest_feed_id:
        return Check("H3", SKIP, "no_post")
    _, codes = graph.post_insights(latest_feed_id, settings.INSIGHTS_METRICS)
    return Check("H3", WARN if codes else OK, ";".join(codes)[:200])


def h5_schema() -> list[Check]:
    out = []
    for name, db_id, required in (
        ("DB05", settings.DB05_ID, settings.DB05_REQUIRED),
        ("DB08", settings.DB08_ID, settings.DB08_REQUIRED),
        ("DB09", settings.DB09_ID, settings.DB09_REQUIRED),
        ("DB11", settings.DB11_ID, settings.DB11_REQUIRED),
    ):
        try:
            diff = notion.schema_mismatches(notion.retrieve_database(db_id), required)
        except notion.NotionError as e:
            out.append(Check("H5", FAIL, f"{name}:{e}"))
            continue
        if diff:
            out.append(Check("H5", FAIL, f"{name}:" + ",".join(diff)[:150]))
    return out or [Check("H5", OK)]


def h6_h7(today: date) -> list[Check]:
    try:
        row = ledger.calendar_rows(today, today).get(today.isoformat())
    except notion.NotionError as e:
        return [Check("H6", WARN, f"db09:{e}")]
    if row is None:
        return [Check("H6", WARN, "calendar_row_missing")]
    kind = notion.select(row, settings.C_KIND)
    out = []
    try:
        if kind != settings.KIND_EPISODE:
            approved = ledger.approved_episode_numbers()
            out.append(Check("H6", WARN, "approved_during_hiatus:" + ",".join(f"#{n:03d}" for n in approved))
                       if approved else Check("H6", OK))
        else:
            number = notion.number(row, settings.C_NO)
            if number is not None and not ledger.episode_exists(number):
                out.append(Check("H7", WARN, f"draft_missing:#{number:03d}"))
            else:
                out.append(Check("H7", OK))
    except notion.NotionError as e:
        out.append(Check("H6", WARN, f"db05:{e}"))
    return out


def body(log: RunLog, today: date | None = None) -> None:
    day0 = today or today_kst()
    checks: list[Check] = [h1_page()]
    if checks[0].level == OK:
        checks += h2_token()
    latest_feed = ""
    try:
        last = ledger.last_published()
        latest_feed = last.feed_id if last else ""
    except notion.NotionError:
        pass
    checks.append(h3_insights(latest_feed) if checks[0].level == OK else Check("H3", SKIP, "page_unreachable"))
    checks.append(Check("H4", SKIP, "moderation_not_enabled"))
    checks += h5_schema()
    checks += h6_h7(day0)

    for c in checks:
        logger.info(f"[Health] {c.hid} {c.level} {c.code}".rstrip())
    problems = [c for c in checks if c.level in (WARN, FAIL)]
    for c in problems:
        (log.fail if c.level == FAIL else log.degrade)(f"{c.hid}:{c.code}")
    if not problems:
        return
    lines = " / ".join(f"{c.hid} {c.level} {c.code}" for c in problems)
    urgent = any(c.hid in ("H1", "H2") and c.level == FAIL for c in problems)
    hint = " — 토큰 재발급 시 fb-page-publisher FBDET_PAGE_TOKEN 과 함께 교체" if urgent else ""
    send(f"health {day0.isoformat()}: {lines}{hint}", urgent=urgent)


def main() -> int:
    cli.setup_logging()
    return cli.execute("health", VERSION, body, require_enabled=False)


if __name__ == "__main__":
    sys.exit(main())

"""
p76/run_calendar.py
===================
DB-09 운영 캘린더 갱신 (매일 21:28 KST, 21:45 예약 작업 전). FR-01~03.

  1. DB-05 에서 실제 발행된 마지막 회차·발행일을 읽는다 (실패 → 갱신 생략 + 알림, 기존 행 유지)
  2. 오늘부터 21일치를 계산해 캘린더키(날짜)로 upsert
  3. 회장이 손댄 행(게시상태가 초안이 아님 · 본문 있음 · 검수완료)은 덮어쓰지 않는다
  4. 지난 날 행은 조회 대상에서 빼므로 수정하지 않는다
  5. 계산 결과와 같은 행은 쓰지 않는다 → 두 번 실행해도 쓰기 0 (멱등)

사용: python -m p76.run_calendar
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime

from p76 import calendar_plan, cli, ledger, notion, safety, settings
from p76.calendar_plan import Entry
from p76.notify import send
from p76.runlog import RunLog

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def today_kst() -> date:
    return datetime.now(notion.KST).date()


def desired_props(entry: Entry) -> dict:
    return {
        settings.C_DATE: notion.v_date(entry.day),
        settings.C_KIND: notion.v_select(entry.kind),
        settings.C_SEASON: notion.v_number(entry.season),
        settings.C_NO: notion.v_number(entry.number),
        settings.C_HIATUS_DAY: notion.v_number(entry.hiatus_day),
        settings.C_CONTENT: notion.v_select(entry.content),
    }


def is_protected(page: dict) -> bool:
    state = notion.select(page, settings.C_STATE)
    return (state not in ("", settings.S_DRAFT)) or bool(notion.plain(page, settings.C_BODY)) \
        or notion.checkbox(page, settings.C_REVIEWED)


def matches(page: dict, entry: Entry) -> bool:
    day = notion.kst_date(page, settings.C_DATE)
    return (
        day == entry.day
        and notion.select(page, settings.C_KIND) == entry.kind
        and notion.number(page, settings.C_SEASON) == entry.season
        and notion.number(page, settings.C_NO) == entry.number
        and notion.number(page, settings.C_HIATUS_DAY) == entry.hiatus_day
        and (notion.select(page, settings.C_CONTENT) or None) == entry.content
    )


def sync(entries: dict[date, Entry], existing: dict[str, dict], run: RunLog) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "same": 0, "protected": 0}
    for day in sorted(entries):
        entry, key = entries[day], day.isoformat()
        page = existing.get(key)
        if page is not None and is_protected(page):
            counts["protected"] += 1
            continue
        if page is not None and matches(page, entry):
            counts["same"] += 1
            continue
        action = "create" if page is None else "update"
        counts[action] += 1
        if not safety.notion_write_allowed():
            continue
        if page is None:
            props = desired_props(entry)
            props[settings.C_KEY] = notion.v_title(key)
            props[settings.C_STATE] = notion.v_select(settings.S_DRAFT)
            notion.create_page(settings.DB09_ID, props)
        else:
            notion.update_page(page["id"], desired_props(entry))
        run.writes += 1
    return counts


def body(log: RunLog, today: date | None = None) -> None:
    day0 = today or today_kst()
    try:
        anchor = ledger.last_published()
    except notion.NotionError as e:
        log.fail(f"db05_read:{e}")
        send(f"캘린더 갱신 생략 — DB-05 조회 실패({e}). 기존 캘린더 유지")
        return
    if anchor is None:
        log.fail("db05_no_published")
        send("캘린더 갱신 생략 — DB-05 에 발행완료 회차 없음")
        return

    entries = calendar_plan.plan(anchor.number, anchor.day, day0, settings.CALENDAR_HORIZON_DAYS, settings.HIATUS_DAYS)
    try:
        counts = sync(entries, ledger.calendar_rows(day0), log)
    except notion.NotionError as e:
        log.fail(f"db09:{e}")
        send(f"캘린더 갱신 실패 — DB-09 오류({e})")
        return

    hiatus = sorted(d for d, e in entries.items() if e.kind != settings.KIND_EPISODE)
    tail = f" · 다음 휴재 {hiatus[0].isoformat()}~{hiatus[-1].isoformat()}" if hiatus else ""
    logger.info(
        f"[Calendar] 기준 #{anchor.number:03d}({anchor.day.isoformat()}) · {len(entries)}일 · "
        + " ".join(f"{k}={v}" for k, v in counts.items()) + f" · mode={settings.MODE}{tail}"
    )


def main() -> int:
    cli.setup_logging()
    return cli.execute("calendar", VERSION, body)


if __name__ == "__main__":
    sys.exit(main())

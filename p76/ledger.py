"""
p76/ledger.py
=============
DB-05 회차 원장(읽기 전용)과 DB-09 운영 캘린더 조회 묶음.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from p76 import notion, settings

VERSION = "1.0.0"


@dataclass(frozen=True)
class Published:
    number: int
    day: date          # 발행일 (KST)
    feed_id: str
    reel_id: str


def _published_filter() -> dict:
    return {"or": [{"property": settings.D5_STATE, "select": {"equals": s}} for s in settings.PUBLISHED_STATES]}


def last_published() -> Published | None:
    body = {"filter": _published_filter(),
            "sorts": [{"property": settings.D5_NO, "direction": "descending"}], "page_size": 10}
    for page in notion.query(settings.DB05_ID, body, max_pages=1):
        no = notion.number(page, settings.D5_NO)
        day = notion.kst_date(page, settings.D5_PUBLISHED_AT)
        if no is not None and day is not None:
            return Published(no, day, notion.plain(page, settings.D5_FEED_ID), notion.plain(page, settings.D5_REEL_ID))
    return None


def approved_episode_numbers() -> list[int]:
    body = {"filter": {"property": settings.D5_STATE, "select": {"equals": settings.S_APPROVED}}, "page_size": 20}
    return sorted(n for p in notion.query(settings.DB05_ID, body, max_pages=1)
                  if (n := notion.number(p, settings.D5_NO)) is not None)


def episode_exists(number: int) -> bool:
    body = {"filter": {"property": settings.D5_NO, "number": {"equals": number}}, "page_size": 1}
    return any(True for _ in notion.query(settings.DB05_ID, body, max_pages=1))


def calendar_rows(since: date, until: date | None = None) -> dict[str, dict]:
    """캘린더키 → DB-09 페이지."""
    conds = [{"property": settings.C_DATE, "date": {"on_or_after": since.isoformat()}}]
    if until is not None:
        conds.append({"property": settings.C_DATE, "date": {"on_or_before": until.isoformat()}})
    body = {"filter": {"and": conds} if len(conds) > 1 else conds[0], "page_size": 100}
    return {notion.plain(p, settings.C_KEY): p for p in notion.query(settings.DB09_ID, body)}

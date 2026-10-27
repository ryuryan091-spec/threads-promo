"""
p76/calendar_plan.py
====================
운영 캘린더 계산 (순수 함수 — 노션·시계 없음). DSN §6-2, FR-01~03.

기준점 = DB-05 에서 실제 발행된 마지막 회차와 그 발행일(KST). 계획일이 아니라 실제 발행을 따른다.
  - 완결 회(k = L) 다음 날부터 hiatus_days 일 휴재. 1일차 요약·흔적해설 / 가운데 투표 / 마지막 티저.
  - 아직 발행되지 않은 회차는 오늘보다 이른 날에 놓지 않는다(지연되면 뒤로 밀림).
(stdlib calendar 와 이름이 겹치지 않도록 calendar_plan 으로 둔다.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from p76 import settings
from p76.season import is_finale, locate

VERSION = "1.0.0"


@dataclass(frozen=True)
class Entry:
    day: date
    kind: str
    season: int
    number: int | None = None
    hiatus_day: int | None = None
    content: str | None = None


def content_days(hiatus_days: int) -> dict[int, str]:
    """휴재 일차 → 콘텐츠유형. 7일이면 1·4·7."""
    mapping = {1: settings.CONTENT_SUMMARY}
    mapping[(hiatus_days + 1) // 2] = settings.CONTENT_VOTE
    mapping[hiatus_days] = settings.CONTENT_TEASER
    return mapping


def plan(anchor_no: int, anchor_day: date, today: date, horizon: int, hiatus_days: int) -> dict[date, Entry]:
    """today 부터 horizon 일치 캘린더."""
    end = today + timedelta(days=horizon - 1)
    contents = content_days(hiatus_days)
    out: dict[date, Entry] = {}

    def put(entry: Entry) -> None:
        if today <= entry.day <= end:
            out[entry.day] = entry

    cur, day = anchor_no, anchor_day
    put(Entry(day, settings.KIND_EPISODE, locate(cur)[0], number=cur))
    while day <= end:
        if is_finale(cur):
            n = locate(cur)[0]
            for i in range(1, hiatus_days + 1):
                kind = settings.KIND_HIATUS_CONTENT if i in contents else settings.KIND_HIATUS
                put(Entry(day + timedelta(days=i), kind, n, hiatus_day=i, content=contents.get(i)))
            nxt = day + timedelta(days=hiatus_days + 1)
        else:
            nxt = day + timedelta(days=1)
        cur, day = cur + 1, max(nxt, today)
        put(Entry(day, settings.KIND_EPISODE, locate(cur)[0], number=cur))
    return out

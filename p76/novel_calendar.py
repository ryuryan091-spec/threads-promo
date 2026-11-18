"""
p76/novel_calendar.py
=====================
사건 소설(B형) 일정 엔진 — 순수 함수(시계·노션 없음). DSN-P76-NOVEL-003 §4 · DN2026_0119.

  게시일 = settings.NOVEL_START 부터 금요일(settings.NOVEL_REST_WEEKDAY)과 순연 날짜(shifts)를 뺀 날.
  전역 회차 번호 k = (n-1)·NOVEL_EPISODES + m 의 게시일이 due(n, m).
  순연(A2): CONTROL 행 JSON "shifts"(ISO 날짜 목록)에 날짜를 넣으면 그날은 게시일에서 빠지고 이후 회차가 하루씩 밀린다.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from p76 import settings

VERSION = "1.0.0"  # DN2026_0119 신규

WEEKDAY_KO = "월화수목금토일"
_MAX_SCAN_DAYS = 3660  # 안전 상한(약 10년) — 잘못된 입력으로 무한 반복하지 않게


def parse_shifts(raw: object) -> tuple[date, ...]:
    """CONTROL JSON 의 shifts 값 → 날짜 튜플(정렬·중복 제거). 형식이 틀린 항목은 버린다."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: set[date] = set()
    for item in raw:
        if isinstance(item, date):
            out.add(item)
            continue
        try:
            out.add(date.fromisoformat(str(item).strip()))
        except ValueError:
            continue
    return tuple(sorted(out))


def _as_set(shifts: Iterable) -> frozenset[date]:
    return frozenset(parse_shifts(list(shifts)))


def is_publish_day(d: date, shifts: Iterable = ()) -> bool:
    return d.weekday() != settings.NOVEL_REST_WEEKDAY and d not in _as_set(shifts)


def nth_publish_day(start: date, k: int, shifts: Iterable = ()) -> date:
    """start 이후(포함) k 번째 게시일 (k ≥ 1). start 가 비게시일이면 다음 게시일이 1번째."""
    if k < 1:
        raise ValueError("k must be >= 1")
    skip = _as_set(shifts)
    d, count = start, 0
    for _ in range(_MAX_SCAN_DAYS + k):
        if d.weekday() != settings.NOVEL_REST_WEEKDAY and d not in skip:
            count += 1
            if count == k:
                return d
        d += timedelta(days=1)
    raise ValueError("calendar scan limit")


def global_index(n: int, m: int) -> int:
    return (n - 1) * settings.NOVEL_EPISODES + m


def due(n: int, m: int, shifts: Iterable = ()) -> date:
    if settings.NOVEL_START is None:
        raise ValueError("NOVEL_START not configured")
    return nth_publish_day(settings.NOVEL_START, global_index(n, m), shifts)


def season_start(n: int, shifts: Iterable = ()) -> date:
    return due(n, 1, shifts)


def weekday_ko(d: date) -> str:
    return WEEKDAY_KO[d.weekday()]


def label(d: date) -> str:
    """'11-29(일)' — 알림·안내 문구용."""
    return f"{d:%m-%d}({weekday_ko(d)})"

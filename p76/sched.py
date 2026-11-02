"""
p76/sched.py
============
예약 실행의 '예정 시각' 계산 (STD-SCHED-03 — 시각이 중요한 판정은 cron 정시에 의존하지 않는다).

GitHub schedule 이벤트는 고부하 때 수 시간 늦게 시작될 수 있다(2026-11-01 실측 1~6시간).
워크플로는 발화한 cron 문자열(github.event.schedule)을 PAGE76_SCHEDULE_CRON 으로 넘긴다.
여기서는 그 cron 이 마지막으로 발화했어야 하는 시각(UTC, 실행 시각 이전 중 가장 최근)을 구한다.

    scheduled_at(now)   → 예정 시각(UTC) 또는 None(수동 실행·cron 없음·해석 실패)
    effective_now(now)  → 예정 시각이 있으면 그 시각, 없으면 now   ('오늘'·시간대 판정용)
    lag_seconds(now)    → now - 예정 시각(초) 또는 None
    too_late(now)       → 지연이 settings.MAX_LAG_MIN 을 넘으면 True (게시 계열 보류 판정)

외부 의존성 없이 이 레포가 쓰는 cron 문법(숫자·*·목록·범위·간격)만 지원한다. 해석에 실패하면 None 을
돌려주고 호출부는 기존 동작(실행 시각 기준)으로 돌아간다 — 단, 게시 계열은 too_late 가 False 이므로
지연 가드만 빠진다. 해석 실패는 cron_invalid() 로 따로 확인해 degrade 코드를 남긴다.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from p76 import settings

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 8  # 주 1회 cron 도 찾을 수 있게 7일 + 1


def _field(raw: str, lo: int, hi: int) -> set[int] | None:
    """cron 필드 1개 → 허용 값 집합. 형식이 틀리면 None."""
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            part, step_raw = part.split("/", 1)
            if not step_raw.isdigit() or int(step_raw) < 1:
                return None
            step = int(step_raw)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                return None
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = hi
        else:
            return None
        if start < lo or end > hi or start > end:
            return None
        values.update(range(start, end + 1, step))
    return values


def parse(cron: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool] | None:
    """'분 시 일 월 요일' → (분, 시, 일, 월, 요일(0=일), 일 제한 여부, 요일 제한 여부). 실패 시 None."""
    parts = (cron or "").split()
    if len(parts) != 5:
        return None
    minute = _field(parts[0], 0, 59)
    hour = _field(parts[1], 0, 23)
    dom = _field(parts[2], 1, 31)
    month = _field(parts[3], 1, 12)
    dow = _field(parts[4], 0, 7)
    if None in (minute, hour, dom, month, dow):
        return None
    dow = {0 if d == 7 else d for d in dow}  # type: ignore[union-attr]
    return minute, hour, dom, month, dow, parts[2] != "*", parts[4] != "*"  # type: ignore[return-value]


def _day_matches(day: datetime, spec: tuple) -> bool:
    _, _, dom, month, dow, dom_restricted, dow_restricted = spec
    if day.month not in month:
        return False
    cron_dow = (day.weekday() + 1) % 7  # Python 월=0 → cron 일=0
    if dom_restricted and dow_restricted:  # 표준 cron: 둘 다 지정되면 OR
        return day.day in dom or cron_dow in dow
    return day.day in dom and cron_dow in dow


def last_fire(cron: str, now: datetime) -> datetime | None:
    """now(UTC) 이전(같은 분 포함) 중 cron 이 마지막으로 발화한 시각. 없거나 해석 실패면 None."""
    spec = parse(cron)
    if spec is None:
        return None
    minutes, hours = sorted(spec[0], reverse=True), sorted(spec[1], reverse=True)
    now = now.astimezone(UTC).replace(second=0, microsecond=0)
    for back in range(_LOOKBACK_DAYS + 1):
        day = (now - timedelta(days=back)).replace(hour=0, minute=0)
        if not _day_matches(day, spec):
            continue
        for h in hours:
            for m in minutes:
                candidate = day.replace(hour=h, minute=m)
                if candidate <= now:
                    return candidate
    return None


def cron_invalid() -> bool:
    """cron 문자열이 있는데 해석하지 못하면 True (수동 실행처럼 비어 있으면 False)."""
    return bool(settings.SCHEDULE_CRON) and parse(settings.SCHEDULE_CRON) is None


def scheduled_at(now: datetime | None = None) -> datetime | None:
    if not settings.SCHEDULE_CRON:
        return None
    return last_fire(settings.SCHEDULE_CRON, now or datetime.now(UTC))


def effective_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return scheduled_at(now) or now


def lag_seconds(now: datetime | None = None) -> int | None:
    now = now or datetime.now(UTC)
    at = scheduled_at(now)
    return None if at is None else max(0, int((now - at).total_seconds()))


def too_late(now: datetime | None = None) -> bool:
    lag = lag_seconds(now)
    return lag is not None and lag > settings.MAX_LAG_MIN * 60

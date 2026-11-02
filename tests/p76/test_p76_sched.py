"""STD-SCHED-03 예정 시각 계산 (p76/sched.py) — cron 정시에 의존하지 않는 '오늘'·지연 판정."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from p76 import sched


def utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


# ── last_fire ────────────────────────────────────────────
@pytest.mark.parametrize("cron,now,expected", [
    # 일간
    ("5 11 * * *", utc(2026, 11, 10, 11, 30), utc(2026, 11, 10, 11, 5)),
    ("5 11 * * *", utc(2026, 11, 10, 11, 5, 40), utc(2026, 11, 10, 11, 5)),   # 같은 분 포함
    ("5 11 * * *", utc(2026, 11, 10, 11, 4), utc(2026, 11, 9, 11, 5)),        # 아직 전 → 전날
    # 주간 (2026-11-07 = 토요일, cron 6)
    ("17 23 * * 6", utc(2026, 11, 8, 1, 0), utc(2026, 11, 7, 23, 17)),
    ("21 1 * * 0", utc(2026, 11, 11, 9, 0), utc(2026, 11, 8, 1, 21)),
    ("17 23 * * 6", utc(2026, 11, 14, 23, 16), utc(2026, 11, 7, 23, 17)),     # 7일 전까지 거슬러 감
    # */n
    ("*/15 * * * *", utc(2026, 11, 10, 10, 44), utc(2026, 11, 10, 10, 30)),
    ("0 */4 * * *", utc(2026, 11, 10, 13, 0), utc(2026, 11, 10, 12, 0)),
    ("0 */4 * * *", utc(2026, 11, 10, 3, 59), utc(2026, 11, 10, 0, 0)),
    # 자정 넘김 (UTC 기준 전날 발화)
    ("50 23 * * *", utc(2026, 11, 11, 0, 40), utc(2026, 11, 10, 23, 50)),
])
def test_last_fire(cron, now, expected):
    assert sched.last_fire(cron, now) == expected


@pytest.mark.parametrize("cron", ["", "bad", "61 * * * *", "5 24 * * *", "* * * *", "*/0 * * * *", "a-b * * * *"])
def test_last_fire_parse_failure(cron):
    assert sched.parse(cron) is None
    assert sched.last_fire(cron, utc(2026, 11, 10, 12, 0)) is None


# ── 예약 실행 / 수동 실행 ─────────────────────────────────
def test_manual_run_has_no_schedule(env):
    env(PAGE76_SCHEDULE_CRON="")
    now = utc(2026, 11, 10, 12, 0)
    assert sched.scheduled_at(now) is None
    assert sched.lag_seconds(now) is None
    assert sched.effective_now(now) == now
    assert sched.too_late(now) is False
    assert sched.cron_invalid() is False


def test_scheduled_run_effective_now_and_lag(env):
    env(PAGE76_SCHEDULE_CRON="28 12 * * *")
    now = utc(2026, 11, 10, 15, 30)  # KST 11-11 00:30 시작 (3시간 지연)
    assert sched.scheduled_at(now) == utc(2026, 11, 10, 12, 28)
    assert sched.effective_now(now) == utc(2026, 11, 10, 12, 28)
    assert sched.lag_seconds(now) == (3 * 60 + 2) * 60


def test_too_late_boundary(env):
    env(PAGE76_SCHEDULE_CRON="5 11 * * *")
    assert sched.too_late(utc(2026, 11, 10, 12, 35, 0)) is False   # 정확히 90분
    assert sched.too_late(utc(2026, 11, 10, 12, 35, 1)) is True    # 초과
    assert sched.too_late(utc(2026, 11, 10, 11, 45)) is False      # 랜덤 대기 40분 이내


def test_max_lag_override(env):
    env(PAGE76_SCHEDULE_CRON="5 11 * * *", PAGE76_MAX_LAG_MIN="30")
    assert sched.too_late(utc(2026, 11, 10, 11, 36)) is True


def test_cron_invalid(env):
    env(PAGE76_SCHEDULE_CRON="not a cron")
    now = utc(2026, 11, 10, 12, 0)
    assert sched.cron_invalid() is True
    assert sched.scheduled_at(now) is None and sched.effective_now(now) == now
    assert sched.too_late(now) is False

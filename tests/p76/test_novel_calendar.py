"""사건 소설 일정 엔진 (p76.novel_calendar, DSN-P76-NOVEL-003 §4 · DN2026_0119). 시계·노션 없음."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from p76 import novel_calendar as cal
from p76 import settings as S


@pytest.fixture
def start(monkeypatch):
    monkeypatch.setattr(S, "NOVEL_START", date(2026, 11, 28))  # 토요일
    return S.NOVEL_START


def test_friday_is_never_publish_day():
    for i in range(60):
        d = date(2026, 11, 1) + timedelta(days=i)
        assert cal.is_publish_day(d) == (d.weekday() != 4)


def test_shift_date_excluded():
    assert not cal.is_publish_day(date(2026, 11, 30), [date(2026, 11, 30)])
    assert not cal.is_publish_day(date(2026, 11, 30), ["2026-11-30"])  # ISO 문자열도 받는다
    assert cal.is_publish_day(date(2026, 12, 1), ["2026-11-30"])


def test_nth_publish_day_skips_friday_and_starts_on_next_publish_day():
    sat = date(2026, 11, 28)
    days = [cal.nth_publish_day(sat, k) for k in range(1, 8)]
    assert days == [date(2026, 11, 28), date(2026, 11, 29), date(2026, 11, 30), date(2026, 12, 1),
                    date(2026, 12, 2), date(2026, 12, 3), date(2026, 12, 5)]  # 12-04(금) 제외
    fri = date(2026, 12, 4)
    assert cal.nth_publish_day(fri, 1) == date(2026, 12, 5)
    with pytest.raises(ValueError):
        cal.nth_publish_day(sat, 0)


def test_due_and_season_boundary(start):
    assert cal.due(1, 1) == start
    assert cal.due(1, 7) == date(2026, 12, 5)            # 금요일 하루 건너뜀
    assert cal.season_start(2) == cal.due(2, 1) == date(2026, 12, 6)  # 시즌 연속(사이 휴재 없음)
    assert cal.due(2, 7) == date(2026, 12, 13)
    assert cal.global_index(2, 1) == S.NOVEL_EPISODES + 1


def test_shifts_push_later_episodes_only(start):
    shifts = [date(2026, 11, 30)]
    assert cal.due(1, 2, shifts) == date(2026, 11, 29)   # 순연일 이전 회차는 그대로
    assert cal.due(1, 3, shifts) == date(2026, 12, 1)    # 이후 회차는 하루씩 밀림
    assert cal.due(1, 7, shifts) == date(2026, 12, 6)
    assert cal.season_start(2, shifts) == date(2026, 12, 7)
    two = shifts + [date(2026, 12, 1)]
    assert cal.due(1, 3, two) == date(2026, 12, 2)


def test_year_end_boundary(monkeypatch):
    monkeypatch.setattr(S, "NOVEL_START", date(2026, 12, 28))  # 월
    assert [cal.due(1, m) for m in range(1, 8)] == [
        date(2026, 12, 28), date(2026, 12, 29), date(2026, 12, 30), date(2026, 12, 31),
        date(2027, 1, 2), date(2027, 1, 3), date(2027, 1, 4)]  # 2027-01-01(금) 휴재
    assert cal.due(1, 5, ["2027-01-02"]) == date(2027, 1, 3)


def test_parse_shifts_and_labels():
    assert cal.parse_shifts(["2026-12-01", "bad", "2026-11-30", "2026-12-01"]) == (date(2026, 11, 30),
                                                                                   date(2026, 12, 1))
    assert cal.parse_shifts(None) == () and cal.parse_shifts("2026-11-30") == ()
    assert cal.weekday_ko(date(2026, 11, 29)) == "일" and cal.weekday_ko(date(2026, 12, 4)) == "금"
    assert cal.label(date(2026, 11, 30)) == "11-30(월)"


def test_start_not_configured(monkeypatch):
    monkeypatch.setattr(S, "NOVEL_START", None)
    with pytest.raises(ValueError):
        cal.due(1, 1)

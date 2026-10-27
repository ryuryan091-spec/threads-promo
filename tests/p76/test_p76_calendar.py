"""T1-01~05 시즌·캘린더 계산, FR-01~03."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from p76_fake import DB09, add_episode

from p76 import calendar_plan, run_calendar, settings
from p76.runlog import RunLog
from p76.season import is_finale, locate

EP, HC, HI = settings.KIND_EPISODE, settings.KIND_HIATUS_CONTENT, settings.KIND_HIATUS


@pytest.mark.parametrize("e,expected", [
    (1, (1, 1, 20)), (20, (1, 20, 20)), (21, (2, 1, 15)), (35, (2, 15, 15)),
    (36, (3, 1, 15)), (50, (3, 15, 15)), (51, (4, 1, 15)),
])
def test_t1_01_locate(e, expected):
    assert locate(e) == expected


def test_locate_rejects_zero():
    with pytest.raises(ValueError):
        locate(0)


def test_finales_up_to_100():
    assert [e for e in range(1, 101) if is_finale(e)] == [20, 35, 50, 65, 80, 95]


def _plan(no, day, today, horizon=21, hiatus=7):
    return calendar_plan.plan(no, day, today, horizon, hiatus)


def test_t1_02_season2_finale_on_time():
    out = _plan(35, date(2026, 11, 9), date(2026, 11, 9))
    assert out[date(2026, 11, 9)].number == 35
    hiatus = {d: e for d, e in out.items() if e.kind != EP}
    assert sorted(hiatus) == [date(2026, 11, 10) + timedelta(days=i) for i in range(7)]
    assert {e.hiatus_day: e.content for e in hiatus.values() if e.kind == HC} == {
        1: settings.CONTENT_SUMMARY, 4: settings.CONTENT_VOTE, 7: settings.CONTENT_TEASER}
    assert all(e.season == 2 for e in hiatus.values())
    first = out[date(2026, 11, 17)]
    assert (first.kind, first.number, first.season) == (EP, 36, 3)
    assert out[date(2026, 11, 18)].number == 37


def test_t1_03_finale_published_one_day_late():
    out = _plan(35, date(2026, 11, 10), date(2026, 11, 10))
    assert out[date(2026, 11, 11)].hiatus_day == 1
    assert out[date(2026, 11, 17)].hiatus_day == 7
    assert out[date(2026, 11, 18)].number == 36


def test_projection_before_finale():
    out = _plan(30, date(2026, 11, 4), date(2026, 11, 5))
    assert out[date(2026, 11, 5)].number == 31
    assert out[date(2026, 11, 9)].number == 35
    assert out[date(2026, 11, 10)].kind == HC
    assert out[date(2026, 11, 17)].number == 36


def test_delayed_episode_not_placed_in_past():
    out = _plan(30, date(2026, 11, 4), date(2026, 11, 7))
    assert out[date(2026, 11, 7)].number == 31
    assert min(out) == date(2026, 11, 7)


def test_today_inside_hiatus():
    out = _plan(35, date(2026, 11, 9), date(2026, 11, 12))
    assert out[date(2026, 11, 12)].hiatus_day == 3
    assert out[date(2026, 11, 17)].number == 36


def test_content_days_other_lengths():
    assert calendar_plan.content_days(5) == {1: settings.CONTENT_SUMMARY, 3: settings.CONTENT_VOTE,
                                             5: settings.CONTENT_TEASER}


def test_t1_05_365_day_simulation():
    """매일 계획대로 발행된다고 보고 1년 진행 — 본편 15연속 + 휴재 7 반복, 겹침·누락 0."""
    anchor_no, anchor_day = 21, date(2026, 10, 26)
    timeline: dict[date, str] = {anchor_day: EP}
    day = anchor_day
    for _ in range(365):
        day += timedelta(days=1)
        today_plan = _plan(anchor_no, anchor_day, day)
        entry = today_plan[day]
        assert day not in timeline
        timeline[day] = entry.kind
        if entry.kind == EP:
            assert entry.number == anchor_no + 1
            anchor_no, anchor_day = entry.number, day
    kinds = "".join("E" if timeline[d] == EP else "H" for d in sorted(timeline))
    runs = [len(r) for r in kinds.replace("H", " ").split()]
    gaps = [len(r) for r in kinds.replace("E", " ").split()]
    assert runs[0] == 15 and all(r == 15 for r in runs[1:-1])
    assert all(g == 7 for g in gaps)


# ── run_calendar (노션 upsert) ────────────────────────────
def _run(today):
    log = RunLog("calendar", "t")
    run_calendar.body(log, today=today)
    return log


def test_t1_04_idempotent_and_protected(fake_notion):
    add_episode(fake_notion, 34, date(2026, 11, 8))
    add_episode(fake_notion, 35, date(2026, 11, 9))
    log = _run(date(2026, 11, 9))
    assert log.writes == 21 and not log.codes
    rows = {fake_notion.val(p, settings.C_KEY): p for p in fake_notion.rows(DB09)}
    assert fake_notion.val(rows["2026-11-10"], settings.C_CONTENT) == settings.CONTENT_SUMMARY
    assert fake_notion.val(rows["2026-11-17"], settings.C_NO) == 36
    assert fake_notion.val(rows["2026-11-10"], settings.C_STATE) == settings.S_DRAFT

    # 회장이 본문을 쓴 행은 이후 계산이 바뀌어도 그대로 둔다
    edited = rows["2026-11-12"]
    edited["properties"][settings.C_BODY]["rich_text"] = [{"plain_text": "회장 작성"}]
    before = len(fake_notion.writes())
    log2 = _run(date(2026, 11, 9))
    assert log2.writes == 0 and len(fake_notion.writes()) == before


def test_calendar_shift_updates_unprotected_rows(fake_notion):
    add_episode(fake_notion, 34, date(2026, 11, 8))
    _run(date(2026, 11, 8))
    # 완결 회가 하루 늦게 발행 → 휴재가 하루 밀린다
    add_episode(fake_notion, 35, date(2026, 11, 10))
    log = _run(date(2026, 11, 10))
    rows = {fake_notion.val(p, settings.C_KEY): p for p in fake_notion.rows(DB09)}
    assert fake_notion.val(rows["2026-11-10"], settings.C_KIND) == EP
    assert fake_notion.val(rows["2026-11-11"], settings.C_HIATUS_DAY) == 1
    assert fake_notion.val(rows["2026-11-18"], settings.C_NO) == 36
    assert log.writes > 0
    assert len([p for p in fake_notion.rows(DB09) if fake_notion.val(p, settings.C_KEY) == "2026-11-12"]) == 1


def test_calendar_dry_run_writes_nothing(fake_notion, env):
    env(PAGE76_MODE="dry_run")
    add_episode(fake_notion, 35, date(2026, 11, 9))
    log = _run(date(2026, 11, 9))
    assert fake_notion.writes() == [] and log.writes == 0


def test_calendar_db05_failure_keeps_rows(fake_notion):
    from p76.notion import NotionError
    fake_notion.fail = lambda m, p: NotionError("POST http_503:x") if "/query" in p else None
    log = _run(date(2026, 11, 9))
    assert log.status == "failed" and fake_notion.writes() == []


def test_calendar_no_published(fake_notion):
    log = _run(date(2026, 11, 9))
    assert log.codes == ["db05_no_published"]


# ── 운영 계획 기준점 (DB-05 발행 기록이 없는 반수동 운영, 2026-10-28 실측) ──
def test_parse_anchor():
    assert settings.parse_anchor("21@2026-10-26") == (21, date(2026, 10, 26))
    assert settings.parse_anchor(" 21 @ 2026-10-26 ") == (21, date(2026, 10, 26))
    assert settings.parse_anchor("") is None and settings.parse_anchor("21/10-26") is None


def test_plan_without_clamp_follows_schedule():
    out = _plan_nc(21, date(2026, 10, 26), date(2026, 10, 28))
    assert out[date(2026, 10, 28)].number == 23
    assert out[date(2026, 11, 9)].number == 35
    assert out[date(2026, 11, 10)].kind == HC and out[date(2026, 11, 16)].hiatus_day == 7
    assert out[date(2026, 11, 17)].number == 36


def _plan_nc(no, day, today):
    return calendar_plan.plan(no, day, today, 21, 7, clamp=False)


def test_schedule_anchor_used_when_db05_is_behind(fake_notion, env):
    env(PAGE76_SCHEDULE_ANCHOR="21@2026-10-26")
    add_episode(fake_notion, 7, date(2026, 10, 13))
    log = _run(date(2026, 10, 28))
    rows = {fake_notion.val(p, settings.C_KEY): p for p in fake_notion.rows(DB09)}
    assert fake_notion.val(rows["2026-10-28"], settings.C_NO) == 23
    assert fake_notion.val(rows["2026-11-10"], settings.C_CONTENT) == settings.CONTENT_SUMMARY
    assert log.status == "ok"


def test_db05_wins_when_ahead_of_schedule(fake_notion, env):
    env(PAGE76_SCHEDULE_ANCHOR="21@2026-10-26")
    add_episode(fake_notion, 35, date(2026, 11, 10))  # 하루 늦은 완결
    _run(date(2026, 11, 10))
    rows = {fake_notion.val(p, settings.C_KEY): p for p in fake_notion.rows(DB09)}
    assert fake_notion.val(rows["2026-11-11"], settings.C_HIATUS_DAY) == 1


def test_invalid_anchor_falls_back(fake_notion, env):
    env(PAGE76_SCHEDULE_ANCHOR="21/10-26")
    add_episode(fake_notion, 35, date(2026, 11, 9))
    log = _run(date(2026, 11, 9))
    assert "schedule_anchor_invalid" in log.codes and log.writes == 21


def test_no_anchor_at_all(fake_notion, env):
    env(PAGE76_SCHEDULE_ANCHOR="")
    assert _run(date(2026, 11, 9)).codes == ["db05_no_published"]

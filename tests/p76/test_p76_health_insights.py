"""T1-12 모드 파서 · T1-13 health · T1-14 실행 기록 · T1-15 insights."""
from __future__ import annotations

from datetime import date

import pytest
from p76_fake import DB08, DB09, DB11, FAKE_PAGE_ID, FakeResp, add_episode, err, kst

from p76 import cli, run_health, run_insights, safety, settings
from p76.runlog import RunLog

TODAY = date(2026, 11, 12)
SCOPES = ["pages_show_list", "business_management", "pages_read_engagement", "pages_manage_posts"]


# ── T1-12 ────────────────────────────────────────────────
@pytest.mark.parametrize("raw,mode,warn", [
    ("", "dry_run", False), (None, "dry_run", False), ("-", "dry_run", False), ("livee", "dry_run", True),
    ("LIVE", "live", False), ("shadow", "shadow", False), ("dry_run", "dry_run", False),
])
def test_t1_12_mode_parser(raw, mode, warn):
    assert settings.parse_mode(raw) == (mode, warn)


def test_defaults_are_safe(monkeypatch):
    for key in ("PAGE76_ENABLED", "PAGE76_MODE", "PAGE76_HIATUS_PUBLISH_ENABLED", "PAGE76_INSIGHTS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    settings.reload()
    try:
        assert (settings.ENABLED, settings.MODE, settings.HIATUS_PUBLISH_ENABLED, settings.INSIGHTS_ENABLED) == (
            False, "dry_run", False, False)
    finally:
        monkeypatch.undo()
        settings.reload()


# ── T1-13 health ─────────────────────────────────────────
@pytest.fixture
def health_env(fake_notion, fake_graph):
    g = fake_graph
    g.handlers[("GET", f"{FAKE_PAGE_ID}?id,name,followers_count")] = FakeResp(200, {"id": "x", "followers_count": 10})
    g.handlers[("GET", "debug_token")] = FakeResp(200, {"data": {"is_valid": True, "type": "PAGE", "expires_at": 0,
                                                                 "scopes": SCOPES}})
    add_episode(fake_notion, 36, date(2026, 11, 11), feed_id="61_36")
    fake_notion.add(DB09, **{settings.C_KEY: TODAY.isoformat(), settings.C_DATE: TODAY.isoformat(),
                             settings.C_KIND: settings.KIND_EPISODE, settings.C_NO: 37, settings.C_STATE: "초안"})
    add_episode(fake_notion, 37, None, state="초안")
    return fake_notion, g


def health_codes():
    log = RunLog("health", "t")
    run_health.body(log, today=TODAY)
    return log


def test_t1_13_all_ok(health_env):
    log = health_codes()
    assert log.status == "ok", log.codes


def test_t1_13_token_invalid_190(health_env):
    _, g = health_env
    g.handlers[("GET", f"{FAKE_PAGE_ID}?id,name,followers_count")] = err(190, 401)
    log = health_codes()
    assert log.status == "tripped" and any(c.startswith("H1:") for c in log.codes)


@pytest.mark.parametrize("scopes,code", [
    (SCOPES[:-1], "H2:scope_missing:pages_manage_posts"),
    (SCOPES + ["pages_messaging"], "H2:scope_extra:pages_messaging"),
])
def test_t1_13_scope_drift(health_env, scopes, code):
    _, g = health_env
    g.handlers[("GET", "debug_token")] = FakeResp(200, {"data": {"is_valid": True, "type": "PAGE", "expires_at": 0,
                                                                 "scopes": scopes}})
    assert code in health_codes().codes


def test_t1_13_schema_mismatch(health_env):
    fake, _ = health_env
    fake.schemas[DB08].pop(settings.M_SHARES)
    log = health_codes()
    assert log.status == "failed" and any(c.startswith("H5:DB08:missing:공유수") for c in log.codes)


def test_t1_13_approved_during_hiatus(health_env):
    fake, _ = health_env
    row = next(p for p in fake.rows(DB09))
    row["properties"][settings.C_KIND]["select"] = {"name": settings.KIND_HIATUS}
    add_episode(fake, 38, None, state=settings.S_APPROVED)
    assert "H6:approved_during_hiatus:#038" in health_codes().codes


def test_t1_13_scheduled_task_missing(health_env):
    fake, _ = health_env
    row = next(p for p in fake.rows(DB09))
    row["properties"][settings.C_NO]["number"] = 99
    assert "H7:draft_missing:#099" in health_codes().codes


def test_health_runs_when_disabled(health_env, env, monkeypatch):
    env(PAGE76_ENABLED="false", PAGE76_MODE="shadow")
    assert cli.execute("health", "t", lambda log: run_health.body(log, today=TODAY), require_enabled=False) == 0
    fake, _ = health_env
    assert not any(c[1] == "/pages" for c in fake.writes())  # 꺼져 있으면 실행 기록도 남기지 않음


# ── T1-14 실행 기록 ──────────────────────────────────────
@pytest.mark.parametrize("behaviour,status,exit_code", [
    (lambda log: None, "ok", 0),
    (lambda log: log.fail("x"), "failed", 1),
    (lambda log: 1 / 0, "failed", 1),
    (lambda log: safety.trip("t"), "tripped", 7),
])
def test_t1_14_one_runlog_row(fake_notion, env, behaviour, status, exit_code):
    env(PAGE76_MODE="shadow")
    assert cli.execute("calendar", "9.9", behaviour) == exit_code
    rows = fake_notion.rows(DB11)
    assert len(rows) == 1
    assert fake_notion.val(rows[0], settings.R_STATUS) == status
    assert fake_notion.val(rows[0], settings.R_RUN_ID) == "4242-calendar"
    assert fake_notion.val(rows[0], settings.R_MODE) == "shadow"


def test_runlog_skipped_in_dry_run(fake_notion, env):
    env(PAGE76_MODE="dry_run")
    cli.execute("calendar", "9.9", lambda log: None)
    assert fake_notion.rows(DB11) == []


def test_disabled_pipeline_exits_without_work(fake_notion, env):
    env(PAGE76_ENABLED="false")
    called = []
    assert cli.execute("calendar", "9.9", called.append) == 0
    assert called == [] and fake_notion.calls == []


# ── T1-15 insights ───────────────────────────────────────
@pytest.fixture
def insights_env(fake_notion, fake_graph):
    g = fake_graph
    g.handlers[("GET", f"{FAKE_PAGE_ID}?id,name,followers_count")] = FakeResp(200, {"followers_count": 321})
    g.handlers[("GET", "61_35?reactions.summary(total_count).limit(0),shares")] = FakeResp(
        200, {"reactions": {"summary": {"total_count": 40}}})
    g.handlers[("GET", "61_35?comments.summary(total_count).limit(0)")] = FakeResp(
        200, {"comments": {"summary": {"total_count": 7}}})
    add_episode(fake_notion, 35, date(2026, 11, 9), feed_id="61_35", reel_id="v35")
    add_episode(fake_notion, 36, date(2026, 11, 10), feed_id="")  # D2 — 대상 아님
    return fake_notion, g


def run_insights_body(today=TODAY):
    log = RunLog("insights", "t")
    run_insights.body(log, today=today)
    return log


def test_t1_15_engagement_saved(insights_env):
    fake, g = insights_env
    run_insights_body()
    rows = fake.rows(DB08)
    assert len(rows) == 1
    row = rows[0]
    assert fake.val(row, settings.M_KEY) == "#035-D3"
    assert (fake.val(row, settings.M_REACTIONS), fake.val(row, settings.M_COMMENTS), fake.val(row, settings.M_SHARES),
            fake.val(row, settings.M_FOLLOWERS), fake.val(row, settings.M_SEASON)) == (40, 7, 0, 321, 2)
    assert fake.val(row, settings.M_VIEWS) is None  # read_insights 없음 → 빈칸
    assert "insights:skipped" in fake.val(row, settings.M_RESULT)
    assert "reel:not_collected" in fake.val(row, settings.M_RESULT)
    assert g.writes() == []


def test_t1_15_snapshot_idempotent(insights_env):
    fake, _ = insights_env
    run_insights_body()
    run_insights_body()
    assert len(fake.rows(DB08)) == 1


def test_t1_15_comments_permission_partial(insights_env):
    fake, g = insights_env
    g.handlers[("GET", "61_35?comments.summary(total_count).limit(0)")] = err(100)
    log = run_insights_body()
    row = fake.rows(DB08)[0]
    assert fake.val(row, settings.M_REACTIONS) == 40 and fake.val(row, settings.M_COMMENTS) is None
    assert "comments:" in fake.val(row, settings.M_RESULT) and log.status == "degraded"


def test_t1_15_invalid_metric_partial(insights_env, env):
    fake, g = insights_env
    env(PAGE76_INSIGHTS_METRICS="post_media_view,post_impressions")

    def insights(params):
        if "," in params["metric"] or params["metric"] == "post_impressions":
            return err(100)
        return FakeResp(200, {"data": [{"name": "post_media_view", "values": [{"value": 900}]}]})
    g.handlers[("GET", "61_35/insights")] = insights
    run_insights_body()
    row = fake.rows(DB08)[0]
    assert fake.val(row, settings.M_VIEWS) == 900
    assert "post_impressions" in fake.val(row, settings.M_RESULT)


def test_insights_hiatus_content_snapshot(insights_env):
    fake, g = insights_env
    fake.add(DB09, **{settings.C_KEY: "2026-11-11", settings.C_DATE: "2026-11-11",
                      settings.C_KIND: settings.KIND_HIATUS_CONTENT, settings.C_STATE: settings.S_DONE,
                      settings.C_SEASON: 2, settings.C_FEED_ID: "61_h1",
                      settings.C_PUBLISHED_AT: kst(date(2026, 11, 11))})
    g.handlers[("GET", "61_h1?reactions.summary(total_count).limit(0),shares")] = FakeResp(200, {"shares": {"count": 2}})
    g.handlers[("GET", "61_h1?comments.summary(total_count).limit(0)")] = FakeResp(200, {})
    run_insights_body()
    keys = sorted(fake.val(r, settings.M_KEY) for r in fake.rows(DB08))
    assert keys == ["#035-D3", "H2026-11-11-D1"]


def test_insights_dry_run_no_writes(insights_env, env):
    fake, _ = insights_env
    env(PAGE76_MODE="dry_run")
    run_insights_body()
    assert fake.writes() == []


def test_insights_trip_stops(insights_env):
    fake, g = insights_env
    g.handlers[("GET", f"{FAKE_PAGE_ID}?id,name,followers_count")] = err(190, 401)
    log = run_insights_body()
    assert log.status == "tripped" and fake.rows(DB08) == []

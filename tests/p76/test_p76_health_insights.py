"""T1-12 모드 파서 · T1-13 health · T1-14 실행 기록 · T1-15 insights."""
from __future__ import annotations

from datetime import date

import pytest
from p76_fake import DB08, DB09, DB11, FAKE_PAGE_ID, FakeResp, add_episode, err

from p76 import cli, run_health, run_insights, safety, settings
from p76.runlog import RunLog

TODAY = date(2026, 11, 12)
SCOPES = ["pages_show_list", "business_management", "pages_read_engagement", "pages_manage_posts",
          "public_profile"]


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


def test_empty_workflow_variables_use_defaults(env):
    """등록되지 않은 Variable 은 워크플로에서 빈 문자열로 들어온다 (2026-10-28 첫 실행 H2 오경고)."""
    env(PAGE76_EXPECTED_SCOPES="", PAGE76_HIATUS_DAYS="", PAGE76_MODE="")
    assert "pages_manage_posts" in settings.EXPECTED_SCOPES and "public_profile" in settings.EXPECTED_SCOPES
    assert settings.HIATUS_DAYS == 7 and settings.MODE == "dry_run"


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
    ([s for s in SCOPES if s != "pages_manage_posts"], "H2:scope_missing:pages_manage_posts"),
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


# ── T1-15 insights (페이지 게시물 목록 기준) ──────────────
def post(pid, created, status="added_photos"):
    return {"id": pid, "created_time": created, "status_type": status}


@pytest.fixture
def insights_env(fake_notion, fake_graph, env):
    env(PAGE76_SCHEDULE_ANCHOR="21@2026-10-26")
    g = fake_graph
    g.handlers[("GET", f"{FAKE_PAGE_ID}?id,name,followers_count")] = FakeResp(200, {"followers_count": 321})
    # TODAY=11-12: 11-09(D3) #035 피드 + 릴스, 11-10(D2) 휴재 1일차, 11-11(D1) 휴재 콘텐츠 없음
    g.handlers[("GET", f"{FAKE_PAGE_ID}/posts")] = FakeResp(200, {"data": [
        post("61_35", "2026-11-09T11:31:00+0000", "added_video"),   # 회차 릴스
        post("61_35x", "2026-11-09T13:40:00+0000", "added_video"),  # 같은 날 추가 영상
        post("61_h1", "2026-11-10T11:20:00+0000"),
    ]})
    g.handlers[("GET", "61_35?reactions.summary(total_count).limit(0),shares")] = FakeResp(
        200, {"reactions": {"summary": {"total_count": 40}}})
    g.handlers[("GET", "61_35?comments.summary(total_count).limit(0)")] = FakeResp(
        200, {"comments": {"summary": {"total_count": 7}}})
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
    assert (fake.val(row, settings.M_NO), fake.val(row, settings.M_REACTIONS), fake.val(row, settings.M_COMMENTS),
            fake.val(row, settings.M_SHARES), fake.val(row, settings.M_FOLLOWERS), fake.val(row, settings.M_SEASON)) == (
        35, 40, 7, 0, 321, 2)
    assert fake.val(row, settings.M_VIEWS) is None  # read_insights 없음 → 빈칸
    assert "insights:skipped" in fake.val(row, settings.M_RESULT)
    assert "reel:views_not_collected" in fake.val(row, settings.M_RESULT)
    assert "multi_post:2" in fake.val(row, settings.M_RESULT)
    assert not any(c[1].startswith("61_35x") for c in g.calls)
    assert g.writes() == []
    posts_call = next(c for c in g.calls if c[1].endswith("/posts"))
    assert "message" not in posts_call[2]["fields"]  # 본문은 가져오지 않는다


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
    env(PAGE76_INSIGHTS_METRICS="post_media_view,post_impressions", PAGE76_SCHEDULE_ANCHOR="21@2026-10-26")

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
    g.handlers[("GET", "61_h1?reactions.summary(total_count).limit(0),shares")] = FakeResp(200, {"shares": {"count": 2}})
    g.handlers[("GET", "61_h1?comments.summary(total_count).limit(0)")] = FakeResp(200, {})
    run_insights_body(date(2026, 11, 11))  # 11-10 게시 → D1
    keys = sorted(fake.val(r, settings.M_KEY) for r in fake.rows(DB08))
    assert keys == ["H2026-11-10-D1"]


def test_insights_db09_row_overrides_plan(insights_env):
    """회장이 DB-09 에서 날짜를 바꾼 경우(예: 하루 쉼) 캘린더 행을 따른다."""
    fake, _ = insights_env
    fake.add(DB09, **{settings.C_KEY: "2026-11-09", settings.C_DATE: "2026-11-09",
                      settings.C_KIND: settings.KIND_EPISODE, settings.C_SEASON: 2, settings.C_NO: 34})
    run_insights_body()
    assert [fake.val(r, settings.M_KEY) for r in fake.rows(DB08)] == ["#034-D3"]


def test_insights_unmapped_without_anchor(insights_env, env):
    fake, _ = insights_env
    env(PAGE76_SCHEDULE_ANCHOR="")
    log = run_insights_body()
    row = fake.rows(DB08)[0]
    assert fake.val(row, settings.M_KEY) == "P2026-11-09-D3" and fake.val(row, settings.M_NO) is None
    assert "unmapped" in fake.val(row, settings.M_RESULT) and log.status == "degraded"


def test_insights_multiple_feed_posts_same_day(insights_env):
    fake, g = insights_env
    g.handlers[("GET", f"{FAKE_PAGE_ID}/posts")] = FakeResp(200, {"data": [
        post("61_late", "2026-11-09T13:00:00+0000"), post("61_35", "2026-11-09T11:31:00+0000")]})
    log = run_insights_body()
    row = fake.rows(DB08)[0]
    assert "multi_post:2" in fake.val(row, settings.M_RESULT) and log.status == "ok"
    assert not any(c[1].startswith("61_late") for c in g.calls)


def test_insights_posts_pagination(insights_env):
    fake, g = insights_env
    pages = iter([
        FakeResp(200, {"data": [post("61_h1", "2026-11-10T11:20:00+0000")],
                       "paging": {"cursors": {"after": "c1"}, "next": "https://graph.example/next"}}),
        FakeResp(200, {"data": [post("61_35", "2026-11-09T11:31:00+0000")]}),
    ])
    g.handlers[("GET", f"{FAKE_PAGE_ID}/posts")] = lambda params: next(pages)
    run_insights_body()
    assert [fake.val(r, settings.M_KEY) for r in fake.rows(DB08)] == ["#035-D3"]


def test_insights_posts_failure_degrades(insights_env):
    fake, g = insights_env
    g.handlers[("GET", f"{FAKE_PAGE_ID}/posts")] = err(100)
    log = run_insights_body()
    assert fake.rows(DB08) == [] and any(c.startswith("posts:") for c in log.codes)


def test_insights_dry_run_no_writes(insights_env, env):
    fake, _ = insights_env
    env(PAGE76_MODE="dry_run", PAGE76_SCHEDULE_ANCHOR="21@2026-10-26")
    run_insights_body()
    assert fake.writes() == []


def test_insights_trip_stops(insights_env):
    fake, g = insights_env
    g.handlers[("GET", f"{FAKE_PAGE_ID}?id,name,followers_count")] = err(190, 401)
    log = run_insights_body()
    assert log.status == "tripped" and fake.rows(DB08) == []


def test_insights_diagnostic_log(insights_env, caplog):
    import logging
    caplog.set_level(logging.INFO)
    run_insights_body()
    line = next(r.getMessage() for r in caplog.records if "게시물 3건" in r.getMessage())
    assert "added_photos,added_video" in line and "11-09(D3):피드0·영상2" in line
    assert "61_" not in caplog.text  # 게시물 ID 는 공개 로그에 남기지 않는다


def test_insights_earliest_post_regardless_of_type(insights_env):
    """회차는 릴스로 게시 — 같은 날 사진이 더 늦게 올라와도 가장 이른 영상이 회차다."""
    fake, g = insights_env
    g.handlers[("GET", f"{FAKE_PAGE_ID}/posts")] = FakeResp(200, {"data": [
        post("61_photo", "2026-11-09T14:00:00+0000"), post("61_35", "2026-11-09T11:31:00+0000", "added_video")]})
    run_insights_body()
    row = fake.rows(DB08)[0]
    assert fake.val(row, settings.M_REACTIONS) == 40
    assert not any(c[1].startswith("61_photo") for c in g.calls)

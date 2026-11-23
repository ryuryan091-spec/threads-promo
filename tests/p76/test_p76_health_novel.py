"""T-HN 사건 소설 감시 (DN2026_0119, DSN-P76-NOVEL-003 §9) — H-N1·H-N2·H-N4·H-N5·일일 요약. 합성 데이터만."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from p76_fake import DB09, DB11, DB12, FAKE_PAGE_ID, FakeResp, add_episode

from p76 import notion, run_health, settings
from p76.runlog import RunLog

S = settings
TODAY = date(2026, 11, 12)
EVENING = datetime(2026, 11, 12, 19, 0, tzinfo=notion.KST)
MORNING = datetime(2026, 11, 12, 9, 10, tzinfo=notion.KST)
SCOPES = ["pages_show_list", "business_management", "pages_read_engagement", "pages_manage_posts",
          "public_profile"]


def _token(**over):
    return FakeResp(200, {"data": {"is_valid": True, "type": "PAGE", "expires_at": 0, "scopes": SCOPES, **over}})


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, bool]] = []
    monkeypatch.setattr(run_health, "send", lambda m, urgent=False: out.append((m, urgent)) or True)
    return out


@pytest.fixture
def novel_env(env, fake_notion, fake_graph, sent):
    env(PAGE76_DB12_ID=DB12)
    g = fake_graph
    g.handlers[("GET", f"{FAKE_PAGE_ID}?id,name,followers_count")] = FakeResp(200, {"id": "x", "followers_count": 1})
    g.handlers[("GET", "debug_token")] = _token()
    add_episode(fake_notion, 36, date(2026, 11, 11), feed_id="61_36")
    fake_notion.add(DB09, **{S.C_KEY: TODAY.isoformat(), S.C_DATE: TODAY.isoformat(),
                             S.C_KIND: S.KIND_EPISODE, S.C_NO: 37, S.C_STATE: "초안"})
    add_episode(fake_notion, 37, None, state="초안")
    bible(fake_notion, S.NS_CANON)
    control(fake_notion, {"month": "2026-11", "spent": 12.345, "last_run": "x"})
    ep(fake_notion, 1, TODAY, S.NS_SCHEDULED)
    ep(fake_notion, 2, TODAY + timedelta(days=1), S.NS_APPROVED)
    return fake_notion, g


def bible(fake, state, n=1):
    return fake.add(DB12, **{S.N_KEY: f"N{n}-BIBLE", S.N_KIND: S.NK_BIBLE, S.N_STATE: state, S.N_SEASON: n})


def ep(fake, m, due, state, stop=False):
    return fake.add(DB12, **{S.N_KEY: f"N1-E{m:02d}", S.N_KIND: S.NK_EPISODE, S.N_STATE: state, S.N_SEASON: 1,
                             S.N_EPISODE: m, S.N_DUE: due.isoformat(), S.N_STOP: stop})


def control(fake, data):
    return fake.add(DB12, **{S.N_KEY: "CONTROL", S.N_KIND: S.NK_CONTROL, S.N_RESULT: json.dumps(data)})


def run_row(fake, started, writes=0, pipeline="novel", status="ok", error=""):
    return fake.add(DB11, **{S.R_RUN_ID: f"r-{pipeline}", S.R_PIPELINE: pipeline, S.R_STATUS: status,
                             S.R_WRITES: writes, S.R_START: started.isoformat(), S.R_ERROR: error})


def run(now=EVENING):
    log = RunLog("health", "t")
    run_health.body(log, today=now.date(), now=now)
    return log


def hn_codes(log):
    return [c for c in log.codes if c.startswith("H-N")]


def test_skip_without_db12(env, novel_env, sent, caplog):
    env(PAGE76_DB12_ID="")
    caplog.set_level("INFO")
    log = run()
    assert log.status == "ok" and hn_codes(log) == []
    assert "PAGE76_DB12_ID 미설정" in caplog.text
    assert not any("소설 요약" in m for m, _ in sent)


def test_all_ok_and_summary_read_only(novel_env, sent):
    fake, _ = novel_env
    run_row(fake, EVENING - timedelta(hours=3), writes=2)
    run_row(fake, EVENING - timedelta(hours=2), pipeline="calendar", status="degraded")
    log = run()
    assert log.status == "ok", log.codes
    assert fake.writes() == []  # 감시는 읽기만 (CONTROL 행도 만들지 않는다)
    summary = [m for m, u in sent if "소설 요약" in m]
    assert len(summary) == 1 and not any(u for m, u in sent if "소설 요약" in m)
    text = summary[0]
    assert "N1-E01 발행예고" in text and "N1-E02 승인" in text and "2026-11 $12.35" in text
    assert "calendar degraded1" in text and "novel ok1" in text and "정본1" in text
    for m, _ in sent:  # 노션 ID·DB ID 출력 금지 (SEC RR-06)
        assert DB12 not in m and DB11 not in m and not any(pid in m for pid in fake.pages)


def test_summary_cost_unknown_and_month_mismatch(novel_env, sent):
    fake, _ = novel_env
    ctrl = next(p for p in fake.rows(DB12) if fake.val(p, S.N_KEY) == "CONTROL")
    ctrl["properties"][S.N_RESULT]["rich_text"] = [{"plain_text": json.dumps({"month": "2026-10", "spent": 50})}]
    run()
    assert "2026-11 기록 없음" in sent[-1][0]
    del fake.pages[ctrl["id"]]
    run()
    assert "미확인(CONTROL 행 없음)" in sent[-1][0]
    assert fake.writes() == []


@pytest.mark.parametrize("state", [S.NS_APPROVED, S.NS_IMPLICIT, S.NS_REVIEW, ""])
def test_hn1_bible_state_outside(novel_env, sent, state):
    fake, _ = novel_env
    bible(fake, state, n=2)
    log = run()
    assert "H-N1:bible_state" in log.codes and log.status == "degraded"
    msg, urgent = next((m, u) for m, u in sent if "H-N1" in m)
    assert not urgent and "자동 재평가" in msg and "교체" not in msg


@pytest.mark.parametrize("state", [S.NS_CANON, S.NS_GENERATING, S.NS_CHECK_FAILED, S.NS_STOPPED, S.NS_DISCARDED])
def test_hn1_allowed_states(novel_env, state):
    fake, _ = novel_env
    bible(fake, state, n=2)
    assert "H-N1:bible_state" not in run().codes


def test_hn2_tomorrow_unready_evening_only(novel_env, sent):
    fake, _ = novel_env
    ep(fake, 3, TODAY + timedelta(days=1), S.NS_GENERATING)
    log = run(EVENING)
    assert "H-N2:tomorrow_unready" in log.codes and log.status == "degraded"
    msg, urgent = next((m, u) for m, u in sent if "H-N2" in m)
    assert not urgent and "N1-E03=생성중" in msg and "교체" not in msg
    assert "H-N2:tomorrow_unready" not in run(MORNING).codes


@pytest.mark.parametrize("state", [S.NS_APPROVED, S.NS_SCHEDULED, S.NS_PUBLISHED])
def test_hn2_ready_states(novel_env, state):
    fake, _ = novel_env
    ep(fake, 3, TODAY + timedelta(days=1), state)
    assert "H-N2:tomorrow_unready" not in run().codes


def test_hn4_novel_stalled_alert(novel_env, sent):
    fake, _ = novel_env
    run_row(fake, EVENING - timedelta(hours=20), error="")
    run_row(fake, EVENING - timedelta(hours=2), error="budget_stop")
    ep(fake, 4, TODAY + timedelta(days=2), S.NS_GENERATING)
    log = run()
    assert "H-N4:novel_stalled" in log.codes and log.status == "failed"
    msg, urgent = next((m, u) for m, u in sent if "H-N4" in m)
    assert urgent and "runs=2" in msg and "N1-E04=생성중" in msg and "budget_stop" in msg
    assert "교체" not in msg  # 토큰 교체 안내는 H1·H2·H-N5 만


@pytest.mark.parametrize("case", ["writes", "old_runs", "no_runs", "approved", "stopped", "far_due"])
def test_hn4_not_triggered(novel_env, case):
    fake, _ = novel_env
    started = MORNING - timedelta(hours=25 if case == "old_runs" else 2)
    if case != "no_runs":
        run_row(fake, started, writes=1 if case == "writes" else 0)
    due = TODAY + timedelta(days=5 if case == "far_due" else 1)
    state = S.NS_APPROVED if case == "approved" else S.NS_GENERATING
    ep(fake, 5, due, state, stop=case == "stopped")
    log = run(MORNING)  # H-N2 영향 없는 시각
    assert "H-N4:novel_stalled" not in log.codes, log.codes


def test_hn4_other_pipeline_zero_writes_ignored(novel_env):
    fake, _ = novel_env
    run_row(fake, EVENING - timedelta(hours=2), pipeline="novel_publish")
    ep(fake, 6, TODAY, S.NS_CHECK_FAILED)
    assert "H-N4:novel_stalled" not in run(MORNING).codes


def test_hn5_token_expiring_alert(novel_env, sent):
    _, g = novel_env
    g.handlers[("GET", "debug_token")] = _token(expires_at=int((EVENING + timedelta(days=10)).timestamp()))
    log = run()
    assert "H-N5:token_expiring" in log.codes and log.status == "failed"
    msg, urgent = next((m, u) for m, u in sent if "H-N5" in m)
    assert urgent and "D-10" in msg and "교체" in msg


def test_hn5_far_expiry_and_invalid(novel_env, sent):
    _, g = novel_env
    g.handlers[("GET", "debug_token")] = _token(expires_at=int((EVENING + timedelta(days=30)).timestamp()))
    log = run()
    assert not any(c.startswith("H-N5") for c in log.codes) and "H2:expires" in log.codes
    g.handlers[("GET", "debug_token")] = _token(is_valid=False)
    log = run()
    assert "H-N5:token_invalid" in log.codes


def test_hn5_expires_at_epoch_utc(novel_env):
    assert run_health.hn5_token({"is_valid": True, "expires_at": 0}, EVENING).level == run_health.OK
    soon = int(datetime(2026, 11, 20, tzinfo=UTC).timestamp())
    assert run_health.hn5_token({"is_valid": True, "expires_at": soon}, EVENING).code == "token_expiring"


def test_db12_error_degrades_without_crash(novel_env):
    fake, _ = novel_env
    fake.fail = lambda m, p: notion.NotionError("POST http_503:unknown") if DB12 in p else None
    log = run()
    assert any(c.startswith("H-N1:db12:") for c in log.codes) and log.status == "degraded"


# ── DN2026_0124 D3 ───────────────────────────────────────
def _set_state(fake, key, state):
    page = next(p for p in fake.rows(DB12) if fake.val(p, S.N_KEY) == key)
    page["properties"][S.N_STATE]["select"] = {"name": state}


def test_hn1_not_canon_within_d3_is_urgent_fail(novel_env, env, sent):
    fake, _ = novel_env
    env(PAGE76_DB12_ID=DB12, PAGE76_NOVEL_START=(TODAY + timedelta(days=2)).isoformat())  # 제1화 11-14(토)
    _set_state(fake, "N1-BIBLE", S.NS_HELD)
    log = run(MORNING)
    assert "H-N1:bible_not_canon_due" in log.codes and log.status == "failed"
    msg, urgent = next((m, u) for m, u in sent if "H-N1" in m)
    assert urgent and "N1-BIBLE=보류@S1화11-14" in msg


def test_hn1_not_canon_before_d3_stays_warn(novel_env, env, sent):
    fake, _ = novel_env
    env(PAGE76_DB12_ID=DB12, PAGE76_NOVEL_START=(TODAY + timedelta(days=4)).isoformat())
    _set_state(fake, "N1-BIBLE", S.NS_HELD)
    log = run(MORNING)
    assert "H-N1:bible_state" in log.codes and "H-N1:bible_not_canon_due" not in log.codes


def test_hn4_missing_episode_rows_detected(novel_env, env, sent):
    fake, _ = novel_env
    env(PAGE76_DB12_ID=DB12, PAGE76_NOVEL_START=TODAY.isoformat())  # 11-12(목) E1 · 11-14(토) E2 · 11-15 E3
    run_row(fake, MORNING - timedelta(hours=2), writes=0)
    log = run(MORNING)
    assert "H-N4:novel_stalled" in log.codes and log.status == "failed"
    msg, urgent = next((m, u) for m, u in sent if "H-N4" in m)
    assert urgent and "S1E3@11-15=missing" in msg and "S1E1" not in msg
    assert fake.writes() == []


def test_hn4_missing_not_judged_when_paused(novel_env, env):
    fake, _ = novel_env
    env(PAGE76_DB12_ID=DB12, PAGE76_NOVEL_START=TODAY.isoformat())
    ctrl = next(p for p in fake.rows(DB12) if fake.val(p, S.N_KEY) == "CONTROL")
    ctrl["properties"][S.N_RESULT]["rich_text"] = [{"plain_text": json.dumps({"paused": {"season": 1}})}]
    run_row(fake, MORNING - timedelta(hours=2), writes=0)
    assert "H-N4:novel_stalled" not in run(MORNING).codes


@pytest.mark.parametrize("state", [S.NS_GENERATING, S.NS_CHECK_FAILED])
def test_hn1_generating_within_d3_is_fail(novel_env, env, state):
    fake, _ = novel_env
    env(PAGE76_DB12_ID=DB12, PAGE76_NOVEL_START=(TODAY + timedelta(days=2)).isoformat())
    _set_state(fake, "N1-BIBLE", state)
    assert "H-N1:bible_not_canon_due" in run(MORNING).codes


@pytest.mark.parametrize("case", ["past_start", "discarded", "other_canon"])
def test_hn1_due_not_repeated(novel_env, env, case):
    fake, _ = novel_env
    start = TODAY - timedelta(days=1) if case == "past_start" else TODAY + timedelta(days=2)
    env(PAGE76_DB12_ID=DB12, PAGE76_NOVEL_START=start.isoformat())
    if case == "other_canon":
        bible(fake, S.NS_HELD, n=1)  # 같은 시즌에 정본이 이미 있음
    else:
        _set_state(fake, "N1-BIBLE", S.NS_HELD if case == "past_start" else S.NS_DISCARDED)
    assert "H-N1:bible_not_canon_due" not in run(MORNING).codes

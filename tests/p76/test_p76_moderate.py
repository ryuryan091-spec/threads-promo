"""댓글 자동 숨김 (DN2026_0119 A3, DSN-P76-NOVEL-003 §7 · p76/run_moderate.py). 합성 댓글만 쓴다(STD-TEST-14)."""
from __future__ import annotations

import logging
from datetime import datetime

import pytest
from p76_fake import DB09, DB11, DB12, FakeResp, err

from p76 import cli, graph, notion, run_moderate, safety, settings
from p76.runlog import RunLog

S = settings
NOW = datetime(2026, 11, 30, 12, 0, tzinfo=notion.KST)
TERM = "가나다상사"  # 합성 금칙어


@pytest.fixture
def mod(fake_notion, fake_graph, env):
    env(PAGE76_MODERATE_ENABLED="true", PAGE76_BANNED_TERMS=TERM, PAGE76_DB12_ID=DB12)
    fake_notion.add(DB12, **{S.N_KEY: "N1-E01", S.N_KIND: S.NK_EPISODE, S.N_STATE: S.NS_PUBLISHED,
                             S.N_FB_ID: "61_1", S.N_PUBLISHED_AT: "2026-11-29T12:00:05+09:00"})
    fake_notion.add(DB12, **{S.N_KEY: "N1-E00", S.N_KIND: S.NK_EPISODE, S.N_STATE: S.NS_PUBLISHED,
                             S.N_FB_ID: "61_0", S.N_PUBLISHED_AT: "2026-11-20T12:00:05+09:00"})  # 72시간 밖
    fake_notion.add(DB12, **{S.N_KEY: "CONTROL", S.N_KIND: S.NK_CONTROL, S.N_RESULT: "{}"})
    fake_notion.add(DB09, **{S.C_KEY: "2026-11-28", S.C_KIND: S.KIND_HIATUS_CONTENT, S.C_STATE: S.S_DONE,
                             S.C_FEED_ID: "61_2", S.C_PUBLISHED_AT: "2026-11-28T20:30:00+09:00"})
    fake_graph.handlers[("GET", "61_1/comments")] = FakeResp(200, {"data": [
        {"id": "c1", "message": f"{TERM} 얘기 같네요", "is_hidden": False},
        {"id": "c2", "message": "잘 읽었습니다", "is_hidden": False},
        {"id": "c3", "message": f"{TERM} 맞죠", "is_hidden": True}]})
    fake_graph.handlers[("GET", "61_2/comments")] = FakeResp(200, {"data": [
        {"id": "c4", "message": f"저도 {TERM}", "is_hidden": False}]})
    for cid in ("c1", "c4"):
        fake_graph.handlers[("POST", cid)] = FakeResp(200, {"success": True})
    return fake_notion, fake_graph


def run():
    log = RunLog("moderate", "t")
    run_moderate.body(log, now=NOW)
    return log


def test_switch_off(mod, env):
    fake, g = mod
    env(PAGE76_MODERATE_ENABLED="false", PAGE76_BANNED_TERMS=TERM, PAGE76_DB12_ID=DB12)
    run()
    assert g.calls == [] and fake.calls == []


def test_live_hides_matching_only(mod):
    _, g = mod
    log = run()
    hidden = sorted(c[1] for c in g.writes())
    assert hidden == ["c1", "c4"]  # 이미 숨김(c3)·무관(c2)·72시간 밖(61_0) 제외
    assert all(c[2]["is_hidden"] == "true" for c in g.writes())
    assert log.writes == 2 and log.status == "ok"
    assert not any(c[1].startswith("61_0") for c in g.calls)


@pytest.mark.parametrize("mode", ["dry_run", "shadow"])
def test_non_live_no_graph_writes(mod, env, mode):
    fake, g = mod
    env(PAGE76_MODE=mode, PAGE76_MODERATE_ENABLED="true", PAGE76_BANNED_TERMS=TERM, PAGE76_DB12_ID=DB12)
    run()
    assert g.writes() == [] and fake.writes() == []


def test_hide_failure_degrades(mod):
    _, g = mod
    g.handlers[("POST", "c1")] = err(100)
    log = run()
    assert "hide_failed:1" in log.codes


def test_permission_trip_stops(mod):
    _, g = mod
    g.handlers[("POST", "c1")] = err(200, 403)
    log = run()
    assert safety.tripped() and "graph:tripped" in log.codes
    assert len(g.writes()) == 1  # 회로 차단 뒤 추가 쓰기 없음


def test_no_terms_does_nothing(mod, env):
    _, g = mod
    env(PAGE76_MODERATE_ENABLED="true", PAGE76_BANNED_TERMS="", PAGE76_DB12_ID=DB12)
    log = run()
    assert g.calls == [] and "moderate:no_terms" in log.codes


def test_logs_and_db11_counts_only(mod, env, caplog):
    caplog.set_level(logging.DEBUG)
    fake, _ = mod
    env(PAGE76_MODERATE_ENABLED="true", PAGE76_BANNED_TERMS=TERM, PAGE76_DB12_ID=DB12)
    cli.execute("moderate", "t", lambda log: run_moderate.body(log, now=NOW))
    assert "hidden=2" in caplog.text
    record = [b for _m, _p, b in fake.writes() if b and b.get("parent", {}).get("database_id") == DB11]
    assert len(record) == 1
    blob = caplog.text + str(record)
    for secret in (TERM, "얘기 같네요", "c1", "c4", "61_1", "61_2"):
        assert secret not in blob


def test_graph_hide_and_delete_contract(fake_graph):
    fake_graph.handlers[("POST", "c9")] = FakeResp(200, {"success": True})
    fake_graph.handlers[("DELETE", "61_9")] = FakeResp(200, {})
    assert graph.hide_comment("c9")["status"] == "ok"
    assert graph.delete_post("61_9")["status"] == "unknown"  # success=true 가 없으면 미확정


def test_graph_writes_blocked_outside_live(fake_graph, env):
    env(PAGE76_MODE="shadow")
    for call in (lambda: graph.hide_comment("c9"), lambda: graph.delete_post("61_9")):
        with pytest.raises(safety.Tripped):
            call()
    assert fake_graph.writes() == []


# ── 리뷰 W7 (R5-05) ──────────────────────────────────────
def test_w7_queries_filter_by_lookback_and_sort(mod):
    fake, _ = mod
    run()
    qs = {path.split("/")[2]: b for m, path, b in fake.calls if m == "POST" and path.endswith("/query")}
    floor = "2026-11-27"  # NOW(11-30 12:00 KST) − 72시간
    q12, q09 = qs[DB12], qs[DB09]
    assert {"property": S.N_PUBLISHED_AT, "date": {"on_or_after": floor}} in q12["filter"]["and"]
    assert q12["sorts"] == [{"property": S.N_PUBLISHED_AT, "direction": "descending"}]
    assert {"property": S.C_PUBLISHED_AT, "date": {"on_or_after": floor}} in q09["filter"]["and"]
    assert q09["sorts"] == [{"property": S.C_PUBLISHED_AT, "direction": "descending"}]


def test_w7_exact_boundary_still_checked(mod):
    """날짜 필터는 넉넉히(날짜 단위) — 같은 날이라도 72시간을 넘긴 게시물은 코드가 뺀다."""
    fake, g = mod
    fake.add(DB12, **{S.N_KEY: "N1-E09", S.N_KIND: S.NK_EPISODE, S.N_STATE: S.NS_PUBLISHED,
                      S.N_FB_ID: "61_9", S.N_PUBLISHED_AT: "2026-11-27T11:59:00+09:00"})
    run()
    assert not any(c[1].startswith("61_9") for c in g.calls)


def test_w7_company_pattern_out_of_scope(mod):
    """판정은 금칙어만(회사명·실명 패턴은 범위 밖 — 리뷰 W7 결정)."""
    assert run_moderate.matches(f"{TERM} 이야기") is True
    assert run_moderate.matches("라마바주식회사 김철수 과장") is False

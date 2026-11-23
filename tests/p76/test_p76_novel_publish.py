"""사건 소설 게시 v2 — FB 예약 게시 (DN2026_0119, DSN-P76-NOVEL-003 §6 · §12-1 E-1 probe). 합성 데이터만 쓴다."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import requests
import test_p76_novel
import yaml
from p76_fake import DB12, FAKE_PAGE_ID, FakeResp, err, fail_deletes, png_bytes, schedule_handlers
from test_p76_novel import (
    CHAT,
    FAKE_CLAUDE_KEY,
    FAKE_NOVEL_BOT,
    add_canon_bible,
    add_episode_row,
    kst,
)

from p76 import graph, notion, novel_store, run_novel_publish, safety, settings
from p76.runlog import RunLog

S = settings
ROOT = Path(__file__).resolve().parents[2]
DUE = date(2026, 11, 29)  # 일요일 → T = 11-29 12:00 KST
T = kst(2026, 11, 29, 12, 0)
TEST_TERM = "시험금칙어"  # 원고·캡션에 나오지 않는 값
novel = test_p76_novel.novel  # 공용 fixture(가짜 노션·Graph·텔레그램·카드 렌더)


@pytest.fixture
def pubenv(request, env, monkeypatch):
    novel = request.getfixturevalue("novel")
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page, PAGE76_CLAUDE_KEY=FAKE_CLAUDE_KEY,
        PAGE76_BANNED_TERMS=TEST_TERM)  # DN2026_0124: live 는 금칙어가 있어야 등록한다
    monkeypatch.delenv("PAGE76_PUBLISH_PROBE", raising=False)
    add_canon_bible(novel.fake)
    novel.state = schedule_handlers(novel.graph, post_id="61_99")
    novel.notified = []
    monkeypatch.setattr(run_novel_publish, "notify_send", lambda m, urgent=False: novel.notified.append(m) or True)
    return novel


def pub(when):
    log = RunLog("novel_publish", "t")
    run_novel_publish.body(log, now=when)
    return log


def ep(n, m=1, due=DUE, state=S.NS_APPROVED, **kw):
    return add_episode_row(n.fake, 1, m, state, due=due, **kw)


def notice_row(n, due=DUE, body="이번 회차는 하루 쉬어 갑니다.\n※ 여러 사례를 재구성한 픽션입니다."):
    page = n.fake.add(DB12, **{S.N_KEY: "N1-NOTICE-1129", S.N_KIND: S.NK_NOTICE, S.N_SEASON: 1,
                               S.N_STATE: S.NS_APPROVED, S.N_DUE: due.isoformat()})
    n.fake.set_blocks(page["id"], novel_store.section_blocks([
        (novel_store.H_BODY, body, "code"), (novel_store.H_QUOTE, "잠시 쉬어 갑니다", "code"),
        (novel_store.H_SUBTITLE, "휴재 안내", "code")]))
    return page


def feeds(g):
    return [c for c in g.writes() if c[1].endswith("/feed")]


def v(n, page, name):
    return n.fake.val(page, name)


# ── 예약 등록 ─────────────────────────────────────────────
def test_register_episode_d1(pubenv):
    n = pubenv
    e1 = ep(n)
    log = pub(kst(2026, 11, 28, 12, 0))
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED and v(n, e1, S.N_FB_ID) == "61_99"
    assert v(n, e1, S.N_RESULT) == "scheduled:live"
    assert datetime.fromisoformat(v(n, e1, S.N_REGISTER_AT)) == T
    feed = feeds(n.graph)[0][2]
    assert int(feed["scheduled_publish_time"]) == int(T.timestamp())
    assert "제1화 합성 문장" in feed["message"] and "#사건소설" in feed["message"]
    assert any("11-29 12:00 게시 예약됨" in t and "중단" in t for t in n.tg.sent)
    assert not any("직접 확인" in t for t in n.tg.sent)
    assert "schedule_unverified" not in log.codes
    pub(kst(2026, 11, 28, 16, 0))
    assert len(feeds(n.graph)) == 1  # 재실행 멱등


@pytest.mark.parametrize("lead,registered,code", [
    (timedelta(hours=36), True, None),
    (timedelta(hours=36, seconds=1), False, None),
    (timedelta(minutes=15), True, None),
    (timedelta(minutes=14, seconds=59), False, "missed_slot"),
    (timedelta(minutes=-90), False, "missed_slot"),
])
def test_register_lead_boundaries(pubenv, lead, registered, code):
    n = pubenv
    e1 = ep(n)
    log = pub(T - lead)
    assert (v(n, e1, S.N_STATE) == S.NS_SCHEDULED) is registered
    assert bool(feeds(n.graph)) is registered
    if code:
        assert code in log.codes


def test_publish_time_variable(pubenv, env):
    n = pubenv
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_PROMPT_PAGE_ID=n.prompt_page, PAGE76_NOVEL_PUBLISH_HHMM="19:30")
    ep(n)
    pub(kst(2026, 11, 28, 12, 0))
    assert int(feeds(n.graph)[0][2]["scheduled_publish_time"]) == int(kst(2026, 11, 29, 19, 30).timestamp())


def test_friday_row_never_registered(pubenv):
    n = pubenv
    fri = date(2026, 12, 4)
    e1 = ep(n, due=fri)
    log = pub(kst(2026, 12, 3, 12, 0))
    assert "rest_day_row" in log.codes and n.graph.writes() == []
    assert v(n, e1, S.N_STATE) == S.NS_APPROVED


def test_one_per_day(pubenv):
    n = pubenv
    e1, e2 = ep(n, 1), ep(n, 2)
    log = pub(kst(2026, 11, 28, 12, 0))
    assert len(feeds(n.graph)) == 1 and "day_taken" in log.codes
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED and v(n, e2, S.N_STATE) == S.NS_APPROVED


def test_published_row_blocks_same_day(pubenv):
    n = pubenv
    ep(n, 1, state=S.NS_PUBLISHED)
    ep(n, 2)
    pub(kst(2026, 11, 28, 12, 0))
    assert n.graph.writes() == []


def test_two_days_in_window_each_registered(pubenv):
    n = pubenv
    ep(n, 1, due=date(2026, 11, 28))
    ep(n, 2, due=DUE)
    pub(kst(2026, 11, 28, 6, 0))
    assert len(feeds(n.graph)) == 2


def test_notice_row_scheduled_without_footer(pubenv):
    n = pubenv
    row = notice_row(n)
    pub(kst(2026, 11, 28, 12, 0))
    assert v(n, row, S.N_STATE) == S.NS_SCHEDULED
    msg = feeds(n.graph)[0][2]["message"]
    assert msg.startswith("이번 회차는 하루 쉬어 갑니다.") and "#사건소설" not in msg and "AI의 도움" not in msg


def test_notice_row_banned_term_holds(pubenv, env):
    n = pubenv
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_PROMPT_PAGE_ID=n.prompt_page, PAGE76_BANNED_TERMS="가나다상사")
    row = notice_row(n, body="가나다상사 이야기는 쉬어 갑니다.")
    log = pub(kst(2026, 11, 28, 12, 0))
    assert v(n, row, S.N_STATE) == S.NS_HELD and n.graph.writes() == []
    assert "R03:banned_term@body" in v(n, row, S.N_RESULT) and log.status == "failed"


def test_episode_recheck_holds(pubenv):
    n = pubenv
    e1 = ep(n, body="편집된 본문 D+3 서울에서 생긴 일\n" * 3)
    pub(kst(2026, 11, 28, 12, 0))
    assert v(n, e1, S.N_STATE) == S.NS_HELD and n.graph.writes() == []


def test_bible_stopped_blocks_episode(pubenv):
    n = pubenv
    bible = next(r for r in n.fake.rows(DB12) if v(n, r, S.N_KEY) == "N1-BIBLE")
    bible["properties"][S.N_STOP]["checkbox"] = True
    ep(n)
    pub(kst(2026, 11, 28, 12, 0))
    assert n.graph.writes() == []


def test_control_row_untouched(pubenv):
    n = pubenv
    ctrl = n.fake.add(DB12, **{S.N_KEY: "CONTROL", S.N_KIND: S.NK_CONTROL, S.N_RESULT: "{}"})
    ep(n)
    pub(kst(2026, 11, 28, 12, 0))
    assert not any(ctrl["id"] in path for _m, path, _b in n.fake.writes())


# ── 중단 → 예약 취소 ──────────────────────────────────────
def _registered(n):
    e1 = ep(n)
    pub(kst(2026, 11, 28, 12, 0))
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED
    return e1


def test_stop_cancels_schedule(pubenv):
    n = pubenv
    e1 = _registered(n)
    e1["properties"][S.N_STOP]["checkbox"] = True
    pub(kst(2026, 11, 29, 10, 3))
    assert [c[0] for c in n.graph.writes()].count("DELETE") == 1
    assert v(n, e1, S.N_STATE) == S.NS_STOPPED and v(n, e1, S.N_RESULT) == "cancelled"
    assert n.state["scheduled"] == []
    pub(kst(2026, 11, 29, 14, 13))
    assert [c[0] for c in n.graph.writes()].count("DELETE") == 1  # 다시 지우지 않는다


def test_stop_cancel_failure_keeps_row_and_informs(pubenv):
    """T 까지 6시간 넘게 남음 → 정보 알림만(다음 실행이 다시 취소)."""
    n = pubenv
    e1 = _registered(n)
    fail_deletes(n.state)
    e1["properties"][S.N_STOP]["checkbox"] = True
    log = pub(kst(2026, 11, 28, 16, 0))
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED and "cancel_failed:failed" in log.codes
    assert any("예약 취소 실패" in t for t in n.tg.sent) and n.notified == []
    before = len([t for t in n.tg.sent if "예약 취소 실패" in t])
    pub(kst(2026, 11, 28, 18, 23))
    assert len([t for t in n.tg.sent if "예약 취소 실패" in t]) == before == 1  # R6-02 P2: 행당 1회


def test_stop_cancel_failure_near_t_urgent_once(pubenv):
    """리뷰 W1 a: T−now ≤ 6시간인데 취소 실패 → 긴급 1회(사람 조치 예외), 다음 실행은 재시도만."""
    n = pubenv
    e1 = _registered(n)
    fail_deletes(n.state)
    e1["properties"][S.N_STOP]["checkbox"] = True
    log = pub(kst(2026, 11, 29, 6, 43))
    assert n.notified == ["N1-E01 예약 취소 실패 — 11-29 12:00 공개 예정, 페이지에서 직접 삭제 필요"]
    assert "cancel_failed:urgent" in log.codes and v(n, e1, S.N_STATE) == S.NS_SCHEDULED
    pub(kst(2026, 11, 29, 10, 43))
    assert [c[0] for c in n.graph.writes()].count("DELETE") == 2 and len(n.notified) == 1


def test_stopped_by_generator_still_cancelled(pubenv):
    """생성 파이프라인 collect_stops 가 FB 예약을 지우지 않고 '중단'으로 옮긴 경우 — 게시 파이프라인이 취소한다."""
    n = pubenv
    e1 = _registered(n)
    e1["properties"][S.N_STATE]["select"] = {"name": S.NS_STOPPED}
    e1["properties"][S.N_RESULT]["rich_text"] = [{"plain_text": "stopped:notion"}]
    pub(kst(2026, 11, 29, 10, 3))
    assert v(n, e1, S.N_RESULT) == "cancelled" and n.state["scheduled"] == []


def test_stop_after_t_unpublishes(pubenv):
    """리뷰 W1 b: T 경과 후 중단 체크된 게시물이 공개됨 → is_published=false → '중단'(unpublished)."""
    n = pubenv
    e1 = _registered(n)
    e1["properties"][S.N_STOP]["checkbox"] = True
    n.state["published"] = True
    log = pub(kst(2026, 11, 29, 14, 13))
    assert not any(c[0] == "DELETE" for c in n.graph.writes())
    assert [c for c in n.graph.writes() if c[1] == "61_99"] == [("POST", "61_99", {
        "is_published": "false", "access_token": n.graph.writes()[-1][2]["access_token"]})]
    assert v(n, e1, S.N_STATE) == S.NS_STOPPED and v(n, e1, S.N_RESULT) == "unpublished"
    assert "unpublished" in log.codes and n.notified == [] and any("비공개로 전환" in t for t in n.tg.sent)
    pub(kst(2026, 11, 29, 18, 23))
    assert len([c for c in n.graph.writes() if c[1] == "61_99"]) == 1  # 끝 상태 — 다시 시도하지 않음


def test_stop_after_t_unpublish_failure_urgent(pubenv):
    n = pubenv
    e1 = _registered(n)
    e1["properties"][S.N_STOP]["checkbox"] = True
    n.state["published"] = True
    n.state["unpublish"] = err(100)
    log = pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_PUBLISHED and v(n, e1, S.N_RESULT) == "unpublish_failed"
    assert datetime.fromisoformat(v(n, e1, S.N_PUBLISHED_AT)) == T  # R6-02 P3: 댓글 숨김 대상에 포함
    assert "unpublish_failed:failed" in log.codes
    assert n.notified == ["N1-E01 중단 체크된 게시물이 11-29 12:00에 공개됨 — 비공개 전환 실패, 페이지에서 직접 삭제 필요"]
    pub(kst(2026, 11, 29, 18, 23))
    assert len(n.notified) == 1 and len([c for c in n.graph.writes() if c[1] == "61_99"]) == 1


def test_generator_stopped_cancel_failed_until_t_then_unpublished(pubenv):
    """생성 측이 '중단'으로 옮긴 행 — T 전 취소가 계속 실패해 공개됨 → T 이후 비공개 전환."""
    n = pubenv
    e1 = _registered(n)
    e1["properties"][S.N_STATE]["select"] = {"name": S.NS_STOPPED}
    fail_deletes(n.state)
    pub(kst(2026, 11, 29, 6, 43))
    assert len(n.notified) == 1 and v(n, e1, S.N_RESULT) == "cancel_failed:urgent"
    n.state["published"] = True
    pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_STOPPED and v(n, e1, S.N_RESULT) == "unpublished"


# ── T 경과 후 확인 ────────────────────────────────────────
def test_confirm_published(pubenv):
    n = pubenv
    e1 = _registered(n)
    pub(kst(2026, 11, 29, 11, 0))
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED  # T 전에는 조회하지 않음
    n.state["published"] = True
    pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_PUBLISHED and v(n, e1, S.N_RESULT) == "published"
    assert v(n, e1, S.N_PUBLISHED_AT).startswith("2026-11-29T12:00:05")
    t3 = [t for t in n.tg.sent if "게시됨" in t]
    assert t3 and not any("직접 확인" in t for t in t3)


def test_unpublished_goes_check_then_requery(pubenv):
    n = pubenv
    e1 = _registered(n)
    pub(kst(2026, 11, 29, 12, 20))
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED  # T+30분 전
    log = pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_CHECK and "unconfirmed" in log.codes
    n.state["published"] = True
    pub(kst(2026, 11, 29, 18, 23))
    assert v(n, e1, S.N_STATE) == S.NS_PUBLISHED


def test_check_row_blocks_same_day(pubenv):
    n = pubenv
    _registered(n)
    pub(kst(2026, 11, 29, 14, 13))  # 확인필요
    ep(n, 2)
    pub(kst(2026, 11, 29, 6, 43))
    assert len(feeds(n.graph)) == 1


# ── 2단계 실패 분류 · 선점 ─────────────────────────────────
def test_photo_step_failed_releases_claim(pubenv):
    n = pubenv
    n.graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = err(100)
    e1 = ep(n)
    log = pub(kst(2026, 11, 28, 12, 0))
    assert v(n, e1, S.N_STATE) == S.NS_APPROVED and feeds(n.graph) == []
    assert run_novel_publish.POSTING not in v(n, e1, S.N_RESULT) and log.status == "failed"
    n.graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = FakeResp(200, {"id": "ph9"})
    pub(kst(2026, 11, 28, 16, 0))
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED  # 다음 실행 재시도


def test_photo_step_timeout_is_failed(pubenv, tmp_path):
    n = pubenv
    n.graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = requests.exceptions.ReadTimeout("t")
    img = tmp_path / "a.png"
    img.write_bytes(png_bytes(1080, 1350))
    res = graph.schedule_photo(str(img), "캡션", int(T.timestamp()), kst(2026, 11, 28, 12, 0).timestamp())
    assert res["status"] == "failed" and feeds(n.graph) == []


def test_feed_step_unknown_then_reconciled(pubenv):
    n = pubenv
    n.graph.handlers[("POST", f"{FAKE_PAGE_ID}/feed")] = requests.exceptions.ReadTimeout("t")
    e1 = ep(n)
    log = pub(kst(2026, 11, 28, 12, 0))
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED and v(n, e1, S.N_FB_ID) == ""
    assert v(n, e1, S.N_RESULT).startswith("unknown:schedule_feed") and log.status == "failed"
    n.state["scheduled"].append({"id": "61_99", "scheduled_publish_time": int(T.timestamp())})
    pub(kst(2026, 11, 28, 16, 0))
    assert v(n, e1, S.N_FB_ID) == "61_99" and len(feeds(n.graph)) == 1  # 재등록 없음


def test_feed_step_4xx_is_failed(pubenv):
    n = pubenv
    n.graph.handlers[("POST", f"{FAKE_PAGE_ID}/feed")] = err(100)
    e1 = ep(n)
    pub(kst(2026, 11, 28, 12, 0))
    assert v(n, e1, S.N_STATE) == S.NS_APPROVED and v(n, e1, S.N_RESULT).startswith("failed:schedule_feed")


def test_stale_claim_never_reregisters(pubenv):
    n = pubenv
    e1 = ep(n)
    e1["properties"][S.N_RESULT]["rich_text"] = [{"plain_text": "posting:1111"}]
    log = pub(kst(2026, 11, 28, 12, 0))
    assert n.graph.writes() == [] and v(n, e1, S.N_STATE) == S.NS_SCHEDULED
    assert "posting_unconfirmed" in log.codes


def test_schedule_unverified(pubenv):
    n = pubenv
    n.graph.handlers[("GET", f"{FAKE_PAGE_ID}/scheduled_posts")] = FakeResp(200, {"data": []})
    ep(n)
    log = pub(kst(2026, 11, 28, 12, 0))
    assert "schedule_unverified" in log.codes


def test_tripped_before_claim_no_marker(pubenv):
    n = pubenv
    e1 = ep(n)
    safety.trip("test")
    log = pub(kst(2026, 11, 28, 12, 0))
    assert n.graph.writes() == [] and "novel_publish:graph_write_blocked" in log.codes
    assert run_novel_publish.POSTING not in v(n, e1, S.N_RESULT)


def test_out_of_range_never_requests(pubenv, tmp_path):
    n = pubenv
    now = kst(2026, 11, 28, 12, 0).timestamp()
    for ts in (now + 599, now + 31 * 86400):
        assert graph.schedule_photo(str(tmp_path / "x"), "캡션", int(ts), now)["reason"] == "schedule:out_of_range"
    assert n.graph.writes() == []


# ── 모드 · 로그 ───────────────────────────────────────────
@pytest.mark.parametrize("mode", ["dry_run", "shadow"])
def test_non_live_writes_nothing(pubenv, env, mode):
    n = pubenv
    env(PAGE76_MODE=mode, PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_PROMPT_PAGE_ID=n.prompt_page, PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT,
        PAGE76_NOVEL_TG_CHAT_ID=CHAT)
    e1 = ep(n)
    e2 = ep(n, 2, due=date(2026, 11, 28), state=S.NS_SCHEDULED)
    e2["properties"][S.N_FB_ID]["rich_text"] = [{"plain_text": "61_98"}]
    e2["properties"][S.N_STOP]["checkbox"] = True
    stale = ep(n, 3, due=date(2026, 11, 30))
    stale["properties"][S.N_STOP]["checkbox"] = True
    pub(kst(2026, 11, 28, 6, 0))
    assert n.graph.writes() == [] and n.fake.writes() == [] and n.tg.sent == []
    assert v(n, e1, S.N_STATE) == S.NS_APPROVED


def test_switch_off(pubenv, env):
    n = pubenv
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="false")
    ep(n)
    pub(kst(2026, 11, 28, 12, 0))
    assert n.graph.writes() == [] and n.fake.writes() == []


def test_logs_clean(pubenv, caplog):
    caplog.set_level(logging.DEBUG)
    n = pubenv
    e1 = ep(n)
    pub(kst(2026, 11, 28, 12, 0))
    n.state["published"] = True
    pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_PUBLISHED
    for secret in ("61_99", "ph9", "제1화 합성 문장", DB12, e1["id"]):
        assert secret not in caplog.text


# ── probe (DN2026_0119 E-1) ────────────────────────────────
@pytest.fixture
def probe(pubenv, env, monkeypatch):
    env(PAGE76_MODE="probe")  # yml 이 넘기는 값 그대로 — settings 는 dry_run 으로 해석
    monkeypatch.setenv("PAGE76_PUBLISH_PROBE", "true")
    return pubenv


def test_probe_full_cycle(probe):
    n = probe
    assert S.MODE == "dry_run"
    log = pub(kst(2026, 11, 19, 15, 0))
    assert "probe:register_ok,list_ok,delete_ok,gone_ok" in log.codes and log.status == "degraded"
    feed = feeds(n.graph)[0][2]
    assert feed["message"] == run_novel_publish.PROBE_TEXT
    assert int(feed["scheduled_publish_time"]) == int(kst(2026, 11, 19, 15, 0).timestamp()) + 29 * 86400
    assert [c[0] for c in n.graph.writes()] == ["POST", "POST", "DELETE"]
    assert n.fake.writes() == [] and n.tg.sent == [] and n.notified == []
    assert not any(c[0] == "POST" and "databases" in c[1] for c in n.fake.calls)  # DB-12 조회 없음
    assert graph._probe is False


def test_probe_delete_failure_fails_and_alerts(probe):
    n = probe
    n.graph.handlers[("DELETE", "61_99")] = err(100)
    log = pub(kst(2026, 11, 19, 15, 0))
    assert log.status == "failed" and "probe:register_ok,list_ok,delete_failed,gone_still_listed" in log.codes
    assert n.notified == ["예약 점검 게시물 삭제 실패 — 29일 뒤 게시 예정"]


def test_probe_respects_trip(probe):
    n = probe
    safety.trip("test")
    with pytest.raises(safety.Tripped):
        pub(kst(2026, 11, 19, 15, 0))
    assert n.graph.writes() == [] and graph._probe is False


def test_probe_never_on_schedule(probe, env):
    n = probe
    env(PAGE76_MODE="probe", PAGE76_SCHEDULE_CRON="3 1 * * *")
    assert run_novel_publish.probe_requested() is False
    log = pub(kst(2026, 11, 19, 15, 0))
    assert n.graph.writes() == [] and not any(c.startswith("probe:") for c in log.codes)


def test_probe_only_via_dispatch_in_yml():
    doc = yaml.safe_load((ROOT / ".github/workflows/p76_novel_publish.yml").read_text(encoding="utf-8"))
    on = doc.get(True) or doc.get("on")
    assert "probe" in on["workflow_dispatch"]["inputs"]["mode"]["options"]
    expr = doc["jobs"]["run"]["steps"][-1]["env"]["PAGE76_PUBLISH_PROBE"]
    assert expr == ("${{ github.event_name == 'workflow_dispatch' && inputs.mode == 'probe' && 'true' || 'false' }}")
    for name in ("p76_hiatus_publish.yml", "p76_moderate.yml", "p76_novel.yml"):
        assert "PAGE76_PUBLISH_PROBE" not in (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")


def test_quality_warning_without_receipt_waits(pubenv):
    """품질 경고 회차는 알림 영수증(release_ok)이 있어야 등록한다 — 없으면 대기(보류 아님)."""
    n = pubenv
    e1 = ep(n)
    n.fake.set_blocks(e1["id"], list(n.fake.blocks[e1["id"]]) + novel_store.section_blocks(
        [(novel_store.H_META, json.dumps({"quality_warnings": ["R09:cliche"], "quoted_journals": []}), "json")]))
    # sections() 는 같은 제목이면 뒤 블록이 이긴다 — 뒤에 붙인 메타(품질 경고)가 적용된다
    log = pub(kst(2026, 11, 28, 12, 0))
    assert "quality:notice_pending" in log.codes and n.graph.writes() == []
    assert v(n, e1, S.N_STATE) == S.NS_APPROVED


# ── P9 순연 재예약 · 부록 만료 ─────────────────────────────
def _move_due(page, day):
    page["properties"][S.N_DUE]["date"] = {"start": day.isoformat()}


def test_reschedule_after_due_shift(pubenv):
    n = pubenv
    e1 = _registered(n)
    _move_due(e1, date(2026, 11, 30))  # 생성 측 순연: 11-29 → 11-30
    pub(kst(2026, 11, 29, 6, 43))
    assert [c[0] for c in n.graph.writes()] == ["POST", "POST", "DELETE", "POST", "POST"]
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED  # 같은 실행에서 새 날짜로 재등록
    assert datetime.fromisoformat(v(n, e1, S.N_REGISTER_AT)) == kst(2026, 11, 30, 12, 0)
    assert int(feeds(n.graph)[-1][2]["scheduled_publish_time"]) == int(kst(2026, 11, 30, 12, 0).timestamp())
    assert any("rescheduled" in str(b) for _m, _p, b in n.fake.writes())


def test_reschedule_frees_old_day_for_new_row(pubenv):
    n = pubenv
    e1 = _registered(n)
    _move_due(e1, date(2026, 11, 30))
    notice = notice_row(n, due=DUE)  # 순연 슬롯(11-29)에 휴재 안내
    pub(kst(2026, 11, 29, 6, 43))
    assert v(n, notice, S.N_STATE) == S.NS_SCHEDULED and v(n, e1, S.N_STATE) == S.NS_SCHEDULED


def test_reschedule_delete_failure_keeps_row(pubenv):
    n = pubenv
    e1 = _registered(n)
    fail_deletes(n.state)
    _move_due(e1, date(2026, 11, 30))
    log = pub(kst(2026, 11, 28, 16, 0))
    assert "reschedule_cancel_failed" in log.codes and v(n, e1, S.N_STATE) == S.NS_SCHEDULED
    assert v(n, e1, S.N_FB_ID) == "61_99" and len(feeds(n.graph)) == 1
    assert any("취소에 실패" in t for t in n.tg.sent)


def test_reschedule_ignored_after_t(pubenv):
    n = pubenv
    e1 = _registered(n)
    _move_due(e1, date(2026, 11, 30))
    pub(kst(2026, 11, 29, 12, 10))
    assert not any(c[0] == "DELETE" for c in n.graph.writes())
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED and v(n, e1, S.N_FB_ID) == "61_99"


def test_notice_expired_stopped(pubenv):
    n = pubenv
    row = notice_row(n, due=date(2026, 11, 28))
    log = pub(kst(2026, 11, 29, 6, 43))
    assert v(n, row, S.N_STATE) == S.NS_STOPPED and v(n, row, S.N_RESULT) == "notice_expired"
    assert "notice_expired" in log.codes and n.graph.writes() == []



# ── 법률 블록 치환 (v1 TN-17 이관, DN2026_0119 R10) ────────────
def test_unconfirmed_law_line_dropped_in_scheduled_caption(pubenv):
    n = pubenv
    ep(n, body=test_p76_novel._body("\n{{LAW_1350}}"))
    pub(kst(2026, 11, 28, 12, 0))
    msg = feeds(n.graph)[0][2]["message"]
    assert "상담 안내 합성 문안" not in msg and "{{" not in msg


def test_confirmed_law_substituted_in_scheduled_caption(pubenv):
    n = pubenv
    ep(n, body=test_p76_novel._body("\n{{LAW_76_2}}"))
    pub(kst(2026, 11, 28, 12, 0))
    msg = feeds(n.graph)[0][2]["message"]
    assert "법률 블록 76의2 합성 문안" in msg and "{{" not in msg


# ── 리뷰 C1 · 일시 정지 · W4 (R5-05) ───────────────────────
def test_c1_old_t_day_not_reused_when_reschedule_cancel_fails(pubenv):
    """리뷰 재현: E1 11-29 예약 → 게시예정일 11-30 → DELETE 실패 → 같은 실행에서 11-29 부록 등록 시도 → 0건."""
    n = pubenv
    e1 = _registered(n)
    _move_due(e1, date(2026, 11, 30))
    fail_deletes(n.state)
    notice = notice_row(n, due=DUE)
    log = pub(kst(2026, 11, 29, 6, 43))
    assert len(feeds(n.graph)) == 1  # 처음 등록 1건뿐
    assert v(n, notice, S.N_STATE) == S.NS_APPROVED and v(n, e1, S.N_STATE) == S.NS_SCHEDULED
    assert "reschedule_cancel_failed" in log.codes and "day_blocked" in log.codes
    assert len(n.notified) == 1  # T 까지 6시간 이내 → 긴급 1회


def test_c1_register_at_day_holds_without_fb_id(pubenv):
    """FB게시ID 없는(대조 전) 발행예고 행이 게시예정일만 옮겨져도 옛 T 날짜는 계속 차지된다."""
    n = pubenv
    e1 = ep(n, state=S.NS_SCHEDULED)
    e1["properties"][S.N_REGISTER_AT]["date"] = {"start": T.isoformat()}
    _move_due(e1, date(2026, 11, 30))
    notice = notice_row(n, due=DUE)
    log = pub(kst(2026, 11, 29, 6, 43))
    assert feeds(n.graph) == [] and v(n, notice, S.N_STATE) == S.NS_APPROVED
    assert "reschedule:no_fb_id" in log.codes and "day_taken" in log.codes


def test_c1_many_rows_one_per_day(pubenv):
    """다건: 이틀 창에 회차 3건 + 부록 1건 → 날짜당 1건만, 순연 실패 날짜는 비운다."""
    n = pubenv
    e1 = _registered(n)                       # 11-29 예약
    _move_due(e1, date(2026, 11, 30))
    fail_deletes(n.state)
    ep(n, 2, due=DUE)                         # 11-29 → 막힘
    e3 = ep(n, 3, due=date(2026, 11, 30))     # 11-30 → e1 의 게시예정일이 차지
    notice_row(n, due=DUE)                    # 11-29 → 막힘
    pub(kst(2026, 11, 29, 6, 43))
    assert len(feeds(n.graph)) == 1 and v(n, e3, S.N_STATE) == S.NS_APPROVED
    n.state["delete_fail"] = None             # 다음 실행: 취소 성공 → e1 은 11-30 으로 재등록
    pub(kst(2026, 11, 29, 10, 43))
    days = sorted(int(c[2]["scheduled_publish_time"]) for c in feeds(n.graph)[1:])
    assert days == [int(T.timestamp()), int(kst(2026, 11, 30, 12, 0).timestamp())]  # 취소 성공 → 11-29 1건, 11-30 1건
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED and v(n, e3, S.N_STATE) == S.NS_APPROVED
    held = [r for r in n.fake.rows(DB12) if v(n, r, S.N_STATE) == S.NS_SCHEDULED]
    assert len(held) == 2 and len({v(n, r, S.N_REGISTER_AT)[:10] for r in held}) == 2


def test_paused_due_cleared_unschedules(pubenv):
    """생성 측 계약: 연재 일시 정지 → 게시예정일 비움 → T 이전이면 예약 취소 → '승인'(paused_unscheduled)."""
    n = pubenv
    e1 = _registered(n)
    e1["properties"][S.N_DUE]["date"] = None
    log = pub(kst(2026, 11, 29, 6, 43))
    assert [c[0] for c in n.graph.writes()] == ["POST", "POST", "DELETE"]
    assert v(n, e1, S.N_STATE) == S.NS_APPROVED and v(n, e1, S.N_RESULT) == "paused_unscheduled"
    assert v(n, e1, S.N_FB_ID) == "" and v(n, e1, S.N_REGISTER_AT) is None
    assert "day_taken" not in log.codes
    pub(kst(2026, 11, 29, 10, 43))
    assert len(feeds(n.graph)) == 1  # 게시예정일 없는 행은 등록 대상이 아니다


def test_due_empty_approved_not_registered(pubenv):
    n = pubenv
    e1 = ep(n)
    e1["properties"][S.N_DUE]["date"] = None
    pub(kst(2026, 11, 28, 12, 0))
    assert n.graph.writes() == []


def test_w4_claimed_checked_before_missed_slot(pubenv):
    """리뷰 W4 a: 선점 표시가 남은 행은 하한(15분)을 넘겼어도 missed_slot 이 아니라 대조로 넘어간다."""
    n = pubenv
    e1 = ep(n)
    e1["properties"][S.N_RESULT]["rich_text"] = [{"plain_text": "posting:1111"}]
    log = pub(T - timedelta(minutes=5))
    assert n.graph.writes() == [] and v(n, e1, S.N_STATE) == S.NS_SCHEDULED
    assert "posting_unconfirmed" in log.codes and "missed_slot" not in log.codes


def _unknown_registered(n):
    n.graph.handlers[("POST", f"{FAKE_PAGE_ID}/feed")] = requests.exceptions.ReadTimeout("t")
    e1 = ep(n)
    pub(kst(2026, 11, 28, 12, 0))
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED and v(n, e1, S.N_FB_ID) == ""
    msg = next(c[2]["message"] for c in n.graph.calls if c[1].endswith("/feed"))
    return e1, msg


def _post(pid, msg, created="2026-11-29T03:00:05+0000"):
    return {"id": pid, "created_time": created, "message": msg}


def test_w4_unknown_matched_by_caption_hash(pubenv, caplog):
    caplog.set_level(logging.DEBUG)
    n = pubenv
    e1, msg = _unknown_registered(n)
    n.state["page_posts"] = [_post("61_77", msg), _post("61_78", "다른 합성 게시물"),
                             _post("61_79", msg, created="2026-11-28T03:00:05+0000")]  # 창 밖
    log = pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_PUBLISHED and v(n, e1, S.N_FB_ID) == "61_77"
    assert v(n, e1, S.N_RESULT) == "published:matched" and "matched" in log.codes
    assert v(n, e1, S.N_PUBLISHED_AT).startswith("2026-11-29T12:00:05")
    posts_call = next(c for c in n.graph.calls if c[1].endswith("/posts"))
    assert "message" in posts_call[2]["fields"]
    assert int(posts_call[2]["since"]) == int((T - timedelta(hours=2)).timestamp())
    assert int(posts_call[2]["until"]) == int((T + timedelta(hours=2)).timestamp())
    for secret in ("제1화 합성 문장", "61_77", "다른 합성 게시물"):
        assert secret not in caplog.text


def test_w4_unknown_not_found_stays_check_no_reregister(pubenv):
    """R6-02 N1 결정: 대조 0건이어도 승인으로 되돌리지 않는다(중복 게시 방지) — 확인필요 유지 + T5 1회."""
    n = pubenv
    e1, _ = _unknown_registered(n)
    n.state["page_posts"] = [_post("61_78", "다른 합성 게시물")]
    log = pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_CHECK and v(n, e1, S.N_RESULT).split("|")[0] == "unconfirmed:not_found"
    assert "unconfirmed_not_found" in log.codes
    pub(kst(2026, 11, 29, 18, 23))
    assert len([t for t in n.tg.sent if "공개 게시물에서 찾지 못했습니다" in t]) == 1
    assert len(feeds(n.graph)) == 1  # 재등록 없음


def test_w4_unknown_ambiguous_stays_check_info_once(pubenv):
    n = pubenv
    e1, msg = _unknown_registered(n)
    n.state["page_posts"] = [_post("61_77", msg), _post("61_78", msg)]
    log = pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_CHECK and v(n, e1, S.N_RESULT).split("|")[0] == "unconfirmed:ambiguous"
    assert "|h:" in v(n, e1, S.N_RESULT)  # 등록 시점 해시 보존
    assert "match:ambiguous" in log.codes
    sent = len([t for t in n.tg.sent if "같은 캡션 게시물이 2건" in t])
    pub(kst(2026, 11, 29, 18, 23))
    assert sent == 1 and len([t for t in n.tg.sent if "같은 캡션 게시물이 2건" in t]) == 1
    assert n.notified == []


def test_w4_match_read_failure_keeps_check(pubenv):
    n = pubenv
    e1, _ = _unknown_registered(n)
    n.graph.handlers[("GET", f"{FAKE_PAGE_ID}/posts")] = err(100)
    log = pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_CHECK and "match:read_failed" in log.codes



def test_match_uses_registration_hash_not_rebuilt_caption(pubenv, monkeypatch):
    """R6 재리뷰 대응: 대조는 등록 시점 캡션 해시(결과코드 h:)를 쓴다 — 재구성 캡션이 달라져도 1건이면 게시완료."""
    n = pubenv
    e1, msg = _unknown_registered(n)
    assert "|h:" in v(n, e1, S.N_RESULT)
    monkeypatch.setattr(run_novel_publish, "_caption", lambda ctx, row: "재구성 캡션이 달라진 경우")
    n.state["page_posts"] = [_post("61_77", msg)]
    pub(kst(2026, 11, 29, 14, 13))
    assert v(n, e1, S.N_STATE) == S.NS_PUBLISHED and v(n, e1, S.N_FB_ID) == "61_77"


@pytest.mark.parametrize("lag_h", [0, 6, 20])
def test_e2e_cron_lag_does_not_move_publish_time(pubenv, lag_h):
    """설계 AC3: 예약 실행이 0·6·20시간 늦어도 D-1 등록 → T 에 게시 → 게시완료, 게시 시각 오차 0, 등록 1건."""
    n = pubenv
    e1 = ep(n)
    slots = [kst(2026, 11, 28, h, mm) for h, mm in ((2, 33), (6, 43), (10, 3), (14, 13), (18, 23), (22, 43))]
    for at in slots:
        pub(at + timedelta(hours=lag_h))
        if v(n, e1, S.N_STATE) == S.NS_SCHEDULED:
            break
    assert v(n, e1, S.N_STATE) == S.NS_SCHEDULED
    assert len(feeds(n.graph)) == 1
    assert int(feeds(n.graph)[0][2]["scheduled_publish_time"]) == int(T.timestamp())
    n.state["published"] = True
    pub(T + timedelta(hours=1))
    assert v(n, e1, S.N_STATE) == S.NS_PUBLISHED
    assert len(feeds(n.graph)) == 1  # 재등록 없음



def test_claim_records_register_at_and_holds_day(pubenv):
    """R6-02 N2: 선점과 함께 T 를 기록하고, 선점 중 행의 T 날짜는 다른 행이 쓰지 못한다."""
    n = pubenv
    e1 = ep(n)
    novel_store.update(e1, {S.N_RESULT: notion.v_rich("posting:old|h:abcd"), S.N_REGISTER_AT: notion.v_date(T)})
    novel_store.update(e1, {S.N_DUE: notion.v_date(None)})  # 생성 측이 날짜를 비운 경우에도
    e2 = ep(n, m=2)
    pub(kst(2026, 11, 28, 12, 0))
    assert not any(c for c in feeds(n.graph))  # e2 는 같은 날 등록되지 않음
    assert v(n, e2, S.N_STATE) == S.NS_APPROVED



def test_n1_not_found_closes_after_48h(pubenv):
    """R6-03: 대조 0건 확인필요는 48시간 뒤 대조를 끝낸다(조회·degrade 반복 방지), 안내 문구에 조치 포함."""
    n = pubenv
    e1, _ = _unknown_registered(n)
    n.state["page_posts"] = []
    pub(kst(2026, 11, 29, 14, 13))
    assert any("상태를 '승인'으로" in t for t in n.tg.sent)
    log = pub(kst(2026, 12, 1, 14, 13))
    assert v(n, e1, S.N_RESULT).split("|")[0] == "unconfirmed:closed"
    calls = len([c for c in n.graph.calls if c[1].endswith("/posts")]) if hasattr(n.graph, "calls") else None
    pub(kst(2026, 12, 1, 18, 23))
    if calls is not None:
        assert len([c for c in n.graph.calls if c[1].endswith("/posts")]) == calls
    assert "unconfirmed_not_found" not in log.codes


def test_claimed_row_with_stop_checked_is_reconciled_first(pubenv):
    """R6-03: 선점 중 행은 중단 체크여도 먼저 대조 — 같은 시각 예약이 1건이면 발행예고로 넘어가 취소 경로를 탄다."""
    n = pubenv
    e1 = ep(n)
    novel_store.update(e1, {S.N_RESULT: notion.v_rich("posting:old|h:abcd"), S.N_REGISTER_AT: notion.v_date(T)})
    novel_store.update(e1, {S.N_STOP: {"checkbox": True}})
    n.state["scheduled"].append({"id": "61_55", "scheduled_publish_time": int(T.timestamp())})
    pub(kst(2026, 11, 28, 12, 0))
    assert v(n, e1, S.N_STATE) in (S.NS_SCHEDULED, S.NS_STOPPED)
    assert v(n, e1, S.N_FB_ID) == "61_55"


# ── DN2026_0124 D4: live 에서 금칙어가 비면 새 예약을 등록하지 않는다(fail-closed) ──
def test_d4_live_without_terms_registers_nothing(pubenv, env):
    n = pubenv
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=n.prompt_page, PAGE76_CLAUDE_KEY=FAKE_CLAUDE_KEY, PAGE76_BANNED_TERMS="")
    row = ep(n)
    log = pub(kst(2026, 11, 28, 12, 0))
    assert "no_terms" in log.codes and feeds(n.graph) == []
    assert v(n, row, S.N_STATE) == S.NS_APPROVED
    assert any("PAGE76_BANNED_TERMS" in t for t in n.tg.sent)

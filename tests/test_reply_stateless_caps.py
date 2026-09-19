"""답글 엔진 v1.1.0 — 무상태 일일 캡, 스레드 캡, 대댓글 맥락, 스캔 범위."""

from __future__ import annotations

import datetime as dt
import os
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from src import config, reply_engine, run_reply
from src.reply_engine import Comment

KST = ZoneInfo("Asia/Seoul")
NOW = dt.datetime.now(dt.UTC)


def _ts(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


# 자정 직후 실행에도 '오늘'로 판정되도록 KST 자정 이후로 고정한다.
_MIDNIGHT = dt.datetime.combine(NOW.astimezone(KST).date(), dt.time(0, 1), tzinfo=KST)
TODAY_TS = _ts(max(NOW - dt.timedelta(minutes=1), _MIDNIGHT))
OLD_TS = _ts(NOW - dt.timedelta(days=3))


def _c(cid, text="한국어 댓글입니다", *, user="u1", mine=False, parent="post1", ts=TODAY_TS):
    return Comment(id=cid, text=text, username=user, timestamp=ts,
                   replied_to_id=parent, owned_by_me=mine, hide_status="NOT_HUSHED")


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


class TestLedger:
    def test_counts_today_replies_excluding_self_link_reply(self):
        conv = {"post1": [
            _c("c1", user="alice"),
            _c("m1", mine=True, parent="c1"),            # 오늘 alice 에게 답글
            _c("m2", mine=True, parent="post1"),         # 셀프 리플라이(링크) — 제외
            _c("c2", user="bob"),
            _c("m3", mine=True, parent="c2", ts=OLD_TS),  # 과거 답글 — 오늘 집계 제외
        ]}
        ledger = run_reply.build_ledger(conv, {"post1"}, NOW.astimezone(KST).date())
        assert ledger.used_today == 1
        assert ledger.author_today["alice"] == 1
        assert ledger.author_today["bob"] == 0
        assert ledger.thread_author[("post1", "bob")] == 1   # 스레드 누적은 기간 무관

    def test_empty(self):
        ledger = run_reply.build_ledger({}, set(), NOW.date())
        assert ledger.used_today == 0


class TestThreadCap:
    def test_decide_blocks_at_thread_cap(self):
        d = reply_engine.decide(_c("c9"), already_replied=False, author_used=0,
                                thread_author_count=config.REPLY_THREAD_AUTHOR_CAP)
        assert d.strategy is reply_engine.ReplyStrategy.SKIP
        assert "스레드" in d.reason

    def test_decide_default_backward_compatible(self):
        d = reply_engine.decide(_c("c9"), already_replied=False, author_used=0)
        assert d.strategy is reply_engine.ReplyStrategy.NORMAL


class TestNestedPrompt:
    def test_parent_included(self):
        prompt = reply_engine.build_reply_prompt("원글", "대댓글", "내 직전 답글")
        assert "내가 앞서 단 답글" in prompt and "내 직전 답글" in prompt

    def test_no_parent_unchanged_shape(self):
        prompt = reply_engine.build_reply_prompt("원글", "댓글")
        assert "내가 앞서 단 답글" not in prompt and "# 달린 댓글" in prompt


# ---------------------------------------------------------------------------
# sweep 통합 (API 모의)
# ---------------------------------------------------------------------------

BASE_ENV = {
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "CLAUDE_AI_KEY": "sk-ant-test",
    "DRY_RUN": "false",
}


@pytest.fixture(autouse=True)
def _env():
    saved = dict(os.environ)
    os.environ.update(BASE_ENV)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _raw(c: Comment) -> dict:
    return {"id": c.id, "text": c.text, "username": c.username, "timestamp": c.timestamp,
            "replied_to": {"id": c.replied_to_id}, "is_reply": True,
            "is_reply_owned_by_me": c.owned_by_me, "hide_status": c.hide_status}


class FakeApi:
    def __init__(self, posts: list[dict], convs: dict[str, list[Comment]]):
        self.posts = posts
        self.convs = convs
        self.created: list[dict] = []
        self.list_params: list[dict] = []

    def __call__(self, method, url, *, params):
        if url.endswith("/threads_publishing_limit"):
            return {"data": [{"reply_quota_usage": 0, "reply_config": {"quota_total": 1000}}]}
        if url.endswith("/conversation"):
            post_id = url.rsplit("/", 2)[-2]
            return {"data": [_raw(c) for c in self.convs.get(post_id, [])]}
        if url.endswith("/threads") and method == "GET":
            self.list_params.append(dict(params))
            return {"data": self.posts}
        if url.endswith("/threads") and method == "POST":
            self.created.append(dict(params))
            return {"id": f"c{len(self.created)}"}
        if url.endswith("/threads_publish"):
            return {"id": "r"}
        if "status" in params.get("fields", ""):
            return {"status": "FINISHED"}
        return {}


def _sweep(fake: FakeApi, *, per_run_cap: int = 20, parent_capture: list | None = None) -> int:
    from src.env import load_settings
    from src.threads_client import ThreadsClient

    def gen(api_key, post_text, comment_text, parent_reply_text=""):
        if parent_capture is not None:
            parent_capture.append(parent_reply_text)
        return "답글입니다"

    with (
        mock.patch("src.threads_client._request", side_effect=fake),
        mock.patch("src.threads_client.time.sleep"),
        mock.patch("src.antibot.time.sleep"),
        mock.patch("src.reply_engine._generate_reply", side_effect=gen),
    ):
        return run_reply.sweep(ThreadsClient("1", "tok"), load_settings(),
                               per_run_cap=per_run_cap, dry_run=False, now=NOW)


def _post(pid: str, hours_ago: float) -> dict:
    return {"id": pid, "text": "원글", "timestamp": _ts(NOW - dt.timedelta(hours=hours_ago))}


class TestSweep:
    def test_daily_cap_counts_previous_runs(self):
        """오늘 이미 20건을 달았으면 이번 실행은 0건."""
        mine = [_c(f"m{i}", mine=True, parent=f"x{i}") for i in range(config.REPLY_DAILY_CAP)]
        others = [_c(f"x{i}", user=f"user{i}") for i in range(config.REPLY_DAILY_CAP)]
        fresh = [_c("new1", user="newbie")]
        fake = FakeApi([_post("post1", 1)], {"post1": others + mine + fresh})
        assert _sweep(fake) == 0
        assert fake.created == []

    def test_daily_cap_remaining(self):
        used = config.REPLY_DAILY_CAP - 2
        mine = [_c(f"m{i}", mine=True, parent=f"x{i}") for i in range(used)]
        others = [_c(f"x{i}", user=f"user{i}") for i in range(used)]
        fresh = [_c(f"n{i}", user=f"new{i}") for i in range(5)]
        fake = FakeApi([_post("post1", 1)], {"post1": others + mine + fresh})
        assert _sweep(fake) == 2

    def test_per_run_cap(self):
        fresh = [_c(f"n{i}", user=f"new{i}") for i in range(10)]
        fake = FakeApi([_post("post1", 1)], {"post1": fresh})
        assert _sweep(fake, per_run_cap=4) == 4

    def test_author_cap_counts_previous_runs(self):
        """alice 에게 오늘 이미 2건 → 새 댓글에도 답하지 않는다."""
        conv = [
            _c("a1", user="alice"), _c("m1", mine=True, parent="a1"),
            _c("a2", user="alice"), _c("m2", mine=True, parent="a2"),
            _c("a3", user="alice"),
        ]
        fake = FakeApi([_post("post1", 1)], {"post1": conv})
        assert _sweep(fake) == 0

    def test_nested_reply_gets_parent_context(self):
        conv = [
            _c("a1", user="alice"),
            _c("m1", "제가 먼저 단 답글", mine=True, parent="a1"),
            _c("a2", "그럼 다음엔 어떻게 하세요?", user="alice", parent="m1"),
        ]
        captured: list[str] = []
        fake = FakeApi([_post("post1", 1)], {"post1": conv})
        assert _sweep(fake, parent_capture=captured) == 1
        assert fake.created[0]["reply_to_id"] == "a2"
        assert captured == ["제가 먼저 단 답글"]

    def test_old_posts_outside_scan_window_skipped(self):
        fake = FakeApi([_post("old", config.REPLY_SCAN_HOURS + 5)],
                       {"old": [_c("n1", parent="old")]})
        assert _sweep(fake) == 0

    def test_list_uses_since_and_limit(self):
        fake = FakeApi([_post("post1", 1)], {"post1": []})
        _sweep(fake)
        params = fake.list_params[0]
        assert "since" in params
        assert params["limit"] <= 25

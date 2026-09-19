"""CHAT 도입에 따른 기존 모듈 보정 검증 — 페이지네이션, insights, story, watchdog."""

from __future__ import annotations

import datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo

from src import config, insights, watchdog

KST = ZoneInfo("Asia/Seoul")
DAY = dt.date(2026, 9, 21)


def _kst(hh: int, mm: int) -> dt.datetime:
    return dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=KST)


def _post(when: dt.datetime, pid: str = "p") -> dict:
    return {"id": pid, "text": "글",
            "timestamp": when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")}


class TestPagination:
    def test_follows_after_cursor_until_limit(self):
        from src.threads_client import ThreadsClient

        pages = [
            {"data": [{"id": f"a{i}"} for i in range(25)],
             "paging": {"cursors": {"after": "CUR1"}}},
            {"data": [{"id": f"b{i}"} for i in range(25)],
             "paging": {"cursors": {"after": "CUR2"}}},
        ]
        calls: list[dict] = []

        def fake(method, url, *, params):
            calls.append(dict(params))
            return pages[len(calls) - 1]

        with mock.patch("src.threads_client._request", side_effect=fake):
            posts = ThreadsClient("1", "t").get_my_posts(40, since=DAY)
        assert len(posts) == 40
        assert calls[0]["since"] == "2026-09-21"
        assert calls[1]["after"] == "CUR1"
        assert len(calls) == 2

    def test_stops_without_cursor(self):
        from src.threads_client import ThreadsClient

        with mock.patch("src.threads_client._request",
                        return_value={"data": [{"id": "x"}]}) as req:
            posts = ThreadsClient("1", "t").get_my_posts(70)
        assert posts == [{"id": "x"}]
        assert req.call_count == 1

    def test_small_limit_single_page_no_since(self):
        from src.threads_client import ThreadsClient

        with mock.patch("src.threads_client._request",
                        return_value={"data": []}) as req:
            ThreadsClient("1", "t").get_my_posts(5)
        params = req.call_args.kwargs["params"]
        assert params["limit"] == 5
        assert "since" not in params


class TestInsightsChat:
    def test_chat_window_classified(self):
        assert insights.restore_pillar(_kst(10, 15)) == insights.CHAT

    def test_regular_slot_unchanged(self):
        got = insights.restore_pillar(_kst(8, 25))
        assert got not in (insights.CHAT, insights.UNKNOWN)

    def test_aggregate_has_chat_row(self):
        stat = insights.PostStat("p", _kst(10, 0), insights.CHAT, replies=2)
        rows = insights.aggregate([stat])
        chat = [r for r in rows if r.pillar == insights.CHAT][0]
        assert chat.posts == 1 and chat.replies == 2

    def test_weighting_excludes_chat(self):
        from src import run_weighting

        stats = [insights.PostStat("p", _kst(10, 0), insights.CHAT, replies=9),
                 insights.PostStat("q", _kst(8, 25), "STORY", replies=1)]
        scores = run_weighting._scores(stats, clicks_total=10)
        assert [s.pillar for s in scores] == ["STORY"]
        assert scores[0].clicks == 10


class TestStoryGateExcludesChat:
    NOW = _kst(13, 40).astimezone(dt.UTC)

    def _gate(self, posts):
        from src import run_story

        with mock.patch.object(run_story, "_regular_pillar_today", return_value="OTHER"):
            return run_story._gate(posts, self.NOW, DAY, 200)

    def test_chat_posts_do_not_block_event(self):
        posts = [_post(_kst(9, 5) + dt.timedelta(minutes=20 * i), f"c{i}") for i in range(8)]
        posts.append(_post(_kst(8, 25), "reg"))                             # 정기 1건
        assert self._gate(posts) is None

    def test_regular_post_still_counts_for_gap(self):
        posts = [_post(_kst(12, 50), "regB")]
        got = self._gate(posts)
        assert got and "최소 간격" in got


class TestWatchdogChat:
    def test_disabled_ok(self):
        f = watchdog.check_chat_activity(0, _kst(21, 37), enabled=False)
        assert f.severity == watchdog.Severity.OK

    def test_before_window_end_ok(self):
        f = watchdog.check_chat_activity(0, _kst(9, 53), enabled=True)
        assert f.severity == watchdog.Severity.OK

    def test_zero_after_window_warns(self):
        f = watchdog.check_chat_activity(0, _kst(21, 37), enabled=True)
        assert f.severity == watchdog.Severity.WARN

    def test_some_after_window_ok(self):
        f = watchdog.check_chat_activity(3, _kst(21, 37), enabled=True)
        assert f.severity == watchdog.Severity.OK

    def test_freshness_ignores_chat(self):
        """정기 발행이 멈추면 CHAT 이 있어도 경보가 나야 한다."""
        from src import run_watchdog

        now = _kst(21, 37).astimezone(dt.UTC)
        stale_regular = _post(now - dt.timedelta(hours=60), "reg")
        chats = [_post(_kst(10, 0), "c1")]
        regular = [p for p in [*chats, stale_regular] if not run_watchdog._is_chat_post(p)]
        f = watchdog.check_publish_freshness(regular, now)
        assert f.severity != watchdog.Severity.OK

    def test_scan_covers_a_day(self):
        assert watchdog.RECENT_POSTS_TO_SCAN >= len(config.CHAT_TRIGGERS) + 3

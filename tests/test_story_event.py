"""이벤트 기반 STORY 발행 테스트.

핵심 관심사
  1. 무상태 신규 판정이 시간창으로 동작하는가
  2. 안전장치가 중복·과잉 발행을 막는가
  3. 비활성·근거없음일 때 아무것도 하지 않는가
"""

from __future__ import annotations

import datetime as dt
import os
from unittest import mock

import pytest

from src import config, notion_source

NOW = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.UTC)

BASE_ENV = {
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "NOTION_TOKEN": "ntn_test",
    "YOUTUBE_URL": "https://www.youtube.com/@handle",
    "X_URL": "https://x.com/handle",
    "GITHUB_REPOSITORY": "owner/repo",
    "DRY_RUN": "false",
}


@pytest.fixture(autouse=True)
def _env():
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("THREADS_", "NOTION_", "EVENT_", "SLOT", "AI_")):
            os.environ.pop(key, None)
    os.environ.update(BASE_ENV)
    with (
        mock.patch.object(config, "NOTION_DB_ID", "db123"),
        mock.patch.object(config, "EVENT_STORY_ENABLED", True),
        mock.patch.object(config, "YOUTUBE_URL", BASE_ENV["YOUTUBE_URL"]),
        mock.patch.object(config, "X_URL", BASE_ENV["X_URL"]),
    ):
        yield
    os.environ.clear()
    os.environ.update(saved)


def _resp(status: int, body: dict):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body
    r.text = str(body)
    return r


def _post(hours_ago: float) -> dict:
    """고정 기준 시각(NOW) 대비. 게이트 단위 테스트용."""
    when = NOW - dt.timedelta(hours=hours_ago)
    return {"id": "p1", "text": "글",
            "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S+0000")}


def _post_now(hours_ago: float) -> dict:
    """실제 현재 시각 대비. run() 통합 테스트용."""
    when = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)
    return {"id": "p1", "text": "글",
            "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S+0000")}


# ---------------------------------------------------------------------------
# 신규 회차 조회
# ---------------------------------------------------------------------------


class TestFetchNewEpisodes:
    def test_no_config_returns_empty(self):
        assert notion_source.fetch_new_episodes("", "db", NOW, 5) == []
        assert notion_source.fetch_new_episodes("t", "", NOW, 5) == []

    def test_time_window_in_filter(self):
        with mock.patch.object(
            notion_source.requests, "post", return_value=_resp(200, {"results": []})
        ) as post:
            notion_source.fetch_new_episodes("t", "db", NOW, 5)
        payload = post.call_args.kwargs["json"]
        assert payload["filter"]["timestamp"] == "created_time"
        assert "on_or_after" in payload["filter"]["created_time"]

    def test_status_filter_applied(self):
        with mock.patch.object(
            notion_source.requests, "post", return_value=_resp(200, {"results": []})
        ) as post:
            notion_source.fetch_new_episodes(
                "t", "db", NOW, 5,
                status_property="발행 상태", status_value="완료",
            )
        conditions = post.call_args.kwargs["json"]["filter"]["and"]
        assert any(c.get("property") == "발행 상태" for c in conditions)

    def test_status_filter_failure_retries_without_it(self):
        """속성명이 스키마와 다르면 필터 없이 재시도한다."""
        calls: list[dict] = []

        def responder(*args, **kwargs):
            calls.append(kwargs["json"])
            if len(calls) == 1:
                return _resp(400, {"message": "invalid property"})
            return _resp(200, {"results": []})

        with mock.patch.object(notion_source.requests, "post", side_effect=responder):
            notion_source.fetch_new_episodes(
                "t", "db", NOW, 5,
                status_property="없는속성", status_value="완료",
            )
        assert len(calls) == 2, f"재시도가 없었습니다: {len(calls)}회"
        assert "and" in calls[0]["filter"]
        assert "and" not in calls[1]["filter"]

    def test_network_error_returns_empty(self):
        import requests as rq

        with mock.patch.object(
            notion_source.requests, "post", side_effect=rq.RequestException("down")
        ):
            assert notion_source.fetch_new_episodes("t", "db", NOW, 5) == []

    def test_sanitizes_results(self):
        body = {"results": [{"properties": {
            "에피소드": {"type": "title", "title": [{"plain_text": "두 개의 전선"}]},
            "번호": {"type": "number", "number": 61},
            "활성 빌런": {"type": "select", "select": {"name": "Debt Titan"}},
        }}]}
        with mock.patch.object(
            notion_source.requests, "post", return_value=_resp(200, body)
        ):
            lines = notion_source.fetch_new_episodes("t", "db", NOW, 5)
        assert lines and "두 개의 전선" in lines[0]
        assert "61" not in lines[0]
        assert "Debt Titan" not in lines[0]


# ---------------------------------------------------------------------------
# 안전장치
# ---------------------------------------------------------------------------


class TestGates:
    @staticmethod
    def _gate(posts, quota=200, regular="OTHER"):
        from src import run_story

        with mock.patch.object(run_story, "_regular_pillar_today", return_value=regular):
            return run_story._gate(posts, NOW, NOW.date(), quota)

    def test_passes_when_clear(self):
        assert self._gate([_post(10)]) is None

    def test_blocks_when_regular_is_story(self):
        got = self._gate([_post(10)], regular="STORY")
        assert got and "정기 발행이 STORY" in got

    def test_blocks_when_too_soon(self):
        got = self._gate([_post(1)])
        assert got and "최소 간격" in got

    def test_blocks_at_daily_cap(self):
        posts = [_post(5), _post(6)]      # 둘 다 오늘
        got = self._gate(posts)
        assert got and "상한" in got

    def test_blocks_on_low_quota(self):
        got = self._gate([_post(10)], quota=1)
        assert got and "쿼터" in got

    def test_no_posts_passes(self):
        """발행 이력이 없으면 간격 판정을 건너뛴다."""
        assert self._gate([]) is None


class TestRegularPillarDetection:
    def test_detects_story_day(self):
        from src import ai_writer, content, run_story

        # 슬롯 중 하나라도 STORY 인 날을 찾는다
        for offset in range(16):
            day = dt.date(2026, 9, 14) + dt.timedelta(days=offset)
            has_story = any(
                ai_writer.pick_pillar(content.run_index(day, d)) == "STORY"
                for d in config.DISCRIMINATOR_BY_SLOT.values()
            )
            assert (run_story._regular_pillar_today(day) == "STORY") is has_story


# ---------------------------------------------------------------------------
# 비활성 · 근거 없음
# ---------------------------------------------------------------------------


class TestDisabledPaths:
    def test_disabled_returns_early(self):
        from src import run_story

        with (
            mock.patch.object(config, "EVENT_STORY_ENABLED", False),
            mock.patch.object(notion_source, "fetch_new_episodes") as fetch,
        ):
            assert run_story.run() == 0
        assert not fetch.called

    def test_no_notion_returns_early(self):
        from src import run_story

        os.environ.pop("NOTION_TOKEN", None)
        with mock.patch.object(notion_source, "fetch_new_episodes") as fetch:
            assert run_story.run() == 0
        assert not fetch.called

    def test_no_new_episodes_returns_early(self):
        from src import run_story

        with (
            mock.patch.object(notion_source, "fetch_new_episodes", return_value=[]),
            mock.patch.object(run_story, "_acquire_token") as acquire,
        ):
            assert run_story.run() == 0
        assert not acquire.called


class TestEventConfig:
    def test_disabled_by_default(self):
        """관찰 기간 중 자동 발행되면 안 된다."""
        import importlib

        os.environ.pop("EVENT_STORY_ENABLED", None)
        reloaded = importlib.reload(config)
        assert reloaded.EVENT_STORY_ENABLED is False

    def test_window_exceeds_cron_interval(self):
        """cron 6시간 주기보다 창이 넓어야 경계 누락이 없다."""
        assert config.EVENT_WINDOW_HOURS > 6

    def test_event_discriminator_distinct(self):
        assert config.DISCRIMINATOR_EVENT not in config.DISCRIMINATOR_BY_SLOT.values()

    def test_event_jitter_wider_than_regular(self):
        assert config.ANTIBOT_EVENT_JITTER[1] > config.ANTIBOT_PUBLISH_JITTER[1]


class TestPostJitterRecheck:
    """지터 도중 정기 발행이 나갈 수 있다. 발행 직전 재검증이 필요하다."""

    def test_recheck_blocks_when_regular_published_during_jitter(self):
        from src import antibot, content, notion_source, run_story
        from src.threads_client import Quota

        client = mock.Mock()
        client.get_post_quota.return_value = Quota(used=1, total=250)
        # 사전 게이트 시점: 어제 글만 존재 → 통과
        # 지터 후 재조회: 방금 정기 글이 생김 → 차단
        client.get_my_posts.side_effect = [[_post_now(20)], [_post_now(0.2)]]
        client.get_recent_texts.return_value = []

        plan = mock.Mock(pillar="STORY", seed="s", source="ai",
                         text="본문", reply_text="링크")

        with (
            mock.patch.object(notion_source, "fetch_new_episodes",
                              return_value=["Ep61 / 타입=BATTLE"]),
            mock.patch.object(run_story, "_acquire_token", return_value="tok"),
            mock.patch.object(run_story, "fetch_user_id", return_value=("1", "u")),
            mock.patch.object(run_story, "ThreadsClient", return_value=client),
            mock.patch.object(run_story, "_regular_pillar_today", return_value="OTHER"),
            mock.patch.object(content, "build_plan", return_value=plan),
            mock.patch.object(run_story, "_select_usable_image",
                              return_value=("https://x/y.png", [])),
            mock.patch.object(antibot, "jitter_sleep", return_value=0),
            # 실제 현재 시각이 CHAT 창(KST 09:00~12:05)이면 '방금 글'이 CHAT 으로
            # 분류되어 게이트에서 제외된다. 시각 의존을 없애 정기 글로 고정한다.
            mock.patch.object(run_story.chat_plan, "is_chat_time", return_value=False),
        ):
            assert run_story.run() == 0

        assert not client.publish_image_post.called
        assert not client.publish_text_post.called

    def test_publishes_when_recheck_passes(self):
        from src import antibot, content, notion_source, run_story
        from src.threads_client import Quota

        client = mock.Mock()
        client.get_post_quota.return_value = Quota(used=1, total=250)
        client.get_my_posts.return_value = [_post_now(20)]
        client.get_recent_texts.return_value = []
        client.publish_image_post.return_value = "post1"
        client.publish_self_reply.return_value = "reply1"

        plan = mock.Mock(pillar="STORY", seed="s", source="ai",
                         text="본문", reply_text="링크")

        with (
            mock.patch.object(notion_source, "fetch_new_episodes",
                              return_value=["Ep61 / 타입=BATTLE"]),
            mock.patch.object(run_story, "_acquire_token", return_value="tok"),
            mock.patch.object(run_story, "fetch_user_id", return_value=("1", "u")),
            mock.patch.object(run_story, "ThreadsClient", return_value=client),
            mock.patch.object(run_story, "_regular_pillar_today", return_value="OTHER"),
            mock.patch.object(content, "build_plan", return_value=plan),
            mock.patch.object(run_story, "_select_usable_image",
                              return_value=("https://x/y.png", [])),
            mock.patch.object(antibot, "jitter_sleep", return_value=0),
            # 실제 현재 시각이 CHAT 창(KST 09:00~12:05)이면 '방금 글'이 CHAT 으로
            # 분류되어 게이트에서 제외된다. 시각 의존을 없애 정기 글로 고정한다.
            mock.patch.object(run_story.chat_plan, "is_chat_time", return_value=False),
        ):
            assert run_story.run() == 0

        assert client.publish_image_post.called
        assert client.publish_self_reply.called


class TestScheduleSafety:
    """이벤트 슬롯이 정기 발행 직전에 있으면 게이트가 무력화된다."""

    @staticmethod
    def _kst_slots(workflow_name: str) -> list[int]:
        import glob

        import yaml

        out: list[int] = []
        for path in glob.glob(".github/workflows/*.yml"):
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            if data.get("name") != workflow_name:
                continue
            on = data.get(True) or data.get("on") or {}
            for entry in on.get("schedule") or []:
                minute, hour = entry["cron"].split()[:2]
                out.append(((int(hour) + 9) % 24) * 60 + int(minute))
        return sorted(out)

    def test_event_never_shortly_before_publish(self):
        events = self._kst_slots("Threads Story Event")
        publishes = self._kst_slots("Threads Publish")
        assert events and publishes

        # 이벤트 지터 최대 50분 + 정기 지터 8분 -> 90분 여유를 둔다
        for e in events:
            for p in publishes:
                gap = p - e
                assert not (0 < gap < 90), (
                    f"이벤트 {e // 60:02d}:{e % 60:02d} 이 "
                    f"정기 {p // 60:02d}:{p % 60:02d} 직전 {gap}분에 있습니다."
                )

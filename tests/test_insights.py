"""인사이트 수집 및 비중 자동 조절 테스트.

핵심 관심사
  1. 세 가지 응답 형태를 모두 파싱하는가
  2. 무상태로 기둥을 복원하는가
  3. 표본 부족 시 자동 조절이 차단되는가
  4. Threads 에 아무것도 쓰지 않는가
"""

from __future__ import annotations

import collections
import datetime as dt
import os
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from src import ai_writer, config, insights, weighting

KST = ZoneInfo("Asia/Seoul")
DAY = dt.date(2026, 9, 20)


@pytest.fixture(autouse=True)
def _env():
    saved = dict(os.environ)
    os.environ.update(
        YOUTUBE_URL="https://www.youtube.com/@handle",
        X_URL="https://x.com/handle",
        THREADS_APP_ID="1734799514413030",
        THREADS_APP_SECRET="a" * 32,
        THREADS_LONG_LIVED_TOKEN="THAA" + "x" * 180,
    )
    with (
        mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", ""),
        mock.patch.object(config, "ADAPTIVE_WEIGHTS_ENABLED", True),
    ):
        yield
    os.environ.clear()
    os.environ.update(saved)


def _at(hhmm: str, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime.combine(
        day, dt.time(int(hhmm[:2]), int(hhmm[3:])), tzinfo=KST
    )


# ---------------------------------------------------------------------------
# 파싱
# ---------------------------------------------------------------------------


class TestMediaInsightsParsing:
    def test_values_form(self):
        body = {"data": [
            {"name": "views", "values": [{"value": 214}]},
            {"name": "likes", "values": [{"value": 9}]},
        ]}
        got = insights.parse_media_insights(body)
        assert got == {"views": 214, "likes": 9}

    def test_repost_facade_empty(self):
        """REPOST_FACADE 는 빈 배열이 온다."""
        assert insights.parse_media_insights({"data": []}) == {}

    def test_total_value_form(self):
        body = {"data": [{"name": "likes", "total_value": {"value": 5}}]}
        assert insights.parse_media_insights(body)["likes"] == 5


class TestUserInsightsParsing:
    def test_time_series_takes_latest(self):
        body = {"data": [{"name": "views", "values": [
            {"value": 301, "end_time": "2026-09-19T08:00:00+0000"},
            {"value": 412, "end_time": "2026-09-20T08:00:00+0000"},
        ]}]}
        assert insights.parse_user_insights(body).profile_views == 412

    def test_total_value(self):
        body = {"data": [{"name": "followers_count",
                          "total_value": {"value": 128}}]}
        assert insights.parse_user_insights(body).followers == 128

    def test_link_total_values(self):
        """clicks 는 링크별로 온다. 유입 측정의 핵심."""
        body = {"data": [{"name": "clicks", "link_total_values": [
            {"value": 7, "link_url": "https://www.youtube.com/@t"},
            {"value": 2, "link_url": "https://x.com/t"},
        ]}]}
        got = insights.parse_user_insights(body)
        assert got.clicks["https://www.youtube.com/@t"] == 7
        assert got.clicks["https://x.com/t"] == 2

    def test_empty_body(self):
        got = insights.parse_user_insights({})
        assert got.followers == 0 and got.clicks == {}


# ---------------------------------------------------------------------------
# 기둥 복원
# ---------------------------------------------------------------------------


class TestPillarRestore:
    @pytest.mark.parametrize(("hhmm", "expected_disc"), [
        ("08:23", 0), ("08:31", 0),
        ("12:47", 1), ("12:55", 1),
        ("20:31", 2), ("20:39", 2),
    ])
    def test_publish_slots(self, hhmm: str, expected_disc: int):
        assert insights.discriminator_from_timestamp(_at(hhmm)) == expected_disc

    @pytest.mark.parametrize("hhmm", ["03:11", "13:55", "17:50", "23:40"])
    def test_event_slots(self, hhmm: str):
        got = insights.discriminator_from_timestamp(_at(hhmm))
        assert got == config.DISCRIMINATOR_EVENT

    @pytest.mark.parametrize("hhmm", ["15:00", "02:00", "07:00"])
    def test_outside_windows(self, hhmm: str):
        assert insights.discriminator_from_timestamp(_at(hhmm)) is None

    def test_unknown_pillar(self):
        assert insights.restore_pillar(_at("15:00")) == insights.UNKNOWN

    def test_restored_pillar_is_valid(self):
        got = insights.restore_pillar(_at("08:25"))
        assert got in ai_writer.PILLARS

    def test_same_slot_same_pillar(self):
        """멱등성. 같은 슬롯·같은 날은 같은 기둥이어야 한다."""
        a = insights.restore_pillar(_at("08:25"))
        b = insights.restore_pillar(_at("08:29"))
        assert a == b


# ---------------------------------------------------------------------------
# 집계 · 리포트
# ---------------------------------------------------------------------------


def _stat(hhmm: str, **kw) -> insights.PostStat:
    when = _at(hhmm)
    return insights.PostStat(
        post_id="p", posted_at=when, pillar=insights.restore_pillar(when), **kw
    )


class TestAggregate:
    def test_groups_by_pillar(self):
        stats = [_stat("08:25", replies=3), _stat("08:25", replies=2)]
        rows = insights.aggregate(stats)
        target = next(r for r in rows if r.pillar == stats[0].pillar)
        assert target.posts == 2 and target.replies == 5

    def test_unknown_kept_separately(self):
        rows = insights.aggregate([_stat("15:00", replies=1)])
        unknown = next(r for r in rows if r.pillar == insights.UNKNOWN)
        assert unknown.posts == 1

    def test_all_pillars_present(self):
        rows = insights.aggregate([])
        names = {r.pillar for r in rows}
        assert set(ai_writer.PILLARS) <= names

    def test_reply_avg(self):
        row = insights.PillarRow(pillar="STORY", posts=2, replies=5)
        assert row.reply_avg == 2.5


class TestReport:
    def test_clicks_shown(self):
        user = insights.UserStat(clicks={"https://www.youtube.com/@t": 7})
        out = insights.render_report(DAY, user, insights.aggregate([]), 7)
        assert "youtube.com/@t" in out and "7" in out

    def test_no_clicks_message(self):
        out = insights.render_report(DAY, insights.UserStat(),
                                     insights.aggregate([]), 7)
        assert "링크 클릭 없음" in out

    def test_failed_posts_reported(self):
        out = insights.render_report(DAY, insights.UserStat(),
                                     insights.aggregate([]), 7, failed_posts=2)
        assert "2건" in out

    def test_reply_leader_shown(self):
        stats = [_stat("08:25", replies=5), _stat("12:50", replies=1)]
        out = insights.render_report(DAY, insights.UserStat(),
                                     insights.aggregate(stats), 7)
        assert "답글 최다" in out

    def test_follower_delta(self):
        user = insights.UserStat(followers=128)
        out = insights.render_report(DAY, user, insights.aggregate([]), 7,
                                     follower_delta=-1)
        assert "(-1)" in out


# ---------------------------------------------------------------------------
# 비중 자동 조절
# ---------------------------------------------------------------------------


def _score(pillar: str, posts: int, clicks: int, replies: int):
    return weighting.PillarScore(pillar, posts, clicks, replies)


class TestScoring:
    def test_excludes_views_and_likes(self):
        """조회·좋아요는 점수에 들어가지 않는다."""
        s = _score("STORY", 10, 10, 10)
        expected = 1.0 * config.WEIGHT_SCORE_CLICKS + 1.0 * config.WEIGHT_SCORE_REPLIES
        assert s.score == pytest.approx(expected)

    def test_clicks_excluded_from_score(self):
        """v1.2.0: 클릭은 기둥 귀속이 불가능해 가중치 0. 답글만 점수에 반영된다."""
        assert config.WEIGHT_SCORE_CLICKS == 0.0
        clicks_only = _score("A", 10, 10, 0)
        replies_only = _score("B", 10, 0, 10)
        assert clicks_only.score == 0.0
        assert replies_only.score > clicks_only.score

    def test_zero_posts_no_division_error(self):
        assert _score("A", 0, 0, 0).score == 0.0


class TestGates:
    SCORES_OK = [
        _score("STORY", 10, 18, 24),
        _score("MARKET", 12, 6, 9),
        _score("PROMO", 10, 9, 3),
    ]

    def test_disabled_blocks(self):
        with mock.patch.object(config, "ADAPTIVE_WEIGHTS_ENABLED", False):
            got = weighting.decide(self.SCORES_OK, DAY, "")
        assert not got.adjusted and "ENABLED" in got.reason

    def test_manual_override_wins(self):
        with mock.patch.object(
            config, "PILLAR_ROTATION_OVERRIDE",
            "STORY,MARKET,PROMO,STORY,MARKET,PROMO,STORY,MARKET",
        ):
            got = weighting.decide(self.SCORES_OK, DAY, "")
        assert not got.adjusted and "수동" in got.reason

    def test_sample_shortage_blocks(self):
        scores = [
            _score("STORY", 10, 18, 24),
            _score("MARKET", 12, 6, 9),
            _score("PROMO", 8, 9, 3),
        ]
        got = weighting.decide(scores, DAY, "")
        assert not got.adjusted and "표본 부족" in got.reason

    def test_interval_blocks(self):
        recent = (DAY - dt.timedelta(days=5)).isoformat()
        got = weighting.decide(self.SCORES_OK, DAY, recent)
        assert not got.adjusted and "주기" in got.reason
        assert got.next_check is not None

    def test_insignificant_difference_blocks(self):
        scores = [
            _score("STORY", 10, 10, 10),
            _score("MARKET", 10, 9, 9),
            _score("PROMO", 10, 8, 8),
        ]
        got = weighting.decide(scores, DAY, "")
        assert not got.adjusted and "차이 미미" in got.reason

    def test_passes_when_clear(self):
        got = weighting.decide(self.SCORES_OK, DAY, "")
        assert got.adjusted
        assert got.promoted == "STORY"


class TestRotationBuild:
    @pytest.mark.parametrize("counts", [
        {"MARKET": 3, "PROMO": 2, "STORY": 3},
        {"MARKET": 4, "PROMO": 2, "STORY": 2},
        {"MARKET": 2, "PROMO": 2, "STORY": 4},
    ])
    def test_no_adjacent_duplicates(self, counts: dict[str, int]):
        got = weighting.build_rotation(counts, 8)
        assert all(got[i] != got[(i + 1) % len(got)] for i in range(len(got)))

    def test_counts_preserved(self):
        counts = {"MARKET": 3, "PROMO": 2, "STORY": 3}
        got = weighting.build_rotation(counts, 8)
        assert collections.Counter(got) == collections.Counter(counts)

    def test_deterministic(self):
        counts = {"MARKET": 3, "PROMO": 2, "STORY": 3}
        assert weighting.build_rotation(counts, 8) == weighting.build_rotation(
            counts, 8
        )


class TestInvariants:
    def test_promo_cap(self):
        bad = ("PROMO",) * 3 + ("MARKET", "STORY") * 2 + ("MARKET",)
        assert any("S5" in v for v in weighting.validate_rotation(bad))

    def test_story_floor(self):
        bad = ("MARKET", "PROMO") * 3 + ("MARKET", "STORY")
        assert any("S7" in v for v in weighting.validate_rotation(bad))

    def test_pillar_floor(self):
        bad = ("MARKET", "STORY") * 4
        assert any("S4" in v for v in weighting.validate_rotation(bad))

    def test_adjacent_detected(self):
        bad = ("STORY", "STORY", "MARKET", "PROMO",
               "MARKET", "STORY", "MARKET", "PROMO")
        assert any("연속 중복" in v for v in weighting.validate_rotation(bad))

    def test_current_rotation_valid(self):
        assert weighting.validate_rotation(ai_writer.PILLAR_ROTATION) == []

    def test_adjusted_result_passes_invariants(self):
        got = weighting.decide(TestGates.SCORES_OK, DAY, "")
        assert got.adjusted
        assert weighting.validate_rotation(got.after) == []


class TestRotationOverride:
    def test_active_rotation_default(self):
        assert ai_writer.active_rotation() == ai_writer.PILLAR_ROTATION

    def test_override_applied(self):
        custom = "STORY,MARKET,PROMO,STORY,MARKET,PROMO,STORY,MARKET"
        with mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", custom):
            assert ai_writer.active_rotation() == tuple(custom.split(","))

    def test_invalid_override_falls_back(self):
        with mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", "NOPE,BAD"):
            assert ai_writer.active_rotation() == ai_writer.PILLAR_ROTATION

    def test_empty_override_falls_back(self):
        with mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", "   "):
            assert ai_writer.active_rotation() == ai_writer.PILLAR_ROTATION


class TestReadOnly:
    """인사이트는 Threads 에 아무것도 쓰지 않는다."""

    def test_only_get_requests(self):
        from src import run_insights
        from src.threads_client import Quota

        methods: list[str] = []

        def record(method: str, url: str, *, params: dict):
            methods.append(method)
            if url.endswith("/me"):
                return {"id": "1", "username": "u"}
            if url.endswith("/threads_insights"):
                return {"data": []}
            if url.endswith("/threads"):
                return {"data": []}
            return {"data": []}

        client = mock.Mock()
        client.get_user_insights.return_value = {"data": []}
        client.get_my_posts.return_value = []
        client.get_post_quota.return_value = Quota(used=0, total=250)

        with (
            mock.patch("src.threads_client._request", side_effect=record),
            mock.patch.object(run_insights, "ThreadsClient", return_value=client),
            mock.patch.object(run_insights, "fetch_user_id",
                              return_value=("1", "u")),
            mock.patch.object(run_insights.notifier, "send"),
        ):
            assert run_insights.run() == 0

        assert not client.publish_image_post.called
        assert not client.publish_text_post.called
        assert not client.publish_self_reply.called

    def test_disabled_returns_early(self):
        from src import run_insights

        with (
            mock.patch.object(config, "INSIGHTS_ENABLED", False),
            mock.patch.object(run_insights, "fetch_user_id") as fetch,
        ):
            assert run_insights.run() == 0
        assert not fetch.called

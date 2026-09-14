"""워치독 테스트.

핵심 관심사
  1. 조용한 감시자인가 — 정상일 때 알림이 나가면 안 된다
  2. 진짜 공백을 잡는가 — 워크플로우가 아예 안 돈 경우
"""

from __future__ import annotations

import datetime as dt

import pytest

from src import watchdog
from src.watchdog import Severity

NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.UTC)


def _post(hours_ago: float) -> dict:
    when = NOW - dt.timedelta(hours=hours_ago)
    return {"id": "p1", "text": "글", "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S+0000")}


class TestTimestampParsing:
    @pytest.mark.parametrize(
        "raw",
        [
            "2026-09-13T00:00:00+0000",
            "2026-09-13T00:00:00+00:00",
            "2026-09-13T00:00:00Z",
        ],
    )
    def test_formats(self, raw: str):
        assert watchdog.parse_threads_timestamp(raw) is not None

    def test_empty(self):
        assert watchdog.parse_threads_timestamp("") is None

    def test_garbage(self):
        assert watchdog.parse_threads_timestamp("어제") is None

    def test_naive_gets_utc(self):
        got = watchdog.parse_threads_timestamp("2026-09-13T00:00:00")
        assert got is not None and got.tzinfo is not None


class TestPublishFreshness:
    def test_recent_is_ok(self):
        got = watchdog.check_publish_freshness([_post(3)], NOW)
        assert got.severity == Severity.OK

    def test_max_normal_gap_is_not_flagged(self):
        """전날 08:23 -> 다음날 20:31 = 36시간 8분. 정상인데 잡히면 오탐이다."""
        got = watchdog.check_publish_freshness([_post(36.2)], NOW)
        assert got.severity == Severity.OK

    def test_delayed_warns(self):
        got = watchdog.check_publish_freshness([_post(45)], NOW)
        assert got.severity == Severity.WARN

    def test_long_stop_is_critical(self):
        got = watchdog.check_publish_freshness([_post(100)], NOW)
        assert got.severity == Severity.CRITICAL
        assert "cron" in got.detail

    def test_no_posts_is_critical(self):
        got = watchdog.check_publish_freshness([], NOW)
        assert got.severity == Severity.CRITICAL

    def test_unparseable_timestamps_warn(self):
        got = watchdog.check_publish_freshness([{"id": "p", "timestamp": "어제"}], NOW)
        assert got.severity == Severity.WARN

    def test_uses_latest_post(self):
        got = watchdog.check_publish_freshness([_post(100), _post(2)], NOW)
        assert got.severity == Severity.OK


class TestStaleThreshold:
    """임계값은 슬롯 배치에서 산출한다. 고정하면 설정 변경 시 오탐이 난다."""

    def test_covers_max_slot_gap(self):
        assert watchdog.stale_threshold_hours(0) > watchdog.SLOT_SPREAD_MAX_GAP_HOURS

    def test_rest_day_extends_threshold(self):
        base = watchdog.stale_threshold_hours(0)
        assert watchdog.stale_threshold_hours(1) == pytest.approx(base + 24)

    def test_rest_day_prevents_false_alarm(self):
        """휴식일 활성 시 60시간 공백은 정상이다."""
        got = watchdog.check_publish_freshness([_post(59)], NOW, threshold_hours=
                                               watchdog.stale_threshold_hours(1))
        assert got.severity == Severity.OK

    def test_negative_rest_days_ignored(self):
        assert watchdog.stale_threshold_hours(-3) == watchdog.stale_threshold_hours(0)


class TestQuota:
    def test_normal(self):
        assert watchdog.check_quota(2, 250).severity == Severity.OK

    def test_surge_is_critical(self):
        got = watchdog.check_quota(200, 250)
        assert got.severity == Severity.CRITICAL
        assert "중복" in got.detail

    def test_zero_total_warns(self):
        assert watchdog.check_quota(0, 0).severity == Severity.WARN


class TestReplyActivity:
    def test_disabled_is_ok(self):
        got = watchdog.check_reply_activity([], NOW, enabled=False)
        assert got.severity == Severity.OK

    def test_no_replies_is_ok(self):
        """대상 댓글이 없으면 답글이 없는 것이 정상이다."""
        got = watchdog.check_reply_activity([], NOW, enabled=True)
        assert got.severity == Severity.OK

    def test_recent_reply_ok(self):
        stamps = [NOW - dt.timedelta(hours=5)]
        assert watchdog.check_reply_activity(stamps, NOW, enabled=True).severity == Severity.OK

    def test_stale_reply_warns_only(self):
        """답글 정체는 CRITICAL 로 올리지 않는다. 오탐이 잦다."""
        stamps = [NOW - dt.timedelta(hours=200)]
        got = watchdog.check_reply_activity(stamps, NOW, enabled=True)
        assert got.severity == Severity.WARN


class TestTokenExpiry:
    def test_fresh_is_ok(self):
        got = watchdog.check_token_expiry(dt.date(2026, 9, 13), "2026-09-12")
        assert got.severity == Severity.OK

    def test_near_expiry_warns(self):
        """갱신 임계(D-10) 진입 시 경고. 그 전에는 무음이어야 한다."""
        got = watchdog.check_token_expiry(dt.date(2026, 9, 13), "2026-07-25")
        assert got.severity == Severity.WARN

    def test_silent_before_refresh_threshold(self):
        """잔여 15일은 갱신 워크플로우가 아직 돌 시점이 아니다."""
        got = watchdog.check_token_expiry(dt.date(2026, 9, 13), "2026-07-30")
        assert got.severity == Severity.OK

    def test_critical(self):
        got = watchdog.check_token_expiry(dt.date(2026, 9, 13), "2026-07-17")
        assert got.severity == Severity.CRITICAL

    def test_unknown_is_silent(self):
        """발급일 미상은 발행 워크플로우가 이미 알린다. 중복 경보 금지."""
        got = watchdog.check_token_expiry(dt.date(2026, 9, 13), "")
        assert got.severity == Severity.OK


class TestSilence:
    """조용한 감시자 원칙."""

    def test_all_ok_sends_nothing(self):
        report = watchdog.build_report([
            watchdog.Finding(Severity.OK, "발행 정상", "3시간 전"),
            watchdog.Finding(Severity.OK, "쿼터 정상", "2/250"),
        ])
        assert not report.has_alert
        assert report.to_message() == ""

    def test_alert_message_lists_only_problems(self):
        report = watchdog.build_report([
            watchdog.Finding(Severity.OK, "쿼터 정상", "2/250"),
            watchdog.Finding(Severity.WARN, "발행 지연", "40시간 경과"),
        ])
        msg = report.to_message()
        assert "발행 지연" in msg
        assert "쿼터 정상" not in msg

    def test_critical_prefix(self):
        report = watchdog.build_report([
            watchdog.Finding(Severity.CRITICAL, "발행 장기 중단", "100시간"),
        ])
        assert "[최우선]" in report.to_message()

    def test_warn_prefix(self):
        report = watchdog.build_report([
            watchdog.Finding(Severity.WARN, "발행 지연", "40시간"),
        ])
        assert "[경고]" in report.to_message()

    def test_worst_severity(self):
        report = watchdog.build_report([
            watchdog.Finding(Severity.WARN, "a", "x"),
            watchdog.Finding(Severity.CRITICAL, "b", "y"),
        ])
        assert report.worst == Severity.CRITICAL

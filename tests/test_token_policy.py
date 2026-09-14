"""토큰 갱신 정책 및 차단 감지 테스트.

배경: 매 실행 토큰 갱신이 Meta 문서의 갱신 조건에 어긋나고,
      개발자 계정 checkpoint 로 이어진 정황이 있었다.
      갱신을 주 1회로 분리하고 차단(code=200)을 즉시 중단하도록 바꿨다.
"""

from __future__ import annotations

import datetime as dt
import os
from unittest import mock

import pytest

from src import config, token_manager
from src.threads_client import ThreadsApiError


class TestBlockedDetection:
    def test_code_200_is_blocked(self):
        assert ThreadsApiError(400, "{}", code=200).is_blocked

    def test_message_match_is_blocked(self):
        err = ThreadsApiError(400, '{"error":{"message":"API access blocked."}}')
        assert err.is_blocked

    def test_normal_error_not_blocked(self):
        assert not ThreadsApiError(400, "{}", code=36001).is_blocked

    def test_auth_error_not_blocked(self):
        err = ThreadsApiError(400, "{}", code=190)
        assert err.is_auth_error
        assert not err.is_blocked


class TestRefreshPolicy:
    """기본은 갱신하지 않는다. 주간 워크플로우만 갱신한다."""

    @staticmethod
    def _settings():
        s = mock.Mock()
        s.threads_token = "THAAold"
        s.telegram_bot_token = "bot"
        s.telegram_chat_id = "chat"
        s.can_persist_token = True
        s.gh_repo = "owner/repo"
        s.gh_pat = "pat"
        return s

    def test_default_does_not_refresh(self):
        from src import main

        with (
            mock.patch.object(config, "REFRESH_ON_EVERY_RUN", False),
            mock.patch.object(main.token_manager, "refresh_long_lived_token") as ref,
        ):
            got = main._acquire_token(self._settings())

        assert got == "THAAold"
        assert not ref.called

    def test_opt_in_refreshes(self):
        from src import main

        with (
            mock.patch.object(config, "REFRESH_ON_EVERY_RUN", True),
            mock.patch.object(main.token_manager, "refresh_long_lived_token",
                              return_value="THAAnew"),
            mock.patch.object(main.token_manager, "persist_token_to_secret"),
        ):
            assert main._acquire_token(self._settings()) == "THAAnew"

    def test_refresh_records_date(self):
        """갱신일을 기록해야 만료 경보가 정확해진다."""
        from src import main

        calls: list[tuple] = []

        def persist(repo, pat, value, name=token_manager.TOKEN_SECRET_NAME):
            calls.append((name, value))

        with (
            mock.patch.object(main.token_manager, "refresh_long_lived_token",
                              return_value="THAAnew"),
            mock.patch.object(main.token_manager, "persist_token_to_secret",
                              side_effect=persist),
        ):
            main.refresh_and_persist(self._settings())

        names = [n for n, _ in calls]
        assert token_manager.TOKEN_SECRET_NAME in names
        assert config.SECRET_REFRESHED_AT_NAME in names

    def test_refresh_failure_returns_old_token(self):
        from src import main

        with (
            mock.patch.object(main.token_manager, "refresh_long_lived_token",
                              side_effect=token_manager.TokenRefreshError("x")),
            mock.patch.object(main.notifier, "send"),
        ):
            assert main.refresh_and_persist(self._settings()) == "THAAold"


class TestEffectiveIssueDate:
    """주 1회 갱신 체제에서는 마지막 갱신일이 만료 기준이다."""

    def test_refreshed_wins_when_later(self):
        got = token_manager.effective_issue_date("2026-07-01", "2026-09-10")
        assert got == "2026-09-10"

    def test_issued_wins_when_later(self):
        got = token_manager.effective_issue_date("2026-09-10", "2026-07-01")
        assert got == "2026-09-10"

    def test_only_issued(self):
        assert token_manager.effective_issue_date("2026-09-10", "") == "2026-09-10"

    def test_only_refreshed(self):
        assert token_manager.effective_issue_date("", "2026-09-10") == "2026-09-10"

    def test_both_empty(self):
        assert token_manager.effective_issue_date("", "") == ""

    def test_invalid_ignored(self):
        assert token_manager.effective_issue_date("2026/07/01", "2026-09-10") == (
            "2026-09-10"
        )

    def test_all_invalid_returns_empty(self):
        assert token_manager.effective_issue_date("bad", "worse") == ""

    def test_expiry_uses_refresh_date(self):
        """갱신했는데 발급일 기준으로 경보가 울리면 오탐이다."""
        basis = token_manager.effective_issue_date("2026-07-01", "2026-09-12")
        got = token_manager.assess_expiry(dt.date(2026, 9, 13), basis)
        assert got.level == "ok"


class TestWatchdogCallReduction:
    def test_conversation_scan_limited(self):
        from src import watchdog

        assert watchdog.CONVERSATION_SCAN_LIMIT <= 1

    def test_blocked_is_critical(self):
        """차단은 경고가 아니라 최우선이다."""
        err = ThreadsApiError(400, "{}", code=200)
        assert err.is_blocked


class TestWorkflowSeparation:
    def test_refresh_workflow_exists(self):
        from pathlib import Path

        path = Path(".github/workflows/token_refresh.yml")
        assert path.exists()
        body = path.read_text(encoding="utf-8")
        assert "REFRESH_ON_EVERY_RUN: \"true\"" in body
        assert "python -m src.run_refresh" in body

    def test_publish_workflow_does_not_force_refresh(self):
        from pathlib import Path

        body = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")
        assert "REFRESH_ON_EVERY_RUN" not in body



@pytest.fixture(autouse=True)
def _env():
    saved = dict(os.environ)
    os.environ.setdefault("YOUTUBE_URL", "https://www.youtube.com/@handle")
    os.environ.setdefault("X_URL", "https://x.com/handle")
    yield
    os.environ.clear()
    os.environ.update(saved)


class TestRefreshThreshold:
    """D-10 임계 기반 갱신. 판정에 API 호출이 들어가면 안 된다."""

    @staticmethod
    def _assess(days_left: int | None):
        return token_manager.ExpiryAssessment(days_left, "ok", "msg")

    def test_far_from_expiry_skips(self):
        from src.run_refresh import _should_refresh

        assert not _should_refresh(self._assess(60), False)
        assert not _should_refresh(self._assess(11), False)

    def test_at_threshold_refreshes(self):
        from src.run_refresh import _should_refresh

        assert _should_refresh(
            self._assess(config.TOKEN_REFRESH_THRESHOLD_DAYS), False
        )

    def test_below_threshold_refreshes(self):
        from src.run_refresh import _should_refresh

        assert _should_refresh(self._assess(1), False)

    def test_expired_refreshes(self):
        from src.run_refresh import _should_refresh

        assert _should_refresh(self._assess(-3), False)

    def test_unknown_refreshes(self):
        """잔여를 모르면 갱신한다. 방치가 더 위험하다."""
        from src.run_refresh import _should_refresh

        assert _should_refresh(self._assess(None), False)

    def test_force_overrides(self):
        from src.run_refresh import _should_refresh

        assert _should_refresh(self._assess(60), True)

    def test_retry_window_is_ten_days(self):
        """임계 진입 후 재시도 기회가 충분해야 한다."""
        from src.run_refresh import _should_refresh

        chances = sum(
            1 for d in range(config.TOKEN_REFRESH_THRESHOLD_DAYS, 0, -1)
            if _should_refresh(self._assess(d), False)
        )
        assert chances >= 10

    def test_gate_makes_no_api_call(self):
        """임계 미달이면 Threads·GitHub 호출이 0회여야 한다."""
        import os

        from src import run_refresh

        os.environ.update(
            THREADS_APP_ID="1", THREADS_APP_SECRET="s",
            THREADS_LONG_LIVED_TOKEN="THAA" + "x" * 180,
        )
        with (
            mock.patch.object(config, "TOKEN_REFRESHED_AT",
                              dt.date.today().isoformat()),
            mock.patch.object(config, "TOKEN_ISSUED_AT", ""),
            mock.patch.object(run_refresh, "refresh_and_persist") as refresh,
            mock.patch.object(run_refresh, "fetch_user_id") as fetch,
        ):
            assert run_refresh.run() == 0
        assert not refresh.called
        assert not fetch.called


class TestAlertThresholdAlignment:
    """경보 임계가 갱신 임계보다 넓으면 정상 상태에서 매일 경보가 울린다."""

    def test_warn_matches_refresh_threshold(self):
        assert config.TOKEN_WARN_DAYS == config.TOKEN_REFRESH_THRESHOLD_DAYS

    def test_escalation_order(self):
        assert (
            config.TOKEN_CRITICAL_DAYS
            < config.TOKEN_URGENT_DAYS
            < config.TOKEN_WARN_DAYS
        )

    def test_silent_before_threshold(self):
        got = token_manager.assess_expiry(dt.date(2026, 11, 2), "2026-09-14")
        assert got.level == "ok"
        assert not got.should_alert

    def test_warns_at_threshold(self):
        got = token_manager.assess_expiry(dt.date(2026, 11, 3), "2026-09-14")
        assert got.level == "warn"

    def test_urgent_after_five_failures(self):
        got = token_manager.assess_expiry(dt.date(2026, 11, 8), "2026-09-14")
        assert got.level == "urgent"

    def test_critical_near_expiry(self):
        got = token_manager.assess_expiry(dt.date(2026, 11, 11), "2026-09-14")
        assert got.level == "critical"


class TestRefreshWorkflowSchedule:
    def test_runs_daily(self):
        from pathlib import Path

        import yaml

        data = yaml.safe_load(
            Path(".github/workflows/token_refresh.yml").read_text(encoding="utf-8")
        )
        on = data.get(True) or data.get("on") or {}
        crons = [c["cron"] for c in on.get("schedule", [])]
        assert len(crons) == 1
        # 매일이어야 임계 진입 후 재시도가 가능하다
        assert crons[0].split()[4] == "*"

    def test_force_input_exists(self):
        from pathlib import Path

        body = Path(".github/workflows/token_refresh.yml").read_text(encoding="utf-8")
        assert "FORCE_REFRESH" in body

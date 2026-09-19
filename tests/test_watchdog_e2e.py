"""워치독 엔드투엔드 전수 테스트.

HTTP 레이어를 모의해 run_watchdog.run() 전체 경로를 검증한다.

핵심 관심사
  1. 조용한 감시자인가 — 정상일 때 텔레그램이 나가면 안 된다
  2. 진짜 공백을 잡는가 — 무발행·쿼터급증·API장애
  3. 발행에 개입하지 않는가 — 토큰 갱신·Secret 쓰기를 하면 안 된다
  4. 워치독 실패가 파이프라인을 흔들지 않는가 — 항상 exit 0
"""

from __future__ import annotations

import datetime as dt
import os
from unittest import mock

import pytest

BASE_ENV = {
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "TELEGRAM_BOT_TOKEN": "bot",
    "TELEGRAM_ALERT_CHAT_ID": "chat",
    "GITHUB_REPOSITORY": "owner/repo",
    "YOUTUBE_URL": "https://www.youtube.com/@handle",
    "X_URL": "https://x.com/handle",
}


@pytest.fixture(autouse=True)
def _env():
    from src import config

    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("THREADS_", "TELEGRAM_", "REPLY_", "TOKEN_")):
            os.environ.pop(key, None)
    os.environ.update(BASE_ENV)
    with (
        mock.patch.object(config, "REPLY_ENABLED", True),
        mock.patch.object(config, "TOKEN_ISSUED_AT", ""),
    ):
        yield
    os.environ.clear()
    os.environ.update(saved)


def _iso(hours_ago: float) -> str:
    when = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeApi:
    def __init__(
        self,
        *,
        post_age_hours: float = 3,
        quota_used: int = 2,
        reply_age_hours: float | None = 5,
        auth_error: bool = False,
        conversation_error: bool = False,
        no_posts: bool = False,
    ):
        self.post_age_hours = post_age_hours
        self.quota_used = quota_used
        self.reply_age_hours = reply_age_hours
        self.auth_error = auth_error
        self.conversation_error = conversation_error
        self.no_posts = no_posts
        self.calls: list[str] = []

    def __call__(self, method: str, url: str, *, params: dict):
        self.calls.append(url)

        from src.threads_client import ThreadsApiError

        if self.auth_error:
            raise ThreadsApiError(400, '{"error":{"code":190}}', code=190)

        if url.endswith("/me"):
            return {"id": "999", "username": "ryuryan091"}

        if url.endswith("/threads_publishing_limit"):
            return {"data": [{"quota_usage": self.quota_used,
                              "config": {"quota_total": 250}}]}

        if url.endswith("/threads"):
            if self.no_posts:
                return {"data": []}
            return {"data": [{"id": "p1", "text": "글",
                              "timestamp": _iso(self.post_age_hours)}]}

        if url.endswith("/conversation"):
            if self.conversation_error:
                raise ThreadsApiError(500, "server error", code=1)
            if self.reply_age_hours is None:
                return {"data": []}
            return {"data": [{
                "id": "r1", "text": "내 답글", "username": "ryuryan091",
                "timestamp": _iso(self.reply_age_hours),
                "replied_to": {"id": "c1"},
                "is_reply": True, "is_reply_owned_by_me": True,
                "hide_status": "NOT_HUSHED",
            }]}

        return {}


def _run(fake: FakeApi) -> tuple[int, mock.Mock]:
    """워치독을 돌리고 (종료코드, notifier.send 목) 을 돌려준다."""
    from src import run_watchdog

    with (
        mock.patch("src.threads_client._request", side_effect=fake),
        mock.patch("src.threads_client.time.sleep"),
        mock.patch.object(run_watchdog.notifier, "send") as send,
        # 이 파일의 가짜 게시물은 '정기 글' 시나리오다. 실제 현재 시각에 따라
        # CHAT 창(KST 09:00~12:05)에 들어가면 신선도 판정에서 빠지므로 고정한다.
        # CHAT 제외 동작은 test_chat_integration.py 가 따로 검증한다.
        mock.patch.object(run_watchdog.chat_plan, "is_chat_time", return_value=False),
    ):
        code = run_watchdog.run()
    return code, send


# ---------------------------------------------------------------------------
# 침묵 원칙
# ---------------------------------------------------------------------------


class TestSilenceWhenHealthy:
    def test_all_normal_sends_nothing(self):
        code, send = _run(FakeApi())
        assert code == 0
        assert not send.called

    def test_no_replies_is_still_silent(self):
        """대상 댓글이 없어 답글이 없는 것은 정상이다."""
        code, send = _run(FakeApi(reply_age_hours=None))
        assert code == 0
        assert not send.called

    def test_conversation_failure_is_silent(self):
        """대화 조회 실패는 답글 없음으로 처리. 경보 대상 아님."""
        code, send = _run(FakeApi(conversation_error=True))
        assert code == 0
        assert not send.called

    def test_token_issued_at_unknown_is_silent(self):
        """발급일 미상은 발행 워크플로우가 알린다. 워치독은 침묵."""
        code, send = _run(FakeApi())
        assert not send.called


# ---------------------------------------------------------------------------
# 경보
# ---------------------------------------------------------------------------


class TestAlerts:
    def test_max_normal_gap_stays_silent(self):
        """36시간 8분은 슬롯 분산상 정상. 경보가 나가면 오탐이다."""
        _, send = _run(FakeApi(post_age_hours=36.2))
        assert not send.called

    def test_publish_delay_alerts(self):
        code, send = _run(FakeApi(post_age_hours=45))
        assert code == 0
        assert send.called
        assert "발행 지연" in send.call_args[0][2]

    def test_long_stop_is_critical(self):
        _, send = _run(FakeApi(post_age_hours=100))
        msg = send.call_args[0][2]
        assert "[최우선]" in msg
        assert "발행 장기 중단" in msg

    def test_no_posts_alerts(self):
        _, send = _run(FakeApi(no_posts=True))
        assert send.called
        assert "발행 이력 없음" in send.call_args[0][2]

    def test_quota_surge_alerts(self):
        _, send = _run(FakeApi(quota_used=200))
        msg = send.call_args[0][2]
        assert "쿼터 급증" in msg
        assert "[최우선]" in msg

    def test_auth_error_alerts_critical(self):
        _, send = _run(FakeApi(auth_error=True))
        msg = send.call_args[0][2]
        assert "API 접근 실패" in msg
        assert "[최우선]" in msg

    def test_stale_reply_alerts_warn_only(self):
        _, send = _run(FakeApi(reply_age_hours=200))
        msg = send.call_args[0][2]
        assert "답글 활동 정체" in msg
        assert "[경고]" in msg

    def test_token_expiry_alerts(self):
        from src import config

        with mock.patch.object(config, "TOKEN_ISSUED_AT", "2020-01-01"):
            _, send = _run(FakeApi())
        assert send.called
        assert "토큰 만료" in send.call_args[0][2]

    def test_multiple_problems_in_one_message(self):
        _, send = _run(FakeApi(post_age_hours=100, quota_used=200))
        msg = send.call_args[0][2]
        assert "발행 장기 중단" in msg
        assert "쿼터 급증" in msg
        assert "이상 2건" in msg


# ---------------------------------------------------------------------------
# 발행 비개입
# ---------------------------------------------------------------------------


class TestNoInterference:
    """워치독은 감시만 한다. 발행 파이프라인에 개입하면 안 된다."""

    def test_does_not_refresh_token(self):
        from src import run_watchdog, token_manager

        with (
            mock.patch("src.threads_client._request", side_effect=FakeApi()),
            mock.patch.object(run_watchdog.notifier, "send"),
            mock.patch.object(token_manager, "refresh_long_lived_token") as refresh,
        ):
            run_watchdog.run()
        assert not refresh.called

    def test_does_not_write_secret(self):
        from src import run_watchdog, token_manager

        with (
            mock.patch("src.threads_client._request", side_effect=FakeApi()),
            mock.patch.object(run_watchdog.notifier, "send"),
            mock.patch.object(token_manager, "persist_token_to_secret") as persist,
        ):
            run_watchdog.run()
        assert not persist.called

    def test_does_not_publish(self):
        """threads_publishing_limit 는 조회용이므로 발행 엔드포인트와 구분한다."""
        fake = FakeApi()
        _run(fake)
        assert not any(url.endswith("/threads_publish") for url in fake.calls)

    def test_does_not_create_container(self):
        """컨테이너 생성은 POST /threads 다. 워치독은 GET 만 해야 한다."""
        from src import run_watchdog

        methods: list[str] = []

        def record(method: str, url: str, *, params: dict):
            methods.append(method)
            return FakeApi()(method, url, params=params)

        with (
            mock.patch("src.threads_client._request", side_effect=record),
            mock.patch.object(run_watchdog.notifier, "send"),
        ):
            run_watchdog.run()

        assert methods and all(m == "GET" for m in methods)


# ---------------------------------------------------------------------------
# 견고성
# ---------------------------------------------------------------------------


class TestResilience:
    def test_unexpected_error_returns_zero(self):
        """워치독 실패로 Actions 를 빨갛게 만들지 않는다."""
        from src import run_watchdog

        with (
            mock.patch.object(run_watchdog, "run", side_effect=RuntimeError("boom")),
            mock.patch.object(run_watchdog.notifier, "send") as send,
        ):
            code = run_watchdog.main()
        assert code == 0
        assert send.called
        assert "워치독 자체 오류" in send.call_args[0][2]

    def test_notifier_failure_does_not_crash(self):
        from src import run_watchdog

        with (
            mock.patch.object(run_watchdog, "run", side_effect=RuntimeError("boom")),
            mock.patch.object(run_watchdog.notifier, "send",
                              side_effect=RuntimeError("telegram down")),
        ):
            assert run_watchdog.main() == 0

    def test_reply_disabled_skips_check(self):
        from src import config

        with mock.patch.object(config, "REPLY_ENABLED", False):
            _, send = _run(FakeApi(reply_age_hours=500))
        assert not send.called

    def test_scan_limit_respected(self):
        from src import watchdog

        fake = FakeApi()
        _run(fake)
        conv_calls = [u for u in fake.calls if u.endswith("/conversation")]
        assert len(conv_calls) <= watchdog.RECENT_POSTS_TO_SCAN

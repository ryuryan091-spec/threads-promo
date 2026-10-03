"""v1.6.0 계정 보호(안전) 모드 테스트 (DESIGN_V16_SAFETY.md).

이 모듈은 conftest 의 legacy 프로필(자동화 켜짐·이전 캡)을 적용하지 않는다(SAFETY_MODULES).
필요한 값은 테스트마다 cfg 픽스처로 명시한다. 네트워크는 쓰지 않는다(HTTP·클라이언트 모의).

  A. safety 단위 — 기본값, 킬 스위치, 워밍업, 예산·정기 몫 예약, 링크 비율, 회로 차단기, 요약
  B. 쓰기 경로 통합 — main(정기) · run_chat · run_story · run_reply(답글·이어쓰기)
  C. 읽기 경로 — watchdog 오탐 방지, golive_check C13
  D. 워크플로 env 일치 — verify_repo 검사 11
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import logging
import os
import pathlib
import sys
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import yaml

from src import chat_plan, config, safety
from src.threads_client import ContainerNotReadyError, Quota, ThreadsApiError

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
sys.path.insert(0, str(ROOT / "scripts"))

KST = ZoneInfo("Asia/Seoul")
DAY = dt.date(2026, 9, 21)   # 월요일

BLOCKED = ThreadsApiError(400, '{"error":{"code":200,"message":"access blocked"}}', code=200)
AUTH = ThreadsApiError(400, '{"error":{"code":190}}', code=190)
HTTP401 = ThreadsApiError(401, "unauthorized", code=None)
SERVER = ThreadsApiError(500, "server error", code=1)

BASE_ENV = {
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "CLAUDE_AI_KEY": "sk-ant-test",
    "DRY_RUN": "false",
    "EVENT_NAME": "workflow_dispatch",
    "GITHUB_REPOSITORY": "owner/repo",
    "YOUTUBE_URL": "https://www.youtube.com/@handle",
    "X_URL": "https://x.com/handle",
}

SAFETY_KEYS = tuple(config.SAFETY_VARIABLE_DEFAULTS)


@pytest.fixture(autouse=True)
def _env():
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("THREADS_", "CLAUDE_", "DRY_RUN", "EVENT_", "TRIGGER", "SLOT",
                           "REPLY_", "TELEGRAM_", "TOKEN_", "PUBLISH_SLOTS")) or key in SAFETY_KEYS:
            os.environ.pop(key, None)
    os.environ.update(BASE_ENV)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def cfg(monkeypatch):
    """config 를 '자동화 켜짐·워밍업 없음·안전 기본 캡' 기준으로 고정하고, 덮어쓸 함수를 돌려준다."""
    base = {
        "AUTOMATION_ENABLED": True,
        "DAILY_POST_BUDGET": 2,
        "LINK_REPLY_PCT": 0,
        "WARMUP_UNTIL": "",
        "REPLY_CANNED_ENABLED": False,
        "REPLY_ENABLED": True,
        "FOLLOWUP_ENABLED": False,
        "CHAT_ENABLED": True,
        "EVENT_STORY_ENABLED": True,
        "PUBLISH_WEEKLY_REST_DAYS": 0,
        "PUBLISH_SLOTS": ("B",),          # 당첨 슬롯을 B 12:26 으로 고정
        "YOUTUBE_URL": BASE_ENV["YOUTUBE_URL"],
        "X_URL": BASE_ENV["X_URL"],
        "AI_ENABLED": True,
        "TOKEN_ISSUED_AT": "",
    }

    def apply(**kw):
        for key, value in kw.items():
            monkeypatch.setattr(config, key, value)

    apply(**base)
    return apply


def _kst(day: dt.date, hhmm: str) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), tzinfo=KST)


def _ts(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _post(pid: str, when: dt.datetime, media: str = "IMAGE", **extra) -> dict:
    return {"id": pid, "text": "글", "timestamp": _ts(when), "media_type": media, **extra}


# ===========================================================================
# A. safety 단위
# ===========================================================================


class TestDefaults:
    def test_code_defaults_are_safe(self):
        """환경변수 없이 import 하면 안전 기본값이다(S1~S5)."""
        saved = {k: os.environ.pop(k) for k in SAFETY_KEYS if k in os.environ}
        try:
            fresh = importlib.reload(config)
            assert fresh.AUTOMATION_ENABLED is False
            assert fresh.DAILY_POST_BUDGET == 2
            assert fresh.LINK_REPLY_PCT == 0
            assert fresh.WARMUP_UNTIL == ""
            assert fresh.REPLY_CANNED_ENABLED is False
            assert (fresh.REPLY_DAILY_CAP, fresh.REPLY_AUTHOR_DAILY_CAP,
                    fresh.REPLY_THREAD_AUTHOR_CAP, fresh.REPLY_PER_RUN_CAP,
                    fresh.REPLY_SCHEDULED_RUN_CAP) == (10, 1, 2, 2, 3)
            assert fresh.FOLLOWUP_ENABLED is False
        finally:
            os.environ.update(saved)
            importlib.reload(config)

    def test_defaults_table_matches_code(self):
        saved = {k: os.environ.pop(k) for k in SAFETY_KEYS if k in os.environ}
        try:
            fresh = importlib.reload(config)
            for key, expected in fresh.SAFETY_VARIABLE_DEFAULTS.items():
                value = getattr(fresh, key)
                text = ("true" if value else "false") if isinstance(value, bool) else str(value)
                assert text == expected, key
        finally:
            os.environ.update(saved)
            importlib.reload(config)

    @pytest.mark.parametrize("raw", ["abc", "1.5", "  ", ""])
    def test_safe_int_env_invalid_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv("X_SAFE_INT", raw)
        assert config._safe_int_env("X_SAFE_INT", 2) == 2

    def test_safe_int_env_valid(self, monkeypatch):
        monkeypatch.setenv("X_SAFE_INT", " 5 ")
        assert config._safe_int_env("X_SAFE_INT", 2) == 5

    @pytest.mark.parametrize("raw,expected", [("true", True), ("TRUE", True), ("1", True),
                                              ("false", False), ("no", False), ("", False)])
    def test_automation_env_parse(self, raw, expected):
        with mock.patch.dict(os.environ, {"AUTOMATION_ENABLED": raw}):
            assert importlib.reload(config).AUTOMATION_ENABLED is expected
        importlib.reload(config)


class TestKillSwitch:
    @pytest.mark.parametrize("kind", [safety.KIND_REGULAR, safety.KIND_CHAT, safety.KIND_STORY,
                                      safety.KIND_REPLY, safety.KIND_FOLLOWUP])
    def test_off_blocks_every_write_kind(self, cfg, kind):
        cfg(AUTOMATION_ENABLED=False)
        reason = safety.block_reason(kind, DAY)
        assert reason and "AUTOMATION_ENABLED=false" in reason

    @pytest.mark.parametrize("kind", [safety.KIND_REGULAR, safety.KIND_CHAT, safety.KIND_STORY,
                                      safety.KIND_REPLY, safety.KIND_FOLLOWUP])
    def test_on_without_warmup_allows(self, cfg, kind):
        assert safety.block_reason(kind, DAY) is None

    def test_feature_flags_still_apply(self, cfg):
        cfg(CHAT_ENABLED=False, EVENT_STORY_ENABLED=False, REPLY_ENABLED=False)
        assert not safety.chat_allowed(DAY)
        assert not safety.story_allowed(DAY)
        assert not safety.replies_allowed(DAY)

    def test_off_overrides_feature_flags(self, cfg):
        cfg(AUTOMATION_ENABLED=False, FOLLOWUP_ENABLED=True)
        assert not (safety.chat_allowed(DAY) or safety.story_allowed(DAY)
                    or safety.replies_allowed(DAY) or safety.followups_allowed(DAY))


class TestWarmup:
    def test_empty_is_no_warmup(self, cfg):
        w = safety.warmup_state(DAY)
        assert not w.active and w.until is None and not w.invalid

    @pytest.mark.parametrize("raw", ["—", "-", "none", "없음"])
    def test_placeholder_is_no_warmup(self, cfg, raw):
        cfg(WARMUP_UNTIL=raw)
        assert not safety.warmup_state(DAY).active

    def test_active_through_until_inclusive(self, cfg):
        cfg(WARMUP_UNTIL=DAY.isoformat())
        assert safety.warmup_state(DAY).active
        assert safety.warmup_state(DAY - dt.timedelta(days=10)).active
        assert not safety.warmup_state(DAY + dt.timedelta(days=1)).active

    def test_active_restrictions(self, cfg):
        cfg(WARMUP_UNTIL="2099-12-31", DAILY_POST_BUDGET=5, LINK_REPLY_PCT=100,
            FOLLOWUP_ENABLED=True)
        assert safety.effective_post_budget(DAY) == 1
        assert safety.effective_link_reply_pct(DAY) == 0
        assert not safety.link_reply_selected("any", DAY)
        assert safety.block_reason(safety.KIND_REGULAR, DAY) is None
        for kind in (safety.KIND_CHAT, safety.KIND_STORY, safety.KIND_REPLY,
                     safety.KIND_FOLLOWUP):
            assert "워밍업" in safety.block_reason(kind, DAY)
        assert not safety.replies_allowed(DAY)
        assert not safety.followups_allowed(DAY)

    def test_expired_restores_settings(self, cfg):
        cfg(WARMUP_UNTIL="2026-09-20", DAILY_POST_BUDGET=3, LINK_REPLY_PCT=50,
            FOLLOWUP_ENABLED=True)
        assert not safety.warmup_state(DAY).active
        assert safety.effective_post_budget(DAY) == 3
        assert safety.effective_link_reply_pct(DAY) == 50
        assert safety.followups_allowed(DAY)
        assert safety.block_reason(safety.KIND_CHAT, DAY) is None

    def test_budget_zero_stays_zero_in_warmup(self, cfg):
        cfg(WARMUP_UNTIL="2099-12-31", DAILY_POST_BUDGET=0)
        assert safety.effective_post_budget(DAY) == 0

    @pytest.mark.parametrize("raw", ["2026/10/01", "next month", "2026-13-01", "20261001"])
    def test_invalid_date_is_fail_safe(self, cfg, caplog, raw):
        cfg(WARMUP_UNTIL=raw, LINK_REPLY_PCT=100, DAILY_POST_BUDGET=4)
        safety._WARNED_INVALID.discard(raw)
        with caplog.at_level(logging.WARNING, logger="src.safety"):
            w = safety.warmup_state(DAY)
        assert w.active and w.invalid
        assert any("WARMUP_UNTIL" in r.message for r in caplog.records)
        assert safety.effective_post_budget(DAY) == 1
        assert safety.effective_link_reply_pct(DAY) == 0
        assert "형식 오류" in safety.block_reason(safety.KIND_CHAT, DAY)

    def test_invalid_date_warns_once(self, cfg, caplog):
        cfg(WARMUP_UNTIL="bad-date")
        safety._WARNED_INVALID.discard("bad-date")
        with caplog.at_level(logging.WARNING, logger="src.safety"):
            for _ in range(3):
                safety.warmup_state(DAY)
        assert sum("WARMUP_UNTIL" in r.message for r in caplog.records) == 1


class TestCountTopLevel:
    def test_counts_only_kst_today(self, cfg):
        now = _kst(DAY, "10:00")
        posts = [
            _post("a", _kst(DAY, "00:00")),                     # 오늘 0시 (UTC 전날 15:00)
            _post("b", _kst(DAY, "09:30"), "TEXT_POST"),
            _post("c", _kst(DAY, "00:00") - dt.timedelta(seconds=1)),   # 어제 23:59:59
        ]
        assert safety.count_top_level_today(posts, now) == 2

    def test_is_reply_excluded(self, cfg):
        now = _kst(DAY, "10:00")
        posts = [_post("a", _kst(DAY, "09:10")), _post("r", _kst(DAY, "09:20"), is_reply=True)]
        assert safety.count_top_level_today(posts, now) == 1

    def test_unparseable_timestamp_counted_conservatively(self, cfg):
        now = _kst(DAY, "10:00")
        assert safety.count_top_level_today([{"id": "x", "timestamp": "??"}, {"id": "y"}], now) == 2


class TestBudgetAndReservation:
    """S2: 예산 + 정기 몫 1건 예약. 당첨 슬롯은 cfg 로 B 12:26 고정 → 예약 해제 13:13."""

    def test_slot_fixture(self, cfg):
        assert safety.regular_slot_at(DAY) == _kst(DAY, "12:26")

    def test_regular_allowed_until_budget(self, cfg):
        now = _kst(DAY, "12:30")
        one = [_post("c1", _kst(DAY, "09:30"), "TEXT_POST")]
        two = one + [_post("c2", _kst(DAY, "10:30"), "TEXT_POST")]
        assert safety.budget_block(safety.KIND_REGULAR, [], now) is None
        assert safety.budget_block(safety.KIND_REGULAR, one, now) is None
        assert "도달" in safety.budget_block(safety.KIND_REGULAR, two, now)

    def test_chat_first_post_allowed_before_regular(self, cfg):
        """예산 2 · 정기 전: CHAT 은 예산-1(=1)건까지."""
        assert safety.budget_block(safety.KIND_CHAT, [], _kst(DAY, "09:30")) is None

    def test_chat_cannot_take_regular_share(self, cfg):
        """CHAT 1건이 나간 뒤 정기 전이면 두 번째 CHAT 은 막힌다(정기 몫 예약)."""
        posts = [_post("c1", _kst(DAY, "09:30"), "TEXT_POST")]
        reason = safety.budget_block(safety.KIND_CHAT, posts, _kst(DAY, "10:30"))
        assert reason and "예약" in reason and "13:13" in reason
        # 정기는 같은 상태에서 허용된다
        assert safety.budget_block(safety.KIND_REGULAR, posts, _kst(DAY, "12:30")) is None

    def test_reservation_released_when_regular_posted(self, cfg):
        posts = [_post("r1", _kst(DAY, "12:40"), "IMAGE")]
        assert safety.regular_reserved(posts, _kst(DAY, "12:50")) is None
        assert safety.budget_block(safety.KIND_CHAT, posts, _kst(DAY, "12:50")) is None

    def test_text_fallback_regular_in_window_releases(self, cfg):
        posts = [_post("r1", _kst(DAY, "12:45"), "TEXT_POST")]   # 판정 창 안 텍스트 = 정기 폴백
        assert safety.regular_reserved(posts, _kst(DAY, "12:50")) is None

    def test_reservation_released_after_window_even_without_regular(self, cfg):
        posts = [_post("c1", _kst(DAY, "09:30"), "TEXT_POST")]
        assert safety.regular_reserved(posts, _kst(DAY, "13:12")) is not None
        assert safety.regular_reserved(posts, _kst(DAY, "13:13")) is None
        assert safety.budget_block(safety.KIND_CHAT, posts, _kst(DAY, "15:58")) is None

    def test_budget_full_blocks_chat_after_regular(self, cfg):
        posts = [_post("r1", _kst(DAY, "12:40")), _post("c1", _kst(DAY, "15:58"), "TEXT_POST")]
        assert "도달" in safety.budget_block(safety.KIND_CHAT, posts, _kst(DAY, "19:17"))

    def test_story_follows_same_rule(self, cfg):
        posts = [_post("c1", _kst(DAY, "09:30"), "TEXT_POST")]
        assert "예약" in safety.budget_block(safety.KIND_STORY, posts, _kst(DAY, "10:00"))
        assert safety.budget_block(safety.KIND_STORY, posts, _kst(DAY, "17:41")) is None

    def test_budget_one_reserves_whole_day_until_window(self, cfg):
        cfg(DAILY_POST_BUDGET=1)
        assert "예약" in safety.budget_block(safety.KIND_CHAT, [], _kst(DAY, "09:30"))
        assert safety.budget_block(safety.KIND_REGULAR, [], _kst(DAY, "12:30")) is None
        # 창이 지나도록 정기가 안 나갔으면 남은 1건은 CHAT 이 쓸 수 있다
        assert safety.budget_block(safety.KIND_CHAT, [], _kst(DAY, "15:58")) is None

    def test_budget_zero_blocks_everything(self, cfg):
        cfg(DAILY_POST_BUDGET=0)
        for kind in (safety.KIND_REGULAR, safety.KIND_CHAT, safety.KIND_STORY):
            assert "도달" in safety.budget_block(kind, [], _kst(DAY, "10:00"))

    def test_negative_budget_is_zero(self, cfg):
        cfg(DAILY_POST_BUDGET=-3)
        assert safety.effective_post_budget(DAY) == 0

    def test_rest_day_no_reservation(self, cfg):
        with mock.patch.object(safety.antibot, "is_rest_day", return_value=True):
            assert safety.regular_slot_at(DAY) is None
            posts = [_post("c1", _kst(DAY, "09:30"), "TEXT_POST")]
            assert safety.budget_block(safety.KIND_CHAT, posts, _kst(DAY, "10:30")) is None

    def test_reservation_uses_winning_slot(self, cfg):
        cfg(PUBLISH_SLOTS=("A", "B", "C", "D", "E", "F", "G"))
        slot = safety.antibot.choose_slot(DAY, list(config.PUBLISH_SLOTS),
                                          config.ANTIBOT_SLOT_SALT_PUBLISH)
        assert safety.regular_slot_at(DAY) == _kst(DAY, config.PUBLISH_SLOT_TIMES[slot])

    def test_warmup_budget_one(self, cfg):
        cfg(WARMUP_UNTIL="2099-12-31", DAILY_POST_BUDGET=3)
        posts = [_post("r1", _kst(DAY, "12:40"))]
        assert "도달" in safety.budget_block(safety.KIND_REGULAR, posts, _kst(DAY, "19:00"))


class TestLinkReplyPct:
    IDS = [f"1789{i:06d}" for i in range(2000)]

    def test_zero_never(self, cfg):
        cfg(LINK_REPLY_PCT=0)
        assert not any(safety.link_reply_selected(i, DAY) for i in self.IDS)

    def test_hundred_always(self, cfg):
        cfg(LINK_REPLY_PCT=100)
        assert all(safety.link_reply_selected(i, DAY) for i in self.IDS)

    @pytest.mark.parametrize("raw,expected", [(-5, 0), (150, 100), (37, 37)])
    def test_clamped(self, cfg, raw, expected):
        cfg(LINK_REPLY_PCT=raw)
        assert safety.effective_link_reply_pct(DAY) == expected

    def test_partial_is_deterministic_and_proportional(self, cfg):
        cfg(LINK_REPLY_PCT=30)
        first = [safety.link_reply_selected(i, DAY) for i in self.IDS]
        second = [safety.link_reply_selected(i, DAY) for i in self.IDS]
        assert first == second
        ratio = sum(first) / len(first)
        assert 0.25 < ratio < 0.35

    def test_partial_monotonic(self, cfg):
        """비율을 올리면 이전 대상은 계속 대상이다(같은 해시 기준)."""
        cfg(LINK_REPLY_PCT=20)
        low = {i for i in self.IDS if safety.link_reply_selected(i, DAY)}
        cfg(LINK_REPLY_PCT=60)
        high = {i for i in self.IDS if safety.link_reply_selected(i, DAY)}
        assert low <= high

    def test_independent_of_followup_hash(self, cfg):
        from src import run_reply

        cfg(LINK_REPLY_PCT=25)
        with mock.patch.object(config, "FOLLOWUP_PCT", 25):
            links = [safety.link_reply_selected(i, DAY) for i in self.IDS]
            follows = [run_reply.is_followup_target(i) for i in self.IDS]
        assert links != follows


class TestCircuitBreaker:
    @pytest.mark.parametrize("exc,fatal", [
        (BLOCKED, True), (AUTH, True), (HTTP401, True), (SERVER, False),
        (ThreadsApiError(400, "bad", code=100), False),
        (ThreadsApiError(400, "Access blocked for app", code=None), True),
        (ContainerNotReadyError("x"), False), (RuntimeError("x"), False),
    ])
    def test_is_account_fatal(self, exc, fatal):
        assert safety.is_account_fatal(exc) is fatal

    def test_trip_and_guard(self):
        safety.guard_write()   # 닫혀 있으면 통과
        safety.trip(AUTH)
        safety.trip(BLOCKED)   # 첫 오류를 유지한다
        assert safety.tripped() is AUTH
        with pytest.raises(ThreadsApiError) as info:
            safety.guard_write()
        assert info.value is AUTH
        safety.reset_circuit()
        safety.guard_write()

    def test_handle_fatal_notifies_once_and_returns_7(self):
        notify = mock.Mock()
        assert safety.handle_fatal(BLOCKED, "테스트", notify) == 7
        notify.assert_called_once()
        assert "code=200" in notify.call_args.args[0]
        assert safety.tripped() is BLOCKED

    def test_auth_message_mentions_reauth(self):
        assert "재인가" in safety.fatal_message(AUTH, "x")


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class TestCircuitBreakerHttp:
    """threads_client._request 수준: 재시도 없음, 차단기 개방, 이후 쓰기 HTTP 0건."""

    def _client(self):
        from src.threads_client import ThreadsClient

        return ThreadsClient("123", "tok")

    def test_blocked_with_5xx_is_not_retried(self):
        calls = []

        def fake(method, url, **_kw):
            calls.append((method, url))
            return _Resp(500, {"error": {"code": 200, "message": "access blocked"}})

        with (
            mock.patch("src.threads_client.requests.request", side_effect=fake),
            mock.patch("src.threads_client.time.sleep"),
            pytest.raises(ThreadsApiError),
        ):
            self._client().get_post_quota()
        assert len(calls) == 1
        assert safety.tripped() is not None

    def test_auth_on_read_blocks_later_writes(self):
        """조회 오류를 삼키는 경로(get_recent_texts)가 있어도 이후 쓰기는 나가지 않는다."""
        calls = []

        def fake(method, url, **_kw):
            calls.append(method)
            return _Resp(400, {"error": {"code": 190, "message": "Invalid OAuth access token"}})

        client = self._client()
        with mock.patch("src.threads_client.requests.request", side_effect=fake):
            assert client.get_recent_texts(3) == []      # 기존: 실패를 삼키고 빈 목록
            with pytest.raises(ThreadsApiError) as info:
                client.publish_text_post("본문")
        assert info.value.is_auth_error
        assert calls == ["GET"]                          # POST 는 한 번도 없다

    def test_non_fatal_4xx_does_not_trip(self):
        def fake(method, url, **_kw):
            return _Resp(400, {"error": {"code": 100, "message": "bad param"}})

        with (
            mock.patch("src.threads_client.requests.request", side_effect=fake),
            pytest.raises(ThreadsApiError),
        ):
            self._client().get_post_quota()
        assert safety.tripped() is None

    def test_server_error_still_retried(self):
        calls = []

        def fake(method, url, **_kw):
            calls.append(method)
            return _Resp(500, {"error": {"code": 1}})

        with (
            mock.patch("src.threads_client.requests.request", side_effect=fake),
            mock.patch("src.threads_client.time.sleep"),
            pytest.raises(ThreadsApiError),
        ):
            self._client().get_post_quota()
        assert len(calls) == config.HTTP_RETRY_COUNT
        assert safety.tripped() is None


class TestProfileSummary:
    def test_safe_profile_warnings_off(self, cfg):
        cfg(AUTOMATION_ENABLED=False, LINK_REPLY_PCT=50, DAILY_POST_BUDGET=9)
        assert safety.safe_profile_warnings() == []

    def test_safe_profile_warnings_on_over(self, cfg, monkeypatch):
        cfg(LINK_REPLY_PCT=10, DAILY_POST_BUDGET=4)
        monkeypatch.setattr(config, "REPLY_DAILY_CAP", 11)
        warns = safety.safe_profile_warnings()
        assert len(warns) == 3
        assert any("LINK_REPLY_PCT" in w for w in warns)
        assert any("REPLY_DAILY_CAP" in w for w in warns)
        assert any("DAILY_POST_BUDGET" in w for w in warns)

    def test_safe_profile_warnings_at_limits(self, cfg, monkeypatch):
        cfg(LINK_REPLY_PCT=0, DAILY_POST_BUDGET=3)
        monkeypatch.setattr(config, "REPLY_DAILY_CAP", 10)
        assert safety.safe_profile_warnings() == []

    def test_describe(self, cfg):
        cfg(AUTOMATION_ENABLED=False)
        text = safety.describe(DAY)
        assert "자동화=꺼짐" in text and "예산 2" in text and "링크리플 0%" in text

    def test_watch_publish_exempt(self, cfg):
        assert safety.watch_publish_exempt(DAY) is None
        cfg(DAILY_POST_BUDGET=0)
        assert "DAILY_POST_BUDGET=0" in safety.watch_publish_exempt(DAY)
        cfg(AUTOMATION_ENABLED=False)
        assert "AUTOMATION_ENABLED=false" in safety.watch_publish_exempt(DAY)


# ===========================================================================
# B. 쓰기 경로 통합
# ===========================================================================


def _plan():
    return mock.Mock(pillar="MARKET", seed="s", source="ai", text="본문입니다",
                     reply_text="링크 리플", image_url="https://x/y.png")


def _main_client(posts=None):
    client = mock.Mock()
    client.get_post_quota.return_value = Quota(used=1, total=250)
    client.get_my_posts.return_value = posts or []
    client.get_recent_texts.return_value = []
    client.publish_image_post.return_value = "post1"
    client.publish_text_post.return_value = "post_txt"
    client.publish_self_reply.return_value = "reply1"
    return client


def _run_main(client, *, entry="main"):
    from src import content, facts, main

    with (
        mock.patch.object(main, "_acquire_token", return_value="tok"),
        mock.patch.object(main, "fetch_user_id", return_value=("123", "u")),
        mock.patch.object(main, "ThreadsClient", return_value=client),
        mock.patch("src.threads_client._request",
                   side_effect=AssertionError("실네트워크 호출 금지")),
        mock.patch.object(facts, "collect",
                          return_value=mock.Mock(episodes=[], has_episodes=False)),
        mock.patch.object(content, "build_plan", return_value=_plan()) as build,
        mock.patch.object(main, "_select_usable_image", return_value=("https://x/y.png", [])),
        mock.patch.object(main.antibot, "jitter_sleep", return_value=0),
        mock.patch.object(main, "_notify_safe") as notify,
    ):
        code = main.main() if entry == "main" else main.run()
    return code, build, notify


class TestMainPublish:
    def test_kill_switch_no_calls(self, cfg):
        """S1: 설정 검사·토큰·Claude·Threads 전부 건너뛰고 0."""
        from src import main

        cfg(AUTOMATION_ENABLED=False, YOUTUBE_URL="")   # _preflight 가 돌면 RuntimeError
        client = _main_client()
        with (
            mock.patch.object(main, "load_settings", side_effect=AssertionError("호출 금지")),
            mock.patch.object(main, "ThreadsClient", side_effect=AssertionError("호출 금지")),
            mock.patch("src.ai_writer.generate", side_effect=AssertionError("Claude 호출 금지")),
        ):
            assert main.run() == 0
        assert not client.method_calls

    def test_kill_switch_main_exit_zero(self, cfg):
        cfg(AUTOMATION_ENABLED=False)
        code, build, notify = _run_main(_main_client())
        assert code == 0
        assert not build.called and not notify.called

    def test_budget_reached_skips_before_generation(self, cfg):
        now = dt.datetime.now(dt.UTC)
        client = _main_client([_post("a", now, "TEXT_POST"), _post("b", now, "TEXT_POST")])
        code, build, _ = _run_main(client)
        assert code == 0
        assert not build.called                    # Claude 생성 전에 멈춘다
        assert not client.publish_image_post.called
        assert not client.publish_text_post.called

    def test_budget_rechecked_after_jitter(self, cfg):
        now = dt.datetime.now(dt.UTC)
        client = _main_client()
        client.get_my_posts.side_effect = [[], [_post("a", now), _post("b", now)]]
        code, build, _ = _run_main(client)
        assert code == 0 and build.called
        assert not client.publish_image_post.called

    def test_pct_zero_publishes_without_link_reply(self, cfg):
        client = _main_client()
        code, _, _ = _run_main(client)
        assert code == 0
        client.publish_image_post.assert_called_once()
        assert not client.publish_self_reply.called

    def test_pct_hundred_adds_link_reply(self, cfg):
        cfg(LINK_REPLY_PCT=100)
        client = _main_client()
        code, _, _ = _run_main(client)
        assert code == 0
        client.publish_self_reply.assert_called_once_with("post1", "링크 리플", dry_run=False)

    def test_warmup_regular_still_runs_without_link(self, cfg):
        cfg(WARMUP_UNTIL="2099-12-31", LINK_REPLY_PCT=100)
        client = _main_client()
        code, _, _ = _run_main(client)
        assert code == 0
        client.publish_image_post.assert_called_once()
        assert not client.publish_self_reply.called

    def test_warmup_budget_one(self, cfg):
        cfg(WARMUP_UNTIL="2099-12-31", DAILY_POST_BUDGET=5)
        client = _main_client([_post("a", dt.datetime.now(dt.UTC))])
        code, build, _ = _run_main(client)
        assert code == 0 and not build.called
        assert not client.publish_image_post.called

    @pytest.mark.parametrize("err", [AUTH, BLOCKED, HTTP401])
    def test_fatal_on_image_no_text_fallback(self, cfg, err):
        """S7: 이미지 발행이 계정 사용 불가 오류면 텍스트 폴백(쓰기 재시도)을 하지 않는다."""
        cfg(LINK_REPLY_PCT=100)
        client = _main_client()
        client.publish_image_post.side_effect = err
        code, _, notify = _run_main(client)
        assert code == safety.FATAL_EXIT_CODE == 7
        assert not client.publish_text_post.called
        assert not client.publish_self_reply.called
        notify.assert_called_once()

    def test_non_fatal_image_error_still_falls_back(self, cfg):
        client = _main_client()
        client.publish_image_post.side_effect = SERVER
        code, _, _ = _run_main(client)
        assert code == 0
        client.publish_text_post.assert_called_once()

    def test_fatal_on_link_reply_returns_7(self, cfg):
        cfg(LINK_REPLY_PCT=100)
        client = _main_client()
        client.publish_self_reply.side_effect = AUTH
        code, _, notify = _run_main(client)
        assert code == 7
        notify.assert_called_once()

    def test_tripped_by_swallowed_read_stops_before_generation(self, cfg):
        """get_recent_texts 가 190 을 삼켜도(차단기만 열림) Claude 생성·발행 없이 종료코드 7."""
        client = _main_client()

        def swallowed(_n):
            safety.trip(AUTH)
            return []

        client.get_recent_texts.side_effect = swallowed
        code, build, notify = _run_main(client)
        assert code == 7
        assert not build.called
        assert not client.publish_image_post.called
        notify.assert_called_once()

    def test_link_reply_helper_dry_run(self, cfg):
        from src import main

        cfg(LINK_REPLY_PCT=100)
        client = _main_client()
        assert main._link_reply(client, "p9", "x", dry_run=True) == "reply1"
        cfg(LINK_REPLY_PCT=0)
        assert main._link_reply(client, "p9", "x") is None


# --- CHAT ------------------------------------------------------------------


class FrozenDT(dt.datetime):
    NOW: dt.datetime

    @classmethod
    def now(cls, tz=None):
        return cls.NOW.astimezone(tz) if tz else cls.NOW.replace(tzinfo=None)


def _chat_client(posts=None):
    client = mock.Mock()
    client.get_post_quota.return_value = Quota(used=1, total=250)
    client.get_my_posts.return_value = posts or []
    client.get_recent_texts.return_value = []
    client.publish_text_post.return_value = "chat1"
    return client


def _first_selected():
    trig = chat_plan.selected_triggers(DAY)[0]
    return trig, _kst(DAY, config.CHAT_TRIGGERS[trig - 1])


def _run_chat(client, *, sweep_effect=None, entry="main"):
    from src import ai_writer, mood_source, run_chat

    trig, now = _first_selected()
    os.environ["TRIGGER"] = f"T{trig}"
    os.environ["EVENT_NAME"] = "schedule"
    FrozenDT.NOW = now
    with (
        mock.patch.object(run_chat, "_acquire_token", return_value="tok"),
        mock.patch.object(run_chat, "ThreadsClient", return_value=client) as ctor,
        mock.patch.object(run_chat, "fetch_user_id", return_value=("123", "u")),
        mock.patch("src.threads_client._request",
                   side_effect=AssertionError("실네트워크 호출 금지")),
        mock.patch.object(run_chat.dt, "datetime", FrozenDT),
        mock.patch.object(mood_source, "collect",
                          return_value=mood_source.Mood("web", ("연준",), "관망")) as mood,
        mock.patch.object(ai_writer, "generate",
                          return_value="아침부터 연준 얘기가 많네요. 다들 뭐 보세요?") as gen,
        mock.patch.object(run_chat.antibot, "jitter_sleep", return_value=0),
        mock.patch.object(run_chat.run_reply, "sweep", side_effect=sweep_effect,
                          return_value=0) as sweep,
        mock.patch.object(run_chat, "_notify_safe") as notify,
    ):
        code = run_chat.main() if entry == "main" else run_chat.run()
    return code, {"ctor": ctor, "mood": mood, "gen": gen, "sweep": sweep, "notify": notify}


class TestChatPath:
    def test_kill_switch(self, cfg):
        cfg(AUTOMATION_ENABLED=False)
        client = _chat_client()
        code, m = _run_chat(client)
        assert code == 0
        assert not m["ctor"].called and not m["gen"].called and not m["mood"].called
        assert not m["sweep"].called

    def test_kill_switch_blocks_manual_dry_run_preview(self, cfg):
        cfg(AUTOMATION_ENABLED=False)
        os.environ["DRY_RUN"] = "true"
        from src import run_chat

        with (
            mock.patch.object(run_chat, "_is_manual", return_value=True),
            mock.patch("src.ai_writer.generate", side_effect=AssertionError("Claude 호출 금지")),
        ):
            assert run_chat.run() == 0

    def test_warmup_off(self, cfg):
        cfg(WARMUP_UNTIL="2099-12-31")
        code, m = _run_chat(_chat_client())
        assert code == 0
        assert not m["ctor"].called and not m["gen"].called and not m["sweep"].called

    def test_warmup_expired_publishes(self, cfg):
        cfg(WARMUP_UNTIL="2026-09-01", PUBLISH_SLOTS=("D",))
        client = _chat_client()
        code, _ = _run_chat(client)
        assert code == 0
        client.publish_text_post.assert_called_once()

    def test_budget_allows_first_chat(self, cfg):
        cfg(PUBLISH_SLOTS=("G",))       # 정기 21:43 — 예약 중이지만 0건이면 예산-1=1건 허용
        client = _chat_client()
        code, m = _run_chat(client)
        assert code == 0
        client.publish_text_post.assert_called_once()
        assert m["gen"].called

    def test_budget_reached_no_generation(self, cfg):
        cfg(PUBLISH_SLOTS=("D",), DAILY_POST_BUDGET=1)   # 정기 07:14 이미 나감 → 1건 = 예산
        _, now = _first_selected()
        posts = [_post("r1", _kst(DAY, "07:20"), "IMAGE")]
        assert now > _kst(DAY, "07:20")
        client = _chat_client(posts)
        code, m = _run_chat(client)
        assert code == 0
        assert not client.publish_text_post.called
        assert not m["gen"].called                     # Claude 생성 전에 보류
        assert m["sweep"].called                        # 보류해도 답글 스윕은 기존대로

    def test_reservation_blocks_chat_with_non_chat_post_before_regular(self, cfg):
        """정기 G 21:43 예약 중 · 오늘 비 CHAT 글 1건(예: 수동 글) → 예산 2 에서 CHAT 보류."""
        cfg(PUBLISH_SLOTS=("G",))
        posts = [_post("m1", _kst(DAY, "07:20"), "IMAGE")]
        client = _chat_client(posts)
        code, m = _run_chat(client)
        assert code == 0
        assert not client.publish_text_post.called
        assert not m["gen"].called

    def test_regular_done_releases_for_chat(self, cfg):
        cfg(PUBLISH_SLOTS=("D",))
        posts = [_post("r1", _kst(DAY, "07:20"), "IMAGE")]
        client = _chat_client(posts)
        code, _ = _run_chat(client)
        assert code == 0
        client.publish_text_post.assert_called_once()

    def test_sweep_auth_error_returns_7(self, cfg):
        cfg(PUBLISH_SLOTS=("D",))
        client = _chat_client()
        code, m = _run_chat(client, sweep_effect=AUTH)
        assert code == 7
        m["notify"].assert_called_once()

    def test_safe_sweep_skips_in_warmup(self, cfg):
        from src import run_chat

        cfg(WARMUP_UNTIL="2099-12-31")
        with mock.patch.object(run_chat.run_reply, "sweep") as sweep:
            run_chat._safe_sweep(mock.Mock(), mock.Mock())
        assert not sweep.called


# --- STORY -----------------------------------------------------------------


class TestStoryPath:
    def test_kill_switch(self, cfg):
        from src import notion_source, run_story

        cfg(AUTOMATION_ENABLED=False)
        with mock.patch.object(notion_source, "fetch_new_episodes",
                               side_effect=AssertionError("호출 금지")):
            assert run_story.run() == 0

    def test_warmup(self, cfg):
        from src import notion_source, run_story

        cfg(WARMUP_UNTIL="2099-12-31")
        with mock.patch.object(notion_source, "fetch_new_episodes",
                               side_effect=AssertionError("호출 금지")):
            assert run_story.run() == 0

    def _gate(self, posts, now):
        from src import run_story

        with (
            mock.patch.object(run_story, "_predicted_regular_pillar", return_value="OTHER"),
            mock.patch.object(run_story, "_minutes_to_regular_slot", return_value=None),
            mock.patch.object(run_story, "_story_published_today", return_value=False),
        ):
            return run_story._gate(posts, now, now.astimezone(KST).date(), 100)

    def test_gate_budget(self, cfg):
        now = _kst(DAY, "17:45")
        posts = [_post("c1", _kst(DAY, "09:30"), "TEXT_POST"),
                 _post("r1", _kst(DAY, "12:30"), "IMAGE")]
        assert "도달" in self._gate(posts, now)            # 예산 2, 오늘 2건
        cfg(DAILY_POST_BUDGET=3)
        assert self._gate(posts, now) is None              # 정기 B 창 지남 → 예약 없음
        cfg(PUBLISH_SLOTS=("G",))
        assert "예약" in self._gate(posts, now)            # 정기 G 21:43 예약 중

    def _full_run(self, client):
        from src import antibot, content, notion_source, run_story

        plan = mock.Mock(pillar="STORY", seed="s", source="ai", text="본문", reply_text="링크")
        with (
            mock.patch.object(notion_source, "fetch_new_episodes", return_value=["Ep61"]),
            mock.patch.object(run_story, "_acquire_token", return_value="tok"),
            mock.patch.object(run_story, "fetch_user_id", return_value=("1", "u")),
            mock.patch.object(run_story, "ThreadsClient", return_value=client),
            mock.patch.object(run_story, "_gate", return_value=None),
            mock.patch.object(content, "build_plan", return_value=plan),
            mock.patch.object(run_story, "_select_usable_image",
                              return_value=("https://x/y.png", [])),
            mock.patch.object(antibot, "jitter_sleep", return_value=0),
            mock.patch.object(run_story, "_notify_safe") as notify,
        ):
            os.environ["NOTION_TOKEN"] = "secret_x"
            with mock.patch.object(config, "NOTION_DB_ID", "db"):
                code = run_story.main()
        return code, notify

    def test_link_pct_zero_no_self_reply(self, cfg):
        client = _main_client()
        code, _ = self._full_run(client)
        assert code == 0
        client.publish_image_post.assert_called_once()
        assert not client.publish_self_reply.called

    def test_link_pct_hundred_self_reply(self, cfg):
        cfg(LINK_REPLY_PCT=100)
        client = _main_client()
        code, _ = self._full_run(client)
        assert code == 0
        client.publish_self_reply.assert_called_once()

    def test_fatal_on_image_no_fallback(self, cfg):
        client = _main_client()
        client.publish_image_post.side_effect = BLOCKED
        code, notify = self._full_run(client)
        assert code == 7
        assert not client.publish_text_post.called
        notify.assert_called_once()


# --- 답글 · 이어쓰기 --------------------------------------------------------

NOW = _kst(DAY, "14:00").astimezone(dt.UTC)


def _raw_comment(cid, text, user, *, parent="p1", mine=False, when=None):
    return {"id": cid, "text": text, "username": user,
            "timestamp": _ts(when or (NOW - dt.timedelta(minutes=30))),
            "replied_to": {"id": parent}, "is_reply": True,
            "is_reply_owned_by_me": mine, "hide_status": "NOT_HUSHED"}


class ReplyFake:
    """_request 모의(답글 스윕)."""

    def __init__(self, comments, *, publish_error=None):
        self.comments = comments
        self.publish_error = publish_error
        self.posts_created: list[dict] = []

    def __call__(self, method, url, *, params):
        if url.endswith("/threads_publishing_limit"):
            return {"data": [{"reply_quota_usage": 0, "reply_config": {"quota_total": 1000}}]}
        if url.endswith("/conversation"):
            return {"data": list(self.comments)}
        if url.endswith("/threads") and method == "GET":
            return {"data": [{"id": "p1", "text": "원글",
                              "timestamp": _ts(NOW - dt.timedelta(hours=3))}]}
        if url.endswith("/threads") and method == "POST":
            self.posts_created.append(dict(params))
            if self.publish_error is not None:
                raise self.publish_error
            return {"id": f"c{len(self.posts_created)}"}
        if url.endswith("/threads_publish"):
            return {"id": "r"}
        if "status" in params.get("fields", ""):
            return {"status": "FINISHED"}
        return {}



def _sweep(fake, **kw):
    import itertools

    from src import run_reply
    from src.env import load_settings
    from src.threads_client import ThreadsClient

    texts = itertools.cycle(("좋게 봐주셔서 고맙습니다", "저도 그 부분이 제일 어려웠어요",
                             "말씀 듣고 보니 그렇네요"))
    with (
        mock.patch("src.threads_client._request", side_effect=fake),
        mock.patch("src.threads_client.time.sleep"),
        mock.patch("src.antibot.time.sleep"),
        mock.patch("src.reply_engine._generate_reply", side_effect=lambda *a, **k: next(texts)),
        mock.patch.object(run_reply.notifier, "send") as send,
    ):
        sent = run_reply.sweep(ThreadsClient("1", "tok"), load_settings(),
                               per_run_cap=kw.pop("per_run_cap", 10), dry_run=False, now=NOW, **kw)
    return sent, send


class TestReplyPath:
    COMMENTS = [
        _raw_comment("c1", "This is really great content, thanks", "alice"),
        _raw_comment("c2", "좋은 글 잘 봤습니다 다음 편도 기대돼요", "bob"),
    ]

    def test_sweep_kill_switch_no_api_calls(self, cfg):
        from src import run_reply

        cfg(AUTOMATION_ENABLED=False)
        client = mock.Mock()
        assert run_reply.sweep(client, mock.Mock(), per_run_cap=5, dry_run=False, now=NOW) == 0
        assert not client.method_calls

    def test_sweep_warmup_no_api_calls(self, cfg):
        from src import run_reply

        cfg(WARMUP_UNTIL="2099-12-31")
        client = mock.Mock()
        assert run_reply.sweep(client, mock.Mock(), per_run_cap=5, dry_run=False, now=NOW) == 0
        assert not client.method_calls

    def test_run_kill_switch_before_token(self, cfg):
        from src import run_reply

        cfg(AUTOMATION_ENABLED=False)
        with mock.patch.object(run_reply, "_acquire_token",
                               side_effect=AssertionError("호출 금지")):
            assert run_reply.run() == 0
            assert run_reply.main() == 0

    def test_canned_off_skips_foreign(self, cfg):
        fake = ReplyFake(self.COMMENTS)
        sent, _ = _sweep(fake)
        assert sent == 1
        assert [p["reply_to_id"] for p in fake.posts_created] == ["c2"]

    def test_canned_on_replies_foreign(self, cfg):
        from src import reply_engine

        cfg(REPLY_CANNED_ENABLED=True)
        fake = ReplyFake(self.COMMENTS)
        sent, _ = _sweep(fake)
        assert sent == 2
        by_target = {p["reply_to_id"]: p["text"] for p in fake.posts_created}
        assert by_target["c1"] in reply_engine.NON_KOREAN_REPLIES

    def test_decide_canned_flag(self, cfg):
        from src import reply_engine
        from src.reply_engine import Comment, ReplyStrategy

        c = Comment("c1", "Nice post, very helpful", "alice", "", "p1", False, "NOT_HUSHED")
        d = reply_engine.decide(c, already_replied=False, author_used=0)
        assert d.strategy is ReplyStrategy.SKIP and "정형 문구 비활성" in d.reason
        cfg(REPLY_CANNED_ENABLED=True)
        assert reply_engine.decide(c, already_replied=False, author_used=0).strategy \
            is ReplyStrategy.NON_KOREAN

    def test_new_author_cap_default_one(self, cfg):
        """S4: 저자 일일 캡 1 — 같은 사람의 두 번째 댓글은 같은 날 답하지 않는다."""
        comments = [
            _raw_comment("c1", "좋은 글 잘 봤습니다 다음 편도 기대돼요", "bob"),
            _raw_comment("c2", "그리고 질문이 하나 더 있는데 괜찮을까요", "bob"),
        ]
        with mock.patch.object(config, "REPLY_AUTHOR_DAILY_CAP", 1):
            sent, _ = _sweep(ReplyFake(comments))
        assert sent == 1

    @pytest.mark.parametrize("err", [AUTH, BLOCKED])
    def test_fatal_stops_after_first_write(self, cfg, err):
        comments = [
            _raw_comment("c1", "좋은 글 잘 봤습니다 다음 편도 기대돼요", "bob"),
            _raw_comment("c2", "저도 비슷하게 생각했어요 정리 감사합니다", "carol"),
        ]
        fake = ReplyFake(comments, publish_error=err)
        with pytest.raises(ThreadsApiError):
            _sweep(fake)
        assert len(fake.posts_created) == 1        # 다음 답글을 시도하지 않는다

    def test_non_fatal_continues(self, cfg):
        comments = [
            _raw_comment("c1", "좋은 글 잘 봤습니다 다음 편도 기대돼요", "bob"),
            _raw_comment("c2", "저도 비슷하게 생각했어요 정리 감사합니다", "carol"),
        ]
        fake = ReplyFake(comments, publish_error=SERVER)
        sent, send = _sweep(fake)
        assert sent == 0 and len(fake.posts_created) == 2
        assert send.call_count == 2              # 건별 실패 알림(기존 동작)

    def test_run_reply_main_fatal_http_level(self, cfg):
        """HTTP 수준: 첫 답글 컨테이너가 190 → POST 1건으로 끝, 알림 1회, 종료코드 7."""
        from src import run_reply

        comments = [
            _raw_comment("c1", "좋은 글 잘 봤습니다 다음 편도 기대돼요", "bob",
                         when=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30)),
            _raw_comment("c2", "저도 비슷하게 생각했어요 정리 감사합니다", "carol",
                         when=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=20)),
        ]
        posts_seen = []

        def fake(method, url, **kw):
            params = kw.get("params") or {}
            if url.endswith("/me"):
                return _Resp(200, {"id": "38808359068777315", "username": "u"})
            if url.endswith("/threads_publishing_limit"):
                return _Resp(200, {"data": [{"reply_quota_usage": 0,
                                             "reply_config": {"quota_total": 1000}}]})
            if url.endswith("/conversation"):
                return _Resp(200, {"data": comments})
            if url.endswith("/threads") and method == "GET":
                return _Resp(200, {"data": [{
                    "id": "p1", "text": "원글",
                    "timestamp": _ts(dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))}]})
            if method == "POST":
                posts_seen.append(params.get("reply_to_id"))
                return _Resp(400, {"error": {"code": 190, "message": "Session has expired"}})
            return _Resp(200, {})

        texts = iter(("좋게 봐주셔서 고맙습니다", "저도 그 부분이 제일 어려웠어요"))
        with (
            mock.patch("src.threads_client.requests.request", side_effect=fake),
            mock.patch("src.threads_client.time.sleep"),
            mock.patch("src.antibot.time.sleep"),
            mock.patch("src.reply_engine._generate_reply", side_effect=lambda *a, **k: next(texts)),
            mock.patch.object(run_reply, "_notify_safe") as notify,
            mock.patch.object(run_reply.notifier, "send") as send,
        ):
            code = run_reply.main()
        assert code == 7
        assert len(posts_seen) == 1
        notify.assert_called_once()
        assert not send.called                  # 건별 실패 알림은 나가지 않는다(알림 1회)


class TestFollowupsAndNoLinkReply:
    def test_followups_allowed_matrix(self, cfg):
        cfg(FOLLOWUP_ENABLED=False)
        assert not safety.followups_allowed(DAY)
        cfg(FOLLOWUP_ENABLED=True)
        assert safety.followups_allowed(DAY)
        cfg(WARMUP_UNTIL="2099-12-31")
        assert not safety.followups_allowed(DAY)
        cfg(WARMUP_UNTIL="", AUTOMATION_ENABLED=False)
        assert not safety.followups_allowed(DAY)

    def test_sweep_does_not_call_followups_when_blocked(self, cfg):
        from src import run_reply

        cfg(FOLLOWUP_ENABLED=True)
        with (
            mock.patch.object(run_reply, "_followups") as fu,
            mock.patch.object(safety, "followups_allowed", return_value=False),
        ):
            _sweep(ReplyFake([]), allow_followup=True)
        assert not fu.called

    def test_sweep_calls_followups_when_allowed(self, cfg):
        from src import run_reply

        cfg(FOLLOWUP_ENABLED=True)
        with mock.patch.object(run_reply, "_followups", return_value=0) as fu:
            _sweep(ReplyFake([]), allow_followup=True)
        assert fu.called

    def _convs(self, *, with_link: bool, with_followup: bool):
        from src.run_reply import _parse_comment

        raw = [_raw_comment("c1", "좋은 글이네요 잘 봤습니다", "bob"),
               _raw_comment("m1", "감사합니다", "me", parent="c1", mine=True)]
        if with_link:
            raw.append(_raw_comment("L", "매일 올리는 곳입니다.\nhttps://youtube.com/@x", "me",
                                    mine=True))
        if with_followup:
            raw.append(_raw_comment("F", "덧붙이면 그날은 좀 달랐어요", "me", mine=True))
        return {"p1": [_parse_comment(r) for r in raw]}

    @pytest.mark.parametrize("with_link", [True, False])
    def test_ledger_same_with_or_without_link_reply(self, with_link):
        from src import run_reply

        convs = self._convs(with_link=with_link, with_followup=False)
        ledger = run_reply.build_ledger(convs, {"p1"}, NOW.astimezone(KST).date())
        assert ledger.used_today == 1
        assert ledger.author_today["bob"] == 1

    @pytest.mark.parametrize("with_link", [True, False])
    def test_followup_detection_without_link_reply(self, with_link):
        from src import run_reply

        today = NOW.astimezone(KST).date()
        none = self._convs(with_link=with_link, with_followup=False)
        assert run_reply.followup_count_today(none, today) == 0
        assert run_reply._my_followups("p1", none["p1"]) == []
        one = self._convs(with_link=with_link, with_followup=True)
        assert run_reply.followup_count_today(one, today) == 1

    def test_candidates_when_no_link_reply(self, cfg):
        from src import run_reply

        convs = self._convs(with_link=False, with_followup=False)
        posts = [{"id": "p1", "text": "원글", "timestamp": _ts(NOW - dt.timedelta(hours=3))}]
        with mock.patch.object(run_reply, "is_followup_target", return_value=True):
            assert run_reply.followup_candidates(posts, convs, NOW) == [("p1", "원글")]

    def test_prior_reply_texts_without_link(self):
        from src import reply_engine

        assert reply_engine.prior_reply_texts(["감사합니다", "덧붙이면"]) == ["감사합니다", "덧붙이면"]


# ===========================================================================
# C. 읽기 경로 — 워치독 · golive_check
# ===========================================================================


def _iso(hours_ago: float) -> str:
    return _ts(dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago))


class WatchFake:
    def __init__(self, *, posts=None, replies=None):
        self.posts = posts if posts is not None else []
        self.replies = replies if replies is not None else []
        self.methods: list[str] = []

    def __call__(self, method, url, *, params):
        self.methods.append(method)
        if url.endswith("/me"):
            return {"id": "999", "username": "u"}
        if url.endswith("/threads_publishing_limit"):
            return {"data": [{"quota_usage": 1, "config": {"quota_total": 250}}]}
        if url.endswith("/threads"):
            return {"data": self.posts}
        if url.endswith("/conversation"):
            return {"data": self.replies}
        return {}


def _watch(fake):
    from src import run_watchdog

    os.environ.update({"TELEGRAM_BOT_TOKEN": "bot", "TELEGRAM_ALERT_CHAT_ID": "chat"})
    with (
        mock.patch("src.threads_client._request", side_effect=fake),
        mock.patch.object(run_watchdog.notifier, "send") as send,
        mock.patch.object(run_watchdog.chat_plan, "is_chat_time", return_value=False),
        mock.patch.object(run_watchdog.watchdog, "build_report",
                          wraps=run_watchdog.watchdog.build_report) as report,
    ):
        code = run_watchdog.run()
    findings = report.call_args.args[0]
    return code, send, {f.title: f for f in findings}


class TestWatchdogNoFalseAlarm:
    def test_disabled_no_posts_is_silent(self, cfg):
        cfg(AUTOMATION_ENABLED=False)
        fake = WatchFake(posts=[])
        code, send, found = _watch(fake)
        assert code == 0 and not send.called
        assert "AUTOMATION_ENABLED=false" in found["발행 판정 생략"].detail
        assert "AUTOMATION_ENABLED=false" in found["CHAT 비활성"].detail
        assert "AUTOMATION_ENABLED=false" in found["답글 비활성"].detail
        assert set(fake.methods) == {"GET"}          # 읽기 전용

    def test_disabled_stale_posts_is_silent(self, cfg):
        cfg(AUTOMATION_ENABLED=False)
        code, send, _ = _watch(WatchFake(posts=[{"id": "p1", "timestamp": _iso(24 * 30)}]))
        assert code == 0 and not send.called

    def test_warmup_no_posts_is_silent(self, cfg):
        cfg(WARMUP_UNTIL="2099-12-31")
        code, send, found = _watch(WatchFake(posts=[]))
        assert not send.called
        assert found["발행 이력 없음(워밍업)"].severity == "ok"
        assert "워밍업" in found["CHAT 비활성"].detail
        assert "워밍업" in found["답글 비활성"].detail

    def test_warmup_stale_regular_still_alerts(self, cfg):
        """워밍업 중에도 정기 발행은 매일 돈다 — 진짜 공백은 계속 잡는다."""
        cfg(WARMUP_UNTIL="2099-12-31")
        code, send, found = _watch(WatchFake(posts=[{"id": "p1", "timestamp": _iso(100)}]))
        assert send.called
        assert found["발행 장기 중단"].severity == "critical"

    def test_enabled_no_posts_still_critical(self, cfg):
        code, send, found = _watch(WatchFake(posts=[]))
        assert send.called
        assert found["발행 이력 없음"].severity == "critical"

    def test_budget_zero_no_publish_alarm(self, cfg):
        cfg(DAILY_POST_BUDGET=0)
        code, send, found = _watch(WatchFake(posts=[]))
        assert not send.called
        assert "DAILY_POST_BUDGET=0" in found["발행 판정 생략"].detail

    def test_chat_reason_budget_limited(self, cfg):
        from src import run_watchdog

        reason = run_watchdog.chat_watch_reason(DAY)
        assert reason and "DAILY_POST_BUDGET" in reason
        cfg(DAILY_POST_BUDGET=1000)
        assert run_watchdog.chat_watch_reason(DAY) is None
        cfg(CHAT_ENABLED=False)
        assert "CHAT_ENABLED=false" in run_watchdog.chat_watch_reason(DAY)

    def test_chat_check_not_alarming_with_default_budget(self, cfg):
        from src import watchdog

        reason = __import__("src.run_watchdog", fromlist=["x"]).chat_watch_reason(DAY)
        f = watchdog.check_chat_activity(0, _kst(DAY, "21:37"), enabled=reason is None,
                                         disabled_reason=reason or "")
        assert f.severity == "ok"

    def test_blocked_api_still_critical_when_disabled(self, cfg):
        """킬 스위치 중에도 차단 감지는 남긴다(읽기 오류는 감시 대상)."""
        cfg(AUTOMATION_ENABLED=False)

        def fake(method, url, *, params):
            raise BLOCKED

        code, send, found = _watch(fake)
        assert code == 0 and send.called
        assert found["Threads API 접근 실패"].severity == "critical"


class TestGoliveSafety:
    def _rows(self):
        import golive_check as gl

        r = gl.Report()
        gl.check_safety(r)
        return [row for row in r.rows if row[0] == "C13"]

    def test_disabled_rows(self, cfg):
        cfg(AUTOMATION_ENABLED=False)
        rows = self._rows()
        names = {row[1] for row in rows}
        assert {"AUTOMATION_ENABLED", "WARMUP_UNTIL", "DAILY_POST_BUDGET", "LINK_REPLY_PCT",
                "답글 캡", "REPLY_CANNED_ENABLED", "안전 프로필"} <= names
        assert all(row[2] == "OK" for row in rows)

    def test_over_profile_warns_not_fails(self, cfg):
        cfg(LINK_REPLY_PCT=20)
        rows = self._rows()
        warn = [row for row in rows if row[1] == "안전 프로필"]
        assert warn[0][2] == "WARN" and "LINK_REPLY_PCT=20" in warn[0][3]
        assert not any(row[2] == "FAIL" for row in rows)

    def test_invalid_warmup_warns(self, cfg):
        cfg(WARMUP_UNTIL="soon")
        rows = {row[1]: row for row in self._rows()}
        assert rows["WARMUP_UNTIL"][2] == "WARN"

    def test_active_warmup_shown(self, cfg):
        cfg(WARMUP_UNTIL="2099-12-31", DAILY_POST_BUDGET=2)
        rows = {row[1]: row for row in self._rows()}
        assert "워밍업 중" in rows["WARMUP_UNTIL"][3]
        assert "적용 1" in rows["DAILY_POST_BUDGET"][3]


# ===========================================================================
# D. 워크플로 env 일치
# ===========================================================================


def _step_env(name: str) -> dict:
    data = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    merged: dict = {}
    for job in data["jobs"].values():
        for step in job.get("steps") or []:
            merged.update(step.get("env") or {})
    return merged


class TestWorkflowEnv:
    def test_verify_repo_check_passes(self):
        import verify_repo

        assert verify_repo.check_safety_env_consistency() == 0

    @pytest.mark.parametrize("name", sorted(
        __import__("verify_repo").SAFETY_WORKFLOW_KEYS))
    def test_required_keys_with_canonical_defaults(self, name):
        import verify_repo

        env = _step_env(name)
        for key in verify_repo.SAFETY_WORKFLOW_KEYS[name]:
            assert key in env, (name, key)
            assert str(env[key]).strip() == verify_repo.safety_expr(
                key, config.SAFETY_VARIABLE_DEFAULTS[key]), (name, key)

    def test_write_workflows_pass_kill_switch(self):
        for name in ("publish.yml", "chat.yml", "story.yml", "reply.yml"):
            env = _step_env(name)
            assert env["AUTOMATION_ENABLED"] == "${{ vars.AUTOMATION_ENABLED || 'false' }}"
            assert env["WARMUP_UNTIL"] == "${{ vars.WARMUP_UNTIL }}"

    def test_no_stale_cap_defaults_anywhere(self):
        old = ("'40'", "|| '6'")
        for path in WF.glob("*.yml"):
            for key, value in _step_env(path.name).items():
                if key in ("REPLY_DAILY_CAP", "REPLY_SCHEDULED_RUN_CAP"):
                    assert not any(o in str(value) for o in old), (path.name, key, value)

    def test_verify_repo_detects_wrong_default(self):
        import verify_repo

        real = verify_repo._load_yaml

        def tampered(name):
            data = real(name)
            if name == "publish.yml":
                for job in data["jobs"].values():
                    for step in job.get("steps") or []:
                        if "AUTOMATION_ENABLED" in (step.get("env") or {}):
                            step["env"]["AUTOMATION_ENABLED"] = \
                                "${{ vars.AUTOMATION_ENABLED || 'true' }}"
            return data

        with mock.patch.object(verify_repo, "_load_yaml", side_effect=tampered):
            assert verify_repo.check_safety_env_consistency() >= 1

    def test_verify_repo_detects_missing_key(self):
        import verify_repo

        real = verify_repo._load_yaml

        def tampered(name):
            data = real(name)
            if name == "reply.yml":
                for job in data["jobs"].values():
                    for step in job.get("steps") or []:
                        (step.get("env") or {}).pop("REPLY_CANNED_ENABLED", None)
            return data

        with mock.patch.object(verify_repo, "_load_yaml", side_effect=tampered):
            assert verify_repo.check_safety_env_consistency() >= 1

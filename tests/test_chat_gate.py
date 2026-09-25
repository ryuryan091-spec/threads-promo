"""CHAT 발행 계획·게이트·run_chat 통합 테스트."""

from __future__ import annotations

import datetime as dt
import os
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from src import chat_plan, config, watchdog

KST = ZoneInfo("Asia/Seoul")
DAY = dt.date(2026, 9, 21)


def _kst(hh: int, mm: int, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=KST)


def _post(when: dt.datetime, pid: str = "p") -> dict:
    return {"id": pid, "text": "글",
            "timestamp": when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")}


def _counts(posts: list[dict], now: dt.datetime) -> chat_plan.PostCounts:
    return chat_plan.count_posts(posts, now, watchdog.parse_threads_timestamp)


# ---------------------------------------------------------------------------
# 창 · 트리거
# ---------------------------------------------------------------------------


class TestWindow:
    @pytest.mark.parametrize("hh,mm,expected", [
        (8, 59, False), (9, 0, True), (10, 30, True), (12, 4, True), (12, 5, False),
    ])
    def test_boundaries(self, hh, mm, expected):
        assert chat_plan.is_chat_time(_kst(hh, mm)) is expected

    def test_utc_input_converted(self):
        # UTC 01:00 = KST 10:00
        assert chat_plan.is_chat_time(dt.datetime(2026, 9, 21, 1, 0, tzinfo=dt.UTC))

    def test_regular_and_event_slots_outside_window(self):
        """정기(08:23+8분, 12:47+8분)·이벤트(+55분) 창이 CHAT 창과 겹치지 않는다."""
        from src import insights

        for hhmm in insights.PUBLISH_SLOTS:
            start = _kst(int(hhmm[:2]), int(hhmm[3:]))
            for m in range(0, insights.PUBLISH_WINDOW_MIN + 1):
                assert not chat_plan.is_chat_time(start + dt.timedelta(minutes=m)), hhmm
        for hhmm in insights.EVENT_SLOTS:
            start = _kst(int(hhmm[:2]), int(hhmm[3:]))
            for m in range(0, insights.EVENT_WINDOW_MIN + 1):
                assert not chat_plan.is_chat_time(start + dt.timedelta(minutes=m)), hhmm

    def test_all_triggers_inside_window(self):
        for hhmm in config.CHAT_TRIGGERS:
            assert chat_plan.is_chat_time(_kst(int(hhmm[:2]), int(hhmm[3:]))), hhmm

    def test_trigger_gaps_not_round(self):
        for hhmm in config.CHAT_TRIGGERS:
            assert hhmm[3:] not in ("00", "30"), hhmm

    @pytest.mark.parametrize("raw,expected", [
        ("T1", 1), ("t9", 9), ("T0", None), ("T10", None), ("MANUAL", None), ("", None),
    ])
    def test_trigger_number(self, raw, expected):
        assert chat_plan.trigger_number(raw) == expected


# ---------------------------------------------------------------------------
# 일일 계획
# ---------------------------------------------------------------------------


class TestDailyPlan:
    def test_target_within_bounds_over_year(self):
        for offset in range(365):
            day = DAY + dt.timedelta(days=offset)
            low, high = chat_plan.daily_bounds(day)   # v1.1.0: 주말은 별도 범위
            n = chat_plan.daily_target(day)
            assert low <= n <= high

    def test_deterministic(self):
        assert chat_plan.daily_target(DAY) == chat_plan.daily_target(DAY)
        assert chat_plan.selected_triggers(DAY) == chat_plan.selected_triggers(DAY)

    def test_selected_count_matches_target(self):
        for offset in range(60):
            day = DAY + dt.timedelta(days=offset)
            picked = chat_plan.selected_triggers(day)
            assert len(picked) == chat_plan.daily_target(day)
            assert len(set(picked)) == len(picked)
            assert all(1 <= t <= len(config.CHAT_TRIGGERS) for t in picked)

    def test_selection_varies_by_day(self):
        patterns = {chat_plan.selected_triggers(DAY + dt.timedelta(days=d)) for d in range(30)}
        assert len(patterns) > 5

    def test_bad_config_clamped(self):
        with (
            mock.patch.object(config, "CHAT_DAILY_MIN", 50),
            mock.patch.object(config, "CHAT_DAILY_MAX", 99),
        ):
            assert chat_plan.daily_bounds() == (9, 9)
        with (
            mock.patch.object(config, "CHAT_DAILY_MIN", 5),
            mock.patch.object(config, "CHAT_DAILY_MAX", 2),
        ):
            low, high = chat_plan.daily_bounds()
            assert low <= high

    def test_source_mix_uses_both(self):
        seen = {chat_plan.source_for(DAY, t, "mix") for t in range(1, 10)}
        seen |= {chat_plan.source_for(DAY + dt.timedelta(days=1), t, "mix") for t in range(1, 10)}
        assert seen == {"rss", "web"}

    @pytest.mark.parametrize("mode", ["rss", "web", "none"])
    def test_source_fixed_modes(self, mode):
        assert chat_plan.source_for(DAY, 3, mode) == mode


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------


def _first_selected(day: dt.date) -> int:
    return chat_plan.selected_triggers(day)[0]


def _first_unselected(day: dt.date) -> int | None:
    picked = set(chat_plan.selected_triggers(day))
    for t in range(1, len(config.CHAT_TRIGGERS) + 1):
        if t not in picked:
            return t
    return None


class TestGate:
    def test_passes_first_selected_trigger(self):
        now = _kst(10, 0)
        trig = _first_selected(DAY)
        assert chat_plan.gate(DAY, now, trig, _counts([], now), manual=False) is None

    def test_blocks_unselected_trigger_when_on_schedule(self):
        """미선택 트리거는 계획대로 나가 있으면(허용 건수 도달) 보류한다."""
        trig = _first_unselected(DAY)
        if trig is None:
            pytest.skip("이 날은 전 트리거 선택")
        allowed = sum(1 for t in chat_plan.selected_triggers(DAY) if t <= trig)
        now = _kst(11, 58)
        posts = [_post(_kst(9, 5) + dt.timedelta(minutes=16 * i), f"p{i}") for i in range(allowed)]
        got = chat_plan.gate(DAY, now, trig, _counts(posts, now), manual=False)
        assert got and "미선택" in got

    def test_unselected_trigger_catches_up_deficit(self):
        """v1.0.1: 앞 cron 누락으로 밀려 있으면 미선택 트리거도 1건 보충한다."""
        selected = chat_plan.selected_triggers(DAY)
        later_unselected = [t for t in range(selected[0] + 1, 10) if t not in selected]
        if not later_unselected:
            pytest.skip("첫 선택 이후 미선택 트리거 없음")
        trig = later_unselected[0]
        now = _kst(11, 58)
        assert chat_plan.gate(DAY, now, trig, _counts([], now), manual=False) is None

    def test_unselected_before_any_selected_blocked(self):
        day = next(DAY + dt.timedelta(days=d) for d in range(60)
                   if chat_plan.selected_triggers(DAY + dt.timedelta(days=d))[0] > 1)
        now = _kst(9, 4, day)
        got = chat_plan.gate(day, now, 1, _counts([], now), manual=False)
        assert got and "허용 0건" in got

    def test_gap_wait_and_jitter_range(self):
        now = _kst(10, 0)
        recent = _counts([_post(now - dt.timedelta(minutes=10))], now)
        wait = chat_plan.gap_wait_seconds(recent, now)
        assert wait == 5 * 60
        low, high = chat_plan.jitter_range(wait)
        assert low >= wait + 30 and high > low
        assert chat_plan.jitter_range(0) == config.CHAT_JITTER
        assert chat_plan.gap_wait_seconds(_counts([], now), now) == 0

    def test_gap_not_enforced_in_pre_check(self):
        now = _kst(10, 0)
        trig = _first_selected(DAY)
        recent = _counts([_post(now - dt.timedelta(minutes=5))], now)
        assert chat_plan.gate(DAY, now, trig, recent, manual=True, enforce_gap=False) is None

    def test_rerun_same_trigger_is_idempotent(self):
        """같은 트리거 재실행 — 이미 1건 발행돼 있으면 스킵."""
        now = _kst(10, 0)
        trig = _first_selected(DAY)
        posts = [_post(_kst(9, 50))]
        got = chat_plan.gate(DAY, now, trig, _counts(posts, now), manual=False)
        assert got and "이미" in got

    def test_missed_trigger_catches_up_only_one(self):
        """앞 트리거 누락 시 다음 선택 트리거에서 1건만 보충된다."""
        picked = chat_plan.selected_triggers(DAY)
        if len(picked) < 3:
            pytest.skip("선택 트리거 부족")
        third = picked[2]
        now = _kst(11, 0)
        # 0건 → 허용(보충 1건)
        assert chat_plan.gate(DAY, now, third, _counts([], now), manual=False) is None
        # 1건 발행 후 같은 트리거 재실행 → 허용 한도(3) 미만이지만 다음 실행에서
        # 또 1건이다. 한 실행은 최대 1건만 내므로 몰아 내지 않는다.
        posts = [_post(_kst(10, 40))]
        assert chat_plan.gate(DAY, now, third, _counts(posts, now), manual=False) is None

    def test_blocks_at_daily_target(self):
        now = _kst(11, 55)
        target = chat_plan.daily_target(DAY)
        posts = [_post(_kst(9, 1 + i * 5), f"p{i}") for i in range(target)]
        got = chat_plan.gate(DAY, now, 9, _counts(posts, now), manual=True)
        assert got and "목표" in got

    def test_blocks_min_gap(self):
        now = _kst(10, 0)
        trig = _first_selected(DAY)
        posts = [_post(_kst(8, 50))]      # 정기 글(창 밖)이라 CHAT 수에는 안 잡힘
        posts_recent = [_post(now - dt.timedelta(minutes=5))]
        assert chat_plan.gate(DAY, now, trig, _counts(posts, now), manual=False) is None
        got = chat_plan.gate(DAY, now, trig, _counts(posts_recent, now), manual=True)
        assert got and "최소 간격" in got

    def test_blocks_outside_window(self):
        now = _kst(12, 10)
        got = chat_plan.gate(DAY, now, 9, _counts([], now), manual=True)
        assert got and "창" in got

    def test_unknown_trigger_blocked_on_schedule(self):
        now = _kst(10, 0)
        got = chat_plan.gate(DAY, now, None, _counts([], now), manual=False)
        assert got and "트리거" in got

    def test_manual_skips_selection(self):
        now = _kst(10, 0)
        assert chat_plan.gate(DAY, now, None, _counts([], now), manual=True) is None

    def test_yesterday_chat_not_counted(self):
        now = _kst(10, 0)
        yesterday = DAY - dt.timedelta(days=1)
        posts = [_post(_kst(9, 30, yesterday), f"y{i}") for i in range(8)]
        assert _counts(posts, now).chat_today == 0


# ---------------------------------------------------------------------------
# run_chat 통합
# ---------------------------------------------------------------------------

BASE_ENV = {
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "CLAUDE_AI_KEY": "sk-ant-test",
    "DRY_RUN": "false",
    "EVENT_NAME": "schedule",
}


@pytest.fixture
def chat_env():
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("THREADS_", "CLAUDE_", "DRY_RUN", "EVENT_", "TRIGGER")):
            os.environ.pop(key, None)
    os.environ.update(BASE_ENV)
    with mock.patch.object(config, "CHAT_ENABLED", True):
        yield
    os.environ.clear()
    os.environ.update(saved)


def _run_chat(client, *, now: dt.datetime, trigger: str, text: str = "금리 얘기가 많은 아침이네요.\n다들 오늘 뭐부터 보세요?"):
    from src import ai_writer, mood_source, run_chat

    os.environ["TRIGGER"] = trigger

    class FrozenDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    with (
        mock.patch.object(run_chat, "_acquire_token", return_value="tok"),
        mock.patch.object(run_chat, "ThreadsClient", return_value=client),
        mock.patch.object(run_chat, "fetch_user_id", return_value=("1", "u")),
        mock.patch.object(run_chat.dt, "datetime", FrozenDT),
        mock.patch.object(mood_source, "collect",
                          return_value=mood_source.Mood("web", ("금리",), "관망")),
        mock.patch.object(ai_writer, "generate", return_value=text) as gen,
        mock.patch.object(run_chat.antibot, "jitter_sleep", return_value=0),
        mock.patch.object(run_chat.run_reply, "sweep", return_value=0) as sweep,
    ):
        code = run_chat.run()
    return code, gen, sweep


def _client(posts: list[dict]):
    from src.threads_client import Quota

    client = mock.Mock()
    client.get_post_quota.return_value = Quota(used=1, total=250)
    client.get_my_posts.return_value = posts
    client.get_recent_texts.return_value = []
    client.publish_text_post.return_value = "chat1"
    return client


class TestRunChat:
    def test_publishes_on_selected_trigger(self, chat_env):
        trig = _first_selected(DAY)
        hhmm = config.CHAT_TRIGGERS[trig - 1]
        now = _kst(int(hhmm[:2]), int(hhmm[3:]))
        client = _client([_post(_kst(8, 25))])
        code, gen, sweep = _run_chat(client, now=now, trigger=f"T{trig}")
        assert code == 0
        client.publish_text_post.assert_called_once()
        assert not client.publish_self_reply.called   # CHAT 은 링크 셀프리플 없음
        assert not client.publish_image_post.called
        assert gen.call_args.args[1] == "CHAT"
        assert "금리" in gen.call_args.kwargs["facts_block"]
        sweep.assert_called_once()

    def test_unselected_trigger_only_sweeps(self, chat_env):
        """계획대로 나가 있는 상태의 미선택 트리거 → 발행 없이 답글 스윕만."""
        trig = _first_unselected(DAY)
        if trig is None:
            pytest.skip("전 트리거 선택")
        hhmm = config.CHAT_TRIGGERS[trig - 1]
        now = _kst(int(hhmm[:2]), int(hhmm[3:]))
        allowed = sum(1 for t in chat_plan.selected_triggers(DAY) if t <= trig)
        posts = [_post(_kst(9, 1) + dt.timedelta(minutes=i), f"p{i}") for i in range(allowed)]
        client = _client(posts)
        code, gen, sweep = _run_chat(client, now=now, trigger=f"T{trig}")
        assert code == 0
        assert not client.publish_text_post.called
        assert not gen.called
        sweep.assert_called_once()

    def test_waits_for_gap_instead_of_dropping(self, chat_env):
        """v1.0.1: 직전 글과 간격이 모자라면 버리지 않고 기다렸다 발행한다."""
        from src import run_chat

        trig = _first_selected(DAY)
        hhmm = config.CHAT_TRIGGERS[trig - 1]
        now = _kst(int(hhmm[:2]), int(hhmm[3:]))
        client = _client([_post(now - dt.timedelta(minutes=8))])
        # 재검증 시점에는 간격이 충족된 것으로 본다(지터 동안 시간이 흐름).
        client.get_my_posts.side_effect = [
            [_post(now - dt.timedelta(minutes=8))],
            [_post(now - dt.timedelta(minutes=16))],
        ]
        with mock.patch.object(run_chat.chat_plan, "jitter_range",
                               wraps=chat_plan.jitter_range) as jr:
            code, _, _ = _run_chat(client, now=now, trigger=f"T{trig}")
        assert code == 0
        client.publish_text_post.assert_called_once()
        assert jr.call_args.args[0] == 7 * 60     # 15분 - 8분 = 7분 대기 요청

    def test_gap_wait_over_limit_skips(self, chat_env):
        trig = _first_selected(DAY)
        hhmm = config.CHAT_TRIGGERS[trig - 1]
        now = _kst(int(hhmm[:2]), int(hhmm[3:]))
        client = _client([_post(now)])       # 방금 글 → 대기 15분 > 한도 10분
        with mock.patch.object(config, "CHAT_MAX_GAP_WAIT_SEC", 600):
            code, gen, sweep = _run_chat(client, now=now, trigger=f"T{trig}")
        assert code == 0
        assert not client.publish_text_post.called
        sweep.assert_called_once()

    def test_lint_failure_skips_publish(self, chat_env):
        trig = _first_selected(DAY)
        hhmm = config.CHAT_TRIGGERS[trig - 1]
        now = _kst(int(hhmm[:2]), int(hhmm[3:]))
        client = _client([])
        code, gen, _ = _run_chat(client, now=now, trigger=f"T{trig}",
                                 text="금리가 3번 오를 것 같네요?")
        assert code == 0
        assert not client.publish_text_post.called
        assert gen.call_count == config.AI_MAX_RETRY

    def test_disabled_does_nothing(self, chat_env):
        client = _client([])
        with mock.patch.object(config, "CHAT_ENABLED", False):
            code, gen, sweep = _run_chat(client, now=_kst(10, 7), trigger="T4")
        assert code == 0
        assert not client.publish_text_post.called
        assert not sweep.called

    def test_dry_run_never_publishes(self, chat_env):
        os.environ["DRY_RUN"] = "true"
        client = _client([])
        code, gen, _ = _run_chat(client, now=_kst(10, 7), trigger="T4")
        assert code == 0
        assert gen.called                      # 미리보기는 생성한다
        assert not client.publish_text_post.called

    def test_recheck_after_jitter_blocks(self, chat_env):
        trig = _first_selected(DAY)
        hhmm = config.CHAT_TRIGGERS[trig - 1]
        now = _kst(int(hhmm[:2]), int(hhmm[3:]))
        client = _client([])
        # 지터 후 재조회에서 방금 CHAT 이 생김(다른 실행) → 보류
        client.get_my_posts.side_effect = [[], [_post(now - dt.timedelta(minutes=1))]]
        code, _, _ = _run_chat(client, now=now, trigger=f"T{trig}")
        assert code == 0
        assert not client.publish_text_post.called

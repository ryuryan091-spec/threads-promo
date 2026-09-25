"""전수 테스트 보강 — CHAT·답글 스윕의 장애 경로.

정상 경로는 test_chat_gate / test_reply_stateless_caps 가 본다.
여기서는 '부가 기능 실패가 본 기능을 막지 않는다(NFR-01)'와
'접근 차단은 즉시 멈춘다(NFR-04)'를 CHAT·스윕 경로에서 검증한다.
"""

from __future__ import annotations

import datetime as dt
import os
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from src import chat_plan, config
from src.threads_client import Quota, ThreadsApiError

KST = ZoneInfo("Asia/Seoul")
DAY = dt.date(2026, 9, 21)

BASE_ENV = {
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "CLAUDE_AI_KEY": "sk-ant-test",
    "DRY_RUN": "false",
    "EVENT_NAME": "schedule",
}

BLOCKED = ThreadsApiError(400, '{"error":{"code":200,"message":"access blocked"}}', code=200)
AUTH = ThreadsApiError(400, '{"error":{"code":190}}', code=190)
SERVER = ThreadsApiError(500, "server error", code=1)


@pytest.fixture(autouse=True)
def _env():
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("THREADS_", "CLAUDE_", "DRY_RUN", "EVENT_", "TRIGGER", "SLOT",
                           "REPLY_SLOTS")):
            os.environ.pop(key, None)
    os.environ.update(BASE_ENV)
    with mock.patch.object(config, "CHAT_ENABLED", True):
        yield
    os.environ.clear()
    os.environ.update(saved)


def _selected_now() -> tuple[str, dt.datetime]:
    trig = chat_plan.selected_triggers(DAY)[0]
    hh, mm = map(int, config.CHAT_TRIGGERS[trig - 1].split(":"))
    return f"T{trig}", dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=KST)


class FrozenDT(dt.datetime):
    NOW: dt.datetime

    @classmethod
    def now(cls, tz=None):
        return cls.NOW.astimezone(tz) if tz else cls.NOW.replace(tzinfo=None)


def _client(**kw):
    client = mock.Mock()
    client.get_post_quota.return_value = kw.get("quota", Quota(used=1, total=250))
    client.get_my_posts.return_value = kw.get("posts", [])
    client.get_recent_texts.return_value = []
    client.publish_text_post.return_value = "chat1"
    return client


def _run(client, *, sweep_effect=None, generate="아침부터 연준 얘기가 많네요. 다들 뭐 보세요?",
         entry="main"):
    from src import ai_writer, mood_source, run_chat

    trigger, now = _selected_now()
    os.environ["TRIGGER"] = trigger
    FrozenDT.NOW = now
    with (
        mock.patch.object(run_chat, "_acquire_token", return_value="tok"),
        mock.patch.object(run_chat, "ThreadsClient", return_value=client),
        mock.patch.object(run_chat, "fetch_user_id", return_value=("123", "u")),
        # 실네트워크 차단 — 누락된 모의가 있으면 즉시 드러나게 한다.
        mock.patch("src.threads_client._request",
                   side_effect=AssertionError("실네트워크 호출 금지")),
        mock.patch.object(run_chat.dt, "datetime", FrozenDT),
        mock.patch.object(mood_source, "collect",
                          return_value=mood_source.Mood("web", ("연준",), "관망")),
        mock.patch.object(ai_writer, "generate", return_value=generate),
        mock.patch.object(run_chat.antibot, "jitter_sleep", return_value=0),
        mock.patch.object(run_chat.run_reply, "sweep", side_effect=sweep_effect,
                          return_value=0) as sweep,
        mock.patch.object(run_chat, "_notify_safe") as notify,
    ):
        code = run_chat.main() if entry == "main" else run_chat.run()
    return code, sweep, notify


class TestChatIsolation:
    def test_sweep_server_error_does_not_undo_chat(self):
        client = _client()
        code, sweep, _ = _run(client, sweep_effect=SERVER)
        assert code == 0
        client.publish_text_post.assert_called_once()
        assert sweep.called

    def test_sweep_blocked_propagates_code_7(self):
        client = _client()
        code, _, notify = _run(client, sweep_effect=BLOCKED)
        assert code == 7
        assert "최우선" in notify.call_args.args[0]

    def test_reply_disabled_skips_sweep(self):
        client = _client()
        with mock.patch.object(config, "REPLY_ENABLED", False):
            code, sweep, _ = _run(client)
        assert code == 0
        assert not sweep.called
        client.publish_text_post.assert_called_once()


class TestChatErrorMapping:
    def test_blocked_on_quota_returns_7(self):
        client = _client()
        client.get_post_quota.side_effect = BLOCKED
        code, _, notify = _run(client)
        assert code == 7
        assert not client.publish_text_post.called

    def test_auth_error_returns_4_with_hint(self):
        client = _client()
        client.get_my_posts.side_effect = AUTH
        code, _, notify = _run(client)
        assert code == 4
        assert "재인가" in notify.call_args.args[0]

    def test_publish_failure_returns_4(self):
        client = _client()
        client.publish_text_post.side_effect = SERVER
        code, _, notify = _run(client)
        assert code == 4
        assert "[Threads Chat]" in notify.call_args.args[0]

    def test_unexpected_returns_1(self):
        client = _client()
        client.get_recent_texts.side_effect = RuntimeError("boom")
        code, _, notify = _run(client)
        assert code == 1

    def test_missing_env_returns_2(self):
        os.environ.pop("THREADS_LONG_LIVED_TOKEN")
        code, _, _ = _run(_client())
        assert code == 2


class TestChatSkipPaths:
    def test_quota_exhausted_blocks_publish(self):
        client = _client(quota=Quota(used=250, total=250))
        code, sweep, _ = _run(client)
        assert code == 0
        assert not client.publish_text_post.called
        assert sweep.called

    def test_no_ai_key_skips_publish(self):
        os.environ.pop("CLAUDE_AI_KEY")
        client = _client()
        code, sweep, _ = _run(client)
        assert code == 0
        assert not client.publish_text_post.called
        assert sweep.called

    def test_ai_disabled_skips_publish(self):
        client = _client()
        with mock.patch.object(config, "AI_ENABLED", False):
            code, _, _ = _run(client)
        assert code == 0
        assert not client.publish_text_post.called

    def test_rest_day_does_nothing(self):
        client = _client()
        with mock.patch("src.antibot.is_rest_day", return_value=True):
            code, sweep, _ = _run(client)
        assert code == 0
        assert not client.get_post_quota.called
        assert not sweep.called

    def test_generation_error_skips_publish(self):
        from src import ai_writer, run_chat

        client = _client()
        trigger, now = _selected_now()
        os.environ["TRIGGER"] = trigger
        FrozenDT.NOW = now
        with (
            mock.patch.object(run_chat, "_acquire_token", return_value="tok"),
            mock.patch.object(run_chat, "ThreadsClient", return_value=client),
            mock.patch.object(run_chat, "fetch_user_id", return_value=("123", "u")),
            mock.patch.object(run_chat.dt, "datetime", FrozenDT),
            mock.patch.object(run_chat.mood_source, "collect",
                              return_value=run_chat.mood_source.Mood("none")),
            mock.patch.object(ai_writer, "generate",
                              side_effect=ai_writer.AiWriterError("API 529")) as gen,
            mock.patch.object(run_chat.run_reply, "sweep", return_value=0),
        ):
            assert run_chat.run() == 0
        assert gen.call_count == config.AI_MAX_RETRY
        assert not client.publish_text_post.called

    def test_explicit_user_id_skips_lookup(self):
        from src import ai_writer, run_chat

        os.environ["THREADS_USER_ID"] = "123456789012345"
        client = _client()
        trigger, now = _selected_now()
        os.environ["TRIGGER"] = trigger
        FrozenDT.NOW = now
        with (
            mock.patch.object(run_chat, "_acquire_token", return_value="tok"),
            mock.patch.object(run_chat, "ThreadsClient", return_value=client) as ctor,
            mock.patch.object(run_chat, "fetch_user_id") as lookup,
            mock.patch.object(run_chat.dt, "datetime", FrozenDT),
            mock.patch.object(run_chat.mood_source, "collect",
                              return_value=run_chat.mood_source.Mood("none")),
            mock.patch.object(ai_writer, "generate", return_value="연준 얘기 많은 아침이네요?"),
            mock.patch.object(run_chat.antibot, "jitter_sleep", return_value=0),
            mock.patch.object(run_chat.run_reply, "sweep", return_value=0),
        ):
            assert run_chat.run() == 0
        assert not lookup.called
        assert ctor.call_args.args[0] == "123456789012345"


# ---------------------------------------------------------------------------
# run_reply 장애 경로
# ---------------------------------------------------------------------------


def _reply_client(conv_effect=None, publish_effect=None):
    now = dt.datetime.now(dt.UTC)
    ts = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    client = mock.Mock()
    client.get_reply_quota.return_value = Quota(used=0, total=1000)
    client.get_my_posts.return_value = [{"id": "p1", "text": "원글", "timestamp": ts},
                                        {"id": "p2", "text": "원글2", "timestamp": ts}]
    comment = {"id": "c1", "text": "한국어 댓글입니다", "username": "alice", "timestamp": ts,
               "replied_to": {"id": "p1"}, "is_reply_owned_by_me": False,
               "hide_status": "NOT_HUSHED"}
    client.get_conversation.side_effect = conv_effect or (lambda pid, limit: [comment]
                                                          if pid == "p1" else [])
    if publish_effect:
        client.publish_self_reply.side_effect = publish_effect
    else:
        client.publish_self_reply.return_value = "r1"
    return client


def _sweep(client):
    from src import run_reply
    from src.env import load_settings

    with (
        mock.patch("src.reply_engine._generate_reply", return_value="답글입니다"),
        mock.patch("src.antibot.time.sleep"),
        mock.patch.object(run_reply.notifier, "send") as send,
    ):
        sent = run_reply.sweep(client, load_settings(), per_run_cap=4, dry_run=False)
    return sent, send


class TestReplyFailurePaths:
    def test_one_conversation_failure_does_not_stop_others(self):
        def conv(pid, limit):
            if pid == "p2":
                raise SERVER
            return [{"id": "c1", "text": "한국어 댓글입니다", "username": "alice",
                     "timestamp": "2026-09-21T00:00:00+0000", "replied_to": {"id": "p1"},
                     "is_reply_owned_by_me": False, "hide_status": "NOT_HUSHED"}]

        sent, _ = _sweep(_reply_client(conv_effect=conv))
        assert sent == 1

    def test_conversation_blocked_raises(self):
        with pytest.raises(ThreadsApiError):
            _sweep(_reply_client(conv_effect=BLOCKED))

    def test_publish_failure_notifies_and_continues(self):
        sent, send = _sweep(_reply_client(publish_effect=SERVER))
        assert sent == 0
        assert "발행 실패" in send.call_args.args[2]

    def test_publish_blocked_raises(self):
        with pytest.raises(ThreadsApiError):
            _sweep(_reply_client(publish_effect=BLOCKED))

    def test_reply_quota_exhausted(self):
        client = _reply_client()
        client.get_reply_quota.return_value = Quota(used=1000, total=1000)
        sent, _ = _sweep(client)
        assert sent == 0
        assert not client.get_my_posts.called

    def test_slot_mismatch_stops_run(self):
        from src import run_reply

        os.environ.update({"REPLY_SLOTS": "A,B,C", "SLOT": "MANUAL"})
        with mock.patch.object(run_reply, "sweep") as sweep:
            assert run_reply.run() == 1   # v1.2.0: 설정 오류는 실패 코드
        assert not sweep.called

    def test_all_slots_run_now(self):
        """v1.1.0: 1-of-3 추첨 제거. 어느 슬롯이든 sweep 한다."""
        from src import run_reply

        for slot in ("A", "B", "C"):
            os.environ.update({"REPLY_SLOTS": "A,B,C", "SLOT": slot})
            with (
                mock.patch.object(run_reply, "sweep", return_value=0) as sweep,
                mock.patch.object(run_reply, "_acquire_token", return_value="tok"),
                mock.patch.object(run_reply, "fetch_user_id", return_value=("1", "u")),
                mock.patch.object(run_reply, "ThreadsClient"),
            ):
                assert run_reply.run() == 0
            assert sweep.called, slot
            # v1.2.0: 예약 실행 전용 상한(timeout 안에 들어오게)
            assert sweep.call_args.kwargs["per_run_cap"] == config.REPLY_SCHEDULED_RUN_CAP

    def test_main_blocked_returns_7(self):
        from src import run_reply

        with (
            mock.patch.object(run_reply, "run", side_effect=BLOCKED),
            mock.patch.object(run_reply, "_notify_safe"),
        ):
            assert run_reply.main() == 7

"""답글 엔진·안티봇 회귀 테스트."""

from __future__ import annotations

import datetime as dt

from src import antibot, reply_engine
from src.reply_engine import Comment, ReplyStrategy


def mk(text="좋은 글이네요", **kw) -> Comment:
    base = {
        "id": "c1", "text": text, "username": "u1", "timestamp": "",
        "replied_to_id": "p1", "owned_by_me": False, "hide_status": "NOT_HUSHED",
    }
    base.update(kw)
    return Comment(**base)


def d(comment, replied=False, used=0):
    return reply_engine.decide(comment, already_replied=replied, author_used=used)


class TestLanguageGate:
    def test_korean_normal(self):
        assert d(mk("오늘 글 잘 봤습니다")).strategy is ReplyStrategy.NORMAL

    def test_english_non_korean(self):
        assert d(mk("Great post, thanks!")).strategy is ReplyStrategy.NON_KOREAN

    def test_japanese_non_korean(self):
        assert d(mk("とても面白い")).strategy is ReplyStrategy.NON_KOREAN

    def test_mixed_mostly_korean(self):
        assert d(mk("이 automation 방식 좋네요")).strategy is ReplyStrategy.NORMAL


class TestChoiceGate:
    def test_ab(self):
        assert d(mk("A랑 B 중에 뭐가 나은가요?")).strategy is ReplyStrategy.NEUTRAL_THANKS

    def test_numbered(self):
        assert d(mk("1번 2번 중에 골라주세요")).strategy is ReplyStrategy.NEUTRAL_THANKS

    def test_dulchung(self):
        assert d(mk("둘 중에 어느 쪽이 좋을까요")).strategy is ReplyStrategy.NEUTRAL_THANKS


class TestSkipGate:
    def test_own_comment(self):
        assert d(mk(owned_by_me=True)).strategy is ReplyStrategy.SKIP

    def test_already_replied(self):
        assert d(mk(), replied=True).strategy is ReplyStrategy.SKIP

    def test_author_cap(self):
        assert d(mk(), used=99).strategy is ReplyStrategy.SKIP

    def test_hidden(self):
        assert d(mk(hide_status="HUSHED")).strategy is ReplyStrategy.SKIP

    def test_too_short(self):
        assert d(mk("ㅋ")).strategy is ReplyStrategy.SKIP

    def test_emoji_only(self):
        assert d(mk("🔥🔥🔥")).strategy is ReplyStrategy.SKIP


class TestCanned:
    def test_deterministic(self):
        a = reply_engine.pick_canned(reply_engine.NON_KOREAN_REPLIES, "x1")
        b = reply_engine.pick_canned(reply_engine.NON_KOREAN_REPLIES, "x1")
        assert a == b

    def test_within_pool(self):
        got = reply_engine.pick_canned(reply_engine.NEUTRAL_THANKS_REPLIES, "z9")
        assert got in reply_engine.NEUTRAL_THANKS_REPLIES


class TestAntibot:
    def test_slot_deterministic(self):
        day = dt.date(2026, 9, 13)
        assert antibot.choose_slot(day, ["A", "B", "C"]) == antibot.choose_slot(
            day, ["A", "B", "C"]
        )

    def test_slot_distribution(self):
        seen = {
            antibot.choose_slot(dt.date(2026, 9, 1) + dt.timedelta(days=i), ["A", "B", "C"])
            for i in range(30)
        }
        assert seen == {"A", "B", "C"}

    def test_salt_separates(self):
        day = dt.date(2026, 9, 13)
        pub = [antibot.choose_slot(day + dt.timedelta(days=i), ["A", "B", "C"], "publish")
               for i in range(20)]
        rep = [antibot.choose_slot(day + dt.timedelta(days=i), ["A", "B", "C"], "reply")
               for i in range(20)]
        assert pub != rep

    def test_jitter_dry_run_no_sleep(self):
        assert antibot.jitter_sleep(60, 480, dry_run=True) == 0

    def test_daily_cap(self):
        assert antibot.within_daily_cap(5, 20)
        assert not antibot.within_daily_cap(20, 20)

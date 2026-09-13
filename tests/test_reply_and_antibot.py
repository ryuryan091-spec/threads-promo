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


class TestSlotGate:
    """수동 실행이 슬롯 게이트에 막히지 않는지 확인한다."""

    @staticmethod
    def _gate(monkeypatch_env: dict) -> bool:
        import datetime as dt
        import os

        from src import main

        for key in ("EVENT_NAME", "SLOT", "PUBLISH_SLOTS"):
            os.environ.pop(key, None)
        os.environ.update(monkeypatch_env)
        try:
            return main._slot_gate(dt.date(2026, 9, 13))
        finally:
            for key in ("EVENT_NAME", "SLOT", "PUBLISH_SLOTS"):
                os.environ.pop(key, None)

    def test_manual_dispatch_always_runs(self):
        assert self._gate(
            {"EVENT_NAME": "workflow_dispatch", "SLOT": "MANUAL",
             "PUBLISH_SLOTS": "A,B,C"}
        )

    def test_scheduled_non_matching_slot_skips(self):
        results = {
            self._gate({"EVENT_NAME": "schedule", "SLOT": s, "PUBLISH_SLOTS": "A,B,C"})
            for s in ("A", "B", "C")
        }
        assert results == {True, False}

    def test_unknown_slot_on_schedule_blocks(self):
        assert not self._gate(
            {"EVENT_NAME": "schedule", "SLOT": "MANUAL", "PUBLISH_SLOTS": "A,B,C"}
        )

    def test_no_env_runs(self):
        assert self._gate({})


class TestImageUrlGuard:
    """잘못된 이미지 base URL이 발행까지 흘러가지 않는지 확인한다."""

    @staticmethod
    def _resolve(override: str) -> str:
        import os

        from src import main
        from src.env import load_settings

        os.environ.update(
            GITHUB_REPOSITORY="owner/repo",
            GITHUB_REF_NAME="main",
            THREADS_APP_ID="1",
            THREADS_APP_SECRET="s",
            THREADS_LONG_LIVED_TOKEN="t",
        )
        os.environ["ASSET_RAW_BASE_URL"] = override
        try:
            return main._resolve_raw_base_url(load_settings())
        finally:
            os.environ.pop("ASSET_RAW_BASE_URL", None)

    def test_social_host_is_rejected(self):
        got = self._resolve("https://www.youtube.com/@handle")
        assert got == "https://raw.githubusercontent.com/owner/repo/main/assets"

    def test_non_https_is_rejected(self):
        got = self._resolve("http://example.com/assets")
        assert got.startswith("https://raw.githubusercontent.com")

    def test_valid_override_is_kept(self):
        assert self._resolve("https://cdn.example.com/assets") == (
            "https://cdn.example.com/assets"
        )

    def test_empty_falls_back(self):
        assert self._resolve("") == (
            "https://raw.githubusercontent.com/owner/repo/main/assets"
        )


class TestImageValidation:
    def test_forbidden_host(self):
        from src.threads_client import ImageValidationError, verify_image_url

        try:
            verify_image_url("https://www.youtube.com/@x/promo_01.png")
        except ImageValidationError as exc:
            assert "자산 호스트" in str(exc)
        else:
            raise AssertionError("차단되지 않았습니다")

    def test_non_https(self):
        from src.threads_client import ImageValidationError, verify_image_url

        try:
            verify_image_url("http://example.com/a.png")
        except ImageValidationError as exc:
            assert "https" in str(exc)
        else:
            raise AssertionError("차단되지 않았습니다")

"""v1.3.0 회귀 테스트 — 자연스러운 작성(A) · 답글 확대(B) · 랜덤 발행(C).

항목 번호는 threads-promo/DESIGN_V13_HUMANIZE.md 와 같다.
"""

from __future__ import annotations

import datetime as dt
import math
import pathlib
from collections import Counter
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import yaml

from src import (
    ai_writer,
    antibot,
    chat_plan,
    config,
    content,
    insights,
    reply_engine,
    run_reply,
    style,
    watchdog,
)
from src.reply_engine import Comment
from src.threads_client import ContainerNotReadyError, Quota, ThreadsApiError

KST = ZoneInfo("Asia/Seoul")
ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
DAY = dt.date(2026, 9, 28)


def _ts(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


# ===========================================================================
# A. 자연스러운 작성
# ===========================================================================


class TestStyleAxes:
    def test_weighted_pick_deterministic(self):
        w = config.STYLE_LENGTH_WEIGHTS
        assert style.weighted_pick("k", w) == style.weighted_pick("k", w)

    def test_weighted_pick_distribution(self):
        w = (("a", 25), ("b", 45), ("c", 30))
        got = Counter(style.weighted_pick(f"k{i}", w) for i in range(20000))
        for name, weight in w:
            assert got[name] / 20000 == pytest.approx(weight / 100, abs=0.02)

    def test_weighted_pick_rejects_zero(self):
        with pytest.raises(ValueError):
            style.weighted_pick("k", (("a", 0),))

    def test_post_style_all_axes_appear(self):
        styles = [style.pick_post_style(f"{DAY}::{i}") for i in range(400)]
        assert {s.length for s in styles} == {"one", "short", "normal"}
        assert {s.layout for s in styles} == {"lines", "block", "mixed"}

    def test_one_sentence_block_has_no_layout(self):
        block = style.PostStyle("one", "lines").block()
        assert "한 문장" in block and "줄" not in block.split("\n", 2)[-1]

    def test_block_header(self):
        assert style.PostStyle("short", "block").block().startswith("# 이번 글 형식")

    def test_chat_prefers_short(self):
        n = 5000
        chat = Counter(style.pick_post_style(f"c{i}", chat=True).length for i in range(n))
        post = Counter(style.pick_post_style(f"c{i}").length for i in range(n))
        assert chat["normal"] < post["normal"]

    def test_reply_style(self):
        # v1.4.0: 형식 범주는 댓글 내용으로 정하고 범주 안에서만 해시로 변주한다.
        #   되묻기 허용 비율(REPLY_ASK_PCT)은 보통·긴 댓글에만 적용된다.
        text = "이 방식으로 계속 기록하시는 거 좋아 보여요"   # 보통 댓글
        styles = [style.pick_reply_style(f"cid{i}", text) for i in range(4000)]
        assert {s.length for s in styles} == {"tiny", "one"}
        ask = sum(s.may_ask for s in styles) / len(styles)
        assert ask == pytest.approx(config.REPLY_ASK_PCT / 100, abs=0.03)
        assert "물음표를 쓰지 않습니다" in style.ReplyStyle("one", False).block()


class TestRepetition:
    def test_first_word(self):
        assert style.first_word("  요즘은, 조용합니다") == "요즘은"
        assert style.first_word("") == ""

    def test_ending(self):
        assert style.ending("조용하네요.") == "하네요"
        assert style.ending("정말요?!") == "정말요"

    def test_first_word_repeat(self):
        assert "첫 어절" in style.repetition_issue("요즘 시장은 조용", ["요즘 뭐가 다른지"])

    def test_ending_repeat_threshold(self):
        recent = ["가는 것 같습니다", "보는 것 같습니다", "하나 다릅니다"]
        # 같은 끝맺음 2건까지 허용(REPETITION_ENDING_MAX=2), 3건째부터 반복
        assert style.repetition_issue("새로 쓴 글 같습니다", recent) is None
        recent.append("하나 더 같습니다")
        assert "끝맺음" in style.repetition_issue("새로 쓴 글 같습니다", recent)

    def test_lookback_limited(self):
        recent = ["x"] * config.REPETITION_LOOKBACK + ["요즘 오래된 글"]
        assert style.repetition_issue("요즘 새 글", recent) is None

    def test_no_recent(self):
        assert style.repetition_issue("아무 글", []) is None


class TestGenerateWithStyle:
    def test_repetition_retries_then_accepts_on_last(self):
        recent = ["요즘 첫 글"]
        with mock.patch.object(ai_writer, "generate", return_value="요즘 두번째 글") as gen:
            text = content._generate_with_ai("k", "MARKET", "s", recent, style_block="# 이번 글 형식")
        assert text == "요즘 두번째 글"
        assert gen.call_count == config.AI_MAX_RETRY
        assert gen.call_args.kwargs["style_block"] == "# 이번 글 형식"

    def test_repetition_resolved_on_retry(self):
        with mock.patch.object(ai_writer, "generate", side_effect=["요즘 반복", "오늘은 다름"]):
            assert content._generate_with_ai("k", "MARKET", "s", ["요즘 전 글"]) == "오늘은 다름"

    def test_hard_lint_still_fails(self):
        with (
            mock.patch.object(ai_writer, "generate", return_value="여러분 안녕하세요"),
            pytest.raises(ai_writer.AiWriterError),
        ):
            content._generate_with_ai("k", "MARKET", "s", [])

    def test_build_plan_passes_style(self, tmp_path):
        (tmp_path / "a.png").write_bytes(b"x")
        with mock.patch.object(ai_writer, "generate", return_value="평범한 관찰 한 줄") as gen:
            plan = content.build_plan(DAY, tmp_path, "https://raw", claude_api_key="k",
                                      discriminator=0)
        assert plan.source == "ai"
        assert gen.call_args.kwargs["style_block"].startswith("# 이번 글 형식")

    def test_prompt_contains_style_and_closing(self):
        body = ai_writer._build_user_prompt(
            ai_writer.PILLARS["MARKET"], "s", [], closing=ai_writer.CLOSING_TRAIL,
            style_block="# 이번 글 형식\n한 문장",
        )
        assert "# 이번 글 형식" in body and "여운" in body
        assert body.index("# 이번 글 형식") < body.index("# 마무리")

    def test_system_prompt_no_fixed_sentence_count(self):
        assert "2~4문장" not in ai_writer.SYSTEM_PROMPT
        assert "1~3문장" not in ai_writer.CHAT_PILLAR.brief


class TestClosings:
    def test_four_closing_texts(self):
        for c in (ai_writer.CLOSING_QUESTION, ai_writer.CLOSING_STATEMENT,
                  ai_writer.CLOSING_TRAIL, ai_writer.CLOSING_ASIDE):
            assert ai_writer.closing_block(c).startswith("# 마무리")
        assert ai_writer.closing_block("??") == ai_writer.closing_block(ai_writer.CLOSING_QUESTION)

    def test_pattern_coprime(self):
        n = len(config.CLOSING_PATTERN)
        for other in (len(ai_writer.PILLAR_ROTATION), len(config.REPLY_LEADS),
                      len(config.REPLY_LAYOUTS)):
            assert math.gcd(n, other) == 1

    def test_regular_closing_all_kinds(self):
        got = {content.closing_for(i) for i in range(len(config.CLOSING_PATTERN))}
        assert got == {"question", "statement", "trail", "aside"}

    def test_chat_closing_split(self):
        days = [DAY + dt.timedelta(days=i) for i in range(365)]
        got = Counter(chat_plan.closing_for(d, t) for d in days for t in range(1, 10))
        total = sum(got.values())
        assert got["question"] / total == pytest.approx(0.45, abs=0.03)
        rest = total - got["question"]
        assert got["statement"] / rest == pytest.approx(0.45, abs=0.04)
        assert got["trail"] / rest == pytest.approx(0.30, abs=0.04)
        assert got["aside"] / rest == pytest.approx(0.25, abs=0.04)

    def test_chat_style_key_idempotent(self):
        assert chat_plan.style_key(DAY, 3) == chat_plan.style_key(DAY, 3)
        assert chat_plan.style_key(DAY, 3) != chat_plan.style_key(DAY, 4)


class TestLintAndFallback:
    @pytest.mark.parametrize("term", config.FORBIDDEN_AI_TELL_TERMS)
    def test_ai_tell_blocked(self, term):
        with pytest.raises(content.ContentPolicyError):
            content.lint(f"평범한 문장 {term} 끝")

    def test_fallback_texts_pass_lint(self):
        for text in content.PROMO_TEXTS + content.OBSERVATION_TEXTS:
            content.lint(text)
            assert "여러분" not in text

    def test_fallback_not_all_questions(self):
        texts = content.PROMO_TEXTS + content.OBSERVATION_TEXTS
        assert any(not t.rstrip().endswith("?") for t in texts)

    def test_canned_replies_pass_lint(self):
        # v1.4.0: 선택형 정형 문구(NEUTRAL_THANKS_REPLIES) 폐지 — 외국어 문구만 남는다.
        for text in reply_engine.NON_KOREAN_REPLIES:
            content.lint_reply(text)

    def test_seed_pools_expanded(self):
        assert len(ai_writer.PILLARS["MARKET"].seeds) >= 16
        assert len(ai_writer.PILLARS["STORY"].seeds) >= 16
        assert len(ai_writer.PILLARS["PROMO"].seeds) >= 10
        assert len(ai_writer.CHAT_PILLAR.seeds) >= 18
        for pillar in list(ai_writer.PILLARS.values()) + [ai_writer.CHAT_PILLAR]:
            assert len(set(pillar.seeds)) == len(pillar.seeds)


class TestLinkReply:
    def test_layouts_rotate_with_both_links(self):
        with (
            mock.patch.object(config, "YOUTUBE_URL", "https://www.youtube.com/@yt"),
            mock.patch.object(config, "X_URL", "https://x.com/xx"),
        ):
            n = len(config.REPLY_LEADS) * len(config.REPLY_LAYOUTS)
            texts = [content.build_reply_text(i) for i in range(n)]
        assert len(set(texts)) == n   # 문구 6 × 배치 5 = 30가지 전부 다름
        for t in texts:
            assert "https://www.youtube.com/@yt" in t and "https://x.com/xx" in t
            content.lint(t)


# ===========================================================================
# B. 답글 확대
# ===========================================================================


class TestReplyCapsEnv:
    def test_int_env_empty_is_default(self, monkeypatch):
        monkeypatch.setenv("X_TEST_INT", "  ")
        assert config._int_env("X_TEST_INT", 7) == 7
        monkeypatch.setenv("X_TEST_INT", "12")
        assert config._int_env("X_TEST_INT", 7) == 12

    def test_bool_env(self, monkeypatch):
        monkeypatch.setenv("X_TEST_BOOL", "")
        assert config._bool_env("X_TEST_BOOL", True) is True
        monkeypatch.setenv("X_TEST_BOOL", "TRUE")
        assert config._bool_env("X_TEST_BOOL", False) is True
        monkeypatch.setenv("X_TEST_BOOL", "no")
        assert config._bool_env("X_TEST_BOOL", True) is False

    @pytest.mark.safety_defaults
    def test_defaults(self):
        # v1.6.0(S4): 안전 기본값으로 축소 40/3/4 → 10/1/2 (conftest legacy 프로필 미적용)
        assert config.REPLY_DAILY_CAP == 10
        assert config.REPLY_AUTHOR_DAILY_CAP == 1
        assert config.REPLY_THREAD_AUTHOR_CAP == 2
        assert config.FOLLOWUP_ENABLED is False   # 새 발행 행위는 기본 비활성
        assert config.REPLY_DAILY_CAP < config.DAILY_REPLY_QUOTA

    def test_decide_uses_runtime_cap(self):
        c = Comment("c1", "한국어 댓글입니다", "alice", "", "p1", False, "NOT_HUSHED")
        assert reply_engine.decide(c, already_replied=False, author_used=2).strategy \
            is reply_engine.ReplyStrategy.NORMAL
        with mock.patch.object(config, "REPLY_AUTHOR_DAILY_CAP", 2):
            assert reply_engine.decide(c, already_replied=False, author_used=2).strategy \
                is reply_engine.ReplyStrategy.SKIP


class TestReplyStylePrompt:
    def test_style_block_in_prompt(self):
        body = reply_engine.build_reply_prompt("원글", "댓글", "", "# 이번 답글 형식\n한 마디")
        assert "# 이번 답글 형식" in body
        assert body.index("# 이번 답글 형식") < body.index("JSON으로만")

    def test_compose_passes_style_by_comment_id(self):
        c = Comment("cid-9", "한국어 댓글입니다", "alice", "", "p1", False, "NOT_HUSHED")
        d = reply_engine.ReplyDecision(c, reply_engine.ReplyStrategy.NORMAL, "일반")
        with mock.patch.object(reply_engine, "_generate_reply", return_value="그렇네요") as gen:
            assert reply_engine.compose(d, "원글", "k", content.lint_reply) == "그렇네요"
        expected = style.pick_reply_style("cid-9").block()
        assert gen.call_args.kwargs["style_block"] == expected

    def test_system_prompt_not_always_question(self):
        assert "모든 답글을 질문으로 끝내지 않습니다" in reply_engine.REPLY_SYSTEM_PROMPT
        assert "1~2문장" not in reply_engine.REPLY_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# B4 셀프 이어쓰기
# ---------------------------------------------------------------------------

NOW = dt.datetime(2026, 9, 28, 6, 0, tzinfo=dt.UTC)   # KST 15:00


def _post(pid: str, hours_ago: float, text: str = "원글 본문입니다") -> dict:
    return {"id": pid, "text": text, "timestamp": _ts(NOW - dt.timedelta(hours=hours_ago))}


def _mine(cid: str, parent: str, text: str = "덧붙임", hours_ago: float = 0.5) -> Comment:
    return Comment(cid, text, "me", _ts(NOW - dt.timedelta(hours=hours_ago)), parent, True,
                   "NOT_HUSHED")


def _target_ids(n: int, want: bool = True) -> list[str]:
    ids, i = [], 0
    while len(ids) < n:
        pid = f"post{i}"
        if run_reply.is_followup_target(pid) is want:
            ids.append(pid)
        i += 1
    return ids


class TestFollowupSelection:
    def test_target_ratio(self):
        hit = sum(run_reply.is_followup_target(f"p{i}") for i in range(20000))
        assert hit / 20000 == pytest.approx(config.FOLLOWUP_PCT / 100, abs=0.02)

    def test_age_window(self):
        pid = _target_ids(1)[0]
        conv = {pid: []}
        for hours, ok in ((1.0, False), (2.5, True), (11.9, True), (12.5, False)):
            got = run_reply.followup_candidates([_post(pid, hours)], conv, NOW)
            assert bool(got) is ok, hours

    def test_non_target_skipped(self):
        pid = _target_ids(1, want=False)[0]
        assert run_reply.followup_candidates([_post(pid, 3)], {pid: []}, NOW) == []

    def test_link_reply_is_not_followup(self):
        pid = _target_ids(1)[0]
        conv = {pid: [_mine("m1", pid, "매일 올리는 곳\nhttps://www.youtube.com/@x")]}
        assert run_reply.followup_candidates([_post(pid, 3)], conv, NOW)

    def test_existing_followup_blocks(self):
        pid = _target_ids(1)[0]
        conv = {pid: [_mine("m1", pid, "생각해보니 하나 더")]}
        assert run_reply.followup_candidates([_post(pid, 3)], conv, NOW) == []

    def test_reply_to_comment_is_not_followup(self):
        pid = _target_ids(1)[0]
        conv = {pid: [_mine("m1", "c1", "댓글에 단 답글")]}
        assert run_reply.followup_candidates([_post(pid, 3)], conv, NOW)

    def test_missing_conversation_skipped(self):
        pid = _target_ids(1)[0]
        assert run_reply.followup_candidates([_post(pid, 3)], {}, NOW) == []

    def test_count_today(self):
        today = NOW.astimezone(KST).date()
        conv = {
            "p1": [_mine("m1", "p1"), _mine("m2", "p1", "https://x.com/a"),
                   _mine("m3", "c9")],
            "p2": [_mine("m4", "p2", hours_ago=30)],   # 어제
        }
        assert run_reply.followup_count_today(conv, today) == 1


def _settings():
    s = mock.Mock()
    s.claude_api_key = "k"
    s.telegram_bot_token = ""
    s.telegram_chat_id = ""
    return s


class TestFollowupRun:
    def _run(self, client, posts, conv, **kw):
        args = {"dry_run": False, "started": 0.0, "budget_sec": None, "reply_remaining": 100}
        args.update(kw)
        with mock.patch.object(antibot, "jitter_sleep", return_value=0):
            return run_reply._followups(client, _settings(), posts, conv, NOW, **args)

    def test_publishes_one_per_run(self):
        ids = _target_ids(3)
        posts = [_post(pid, 3) for pid in ids]
        client = mock.Mock()
        client.publish_self_reply.return_value = "r1"
        with mock.patch.object(reply_engine, "compose_followup", return_value="덧붙이는 한 마디"):
            assert self._run(client, posts, {pid: [] for pid in ids}) == config.FOLLOWUP_PER_RUN
        assert client.publish_self_reply.call_count == 1
        assert client.publish_self_reply.call_args.args[0] in ids

    def test_daily_cap(self):
        ids = _target_ids(1)
        conv = {ids[0]: []}
        for i in range(config.FOLLOWUP_DAILY_CAP):
            conv[f"old{i}"] = [_mine(f"m{i}", f"old{i}")]
        client = mock.Mock()
        assert self._run(client, [_post(ids[0], 3)], conv) == 0
        assert not client.publish_self_reply.called

    def test_reply_quota_exhausted(self):
        ids = _target_ids(1)
        client = mock.Mock()
        assert self._run(client, [_post(ids[0], 3)], {ids[0]: []}, reply_remaining=0) == 0

    def test_dry_run_does_not_publish(self):
        ids = _target_ids(1)
        client = mock.Mock()
        with mock.patch.object(reply_engine, "compose_followup", return_value="한 마디"):
            assert self._run(client, [_post(ids[0], 3)], {ids[0]: []}, dry_run=True) == 1
        assert not client.publish_self_reply.called

    def test_generation_failure_skips(self):
        ids = _target_ids(1)
        client = mock.Mock()
        with mock.patch.object(reply_engine, "compose_followup", return_value=None):
            assert self._run(client, [_post(ids[0], 3)], {ids[0]: []}) == 0

    def test_container_failure_notifies_and_continues(self):
        ids = _target_ids(1)
        client = mock.Mock()
        client.publish_self_reply.side_effect = ContainerNotReadyError("x")
        with (
            mock.patch.object(reply_engine, "compose_followup", return_value="한 마디"),
            mock.patch.object(run_reply.notifier, "send") as send,
        ):
            assert self._run(client, [_post(ids[0], 3)], {ids[0]: []}) == 0
        assert send.called

    def test_blocked_propagates(self):
        ids = _target_ids(1)
        client = mock.Mock()
        client.publish_self_reply.side_effect = ThreadsApiError(400, "blocked", code=200)
        with (
            mock.patch.object(reply_engine, "compose_followup", return_value="한 마디"),
            pytest.raises(ThreadsApiError),
        ):
            self._run(client, [_post(ids[0], 3)], {ids[0]: []})

    def test_budget_guard(self):
        ids = _target_ids(1)
        client = mock.Mock()
        with mock.patch.object(reply_engine, "compose_followup", return_value="한 마디") as comp:
            assert self._run(client, [_post(ids[0], 3)], {ids[0]: []}, budget_sec=10) == 0
        assert not comp.called


class TestFollowupSweepWiring:
    def _client(self, posts):
        client = mock.Mock()
        client.get_reply_quota.return_value = Quota(0, 1000)
        client.get_my_posts.return_value = posts
        client.get_conversation.return_value = []
        return client

    def test_sweep_calls_followups_only_when_allowed_and_enabled(self):
        posts = [_post(_target_ids(1)[0], 3)]
        for allow, enabled, expect in ((True, True, True), (False, True, False),
                                       (True, False, False)):
            with (
                mock.patch.object(config, "FOLLOWUP_ENABLED", enabled),
                mock.patch.object(run_reply, "_followups", return_value=0) as fu,
            ):
                run_reply.sweep(self._client(posts), _settings(), per_run_cap=4,
                                dry_run=True, now=NOW, allow_followup=allow)
            assert fu.called is expect, (allow, enabled)

    def test_followups_run_even_when_comment_cap_reached(self):
        posts = [_post(_target_ids(1)[0], 3)]
        with (
            mock.patch.object(config, "FOLLOWUP_ENABLED", True),
            mock.patch.object(config, "REPLY_DAILY_CAP", 0),
            mock.patch.object(run_reply, "_followups", return_value=0) as fu,
        ):
            run_reply.sweep(self._client(posts), _settings(), per_run_cap=4,
                            dry_run=True, now=NOW, allow_followup=True)
        assert fu.called

    def test_chat_sweep_never_followups(self):
        from src import run_chat

        with mock.patch.object(run_reply, "sweep", return_value=0) as sw:
            run_chat._safe_sweep(mock.Mock(), _settings())
        assert sw.call_args.kwargs.get("allow_followup", False) is False


class TestComposeFollowup:
    def _compose(self, value):
        with mock.patch.object(reply_engine, "_call_claude", return_value=value) as call:
            return reply_engine.compose_followup("원글", "k", content.lint_chat), call

    def test_ok(self):
        text, _ = self._compose("적고 나니 조금 다르게 보이기도 합니다.")
        assert text

    @pytest.mark.parametrize("bad", [
        "그렇지 않나요?",                     # 물음표
        "세 번째로 보니 3배 다릅니다",          # 숫자
        "https://x.com 참고",                 # 링크
        "여러분 생각은",                        # AI 티
        "가" * (config.FOLLOWUP_MAX_LEN + 1),  # 길이
    ])
    def test_rejects(self, bad):
        text, call = self._compose(bad)
        assert text is None
        assert call.call_count == config.AI_MAX_RETRY

    def test_no_key(self):
        assert reply_engine.compose_followup("원글", "", content.lint_chat) is None


class TestReplyWorkflow:
    def _wf(self, name):
        return yaml.safe_load((WF / name).read_text(encoding="utf-8"))

    def test_reply_slots_eight(self):
        data = self._wf("reply.yml")
        on = data.get(True) or data.get("on")
        assert len(on["schedule"]) == 8
        env = data["jobs"]["reply"]["steps"][-1]["env"]
        assert env["REPLY_SLOTS"] == "A,B,C,D,E,F,G,H"
        assert data["jobs"]["reply"]["timeout-minutes"] >= (
            config.REPLY_START_JITTER[1] + config.REPLY_SWEEP_BUDGET_SEC) / 60
        for key in ("REPLY_DAILY_CAP", "REPLY_AUTHOR_DAILY_CAP", "REPLY_THREAD_AUTHOR_CAP",
                    "FOLLOWUP_ENABLED", "FOLLOWUP_PCT", "FOLLOWUP_DAILY_CAP"):
            assert "||" in env[key], key   # 빈 값 방지 기본값

    def test_reply_crons_outside_chat_window(self):
        data = self._wf("reply.yml")
        on = data.get(True) or data.get("on")
        for item in on["schedule"]:
            minute, hour = map(int, item["cron"].split()[:2])
            kst = ((hour + 9) % 24) * 60 + minute
            assert not 9 * 60 <= kst < 12 * 60 + 5


# ===========================================================================
# C. 랜덤 발행
# ===========================================================================


class TestPublishSlots:
    def test_slot_table_matches_env_default_and_discriminators(self):
        assert set(config.PUBLISH_SLOTS) == set(config.PUBLISH_SLOT_TIMES)
        discs = [config.DISCRIMINATOR_BY_SLOT[s] for s in config.PUBLISH_SLOTS]
        assert len(set(discs)) == len(discs)
        assert config.DISCRIMINATOR_EVENT not in discs
        assert all(0 <= d < config.DISCRIMINATOR_MAX for d in discs)

    def test_legacy_times_keep_discriminator(self):
        """전환기: 이전 슬롯 시각(08:23/12:47/20:31)의 글이 같은 구분자로 복원된다."""
        for hhmm, disc in (("08:23", 0), ("08:31", 0), ("08:56", 0), ("12:47", 1),
                           ("12:55", 1), ("13:13", 1), ("20:31", 2), ("20:40", 2),
                           ("20:41", None)):   # 20:41 이후 이전 C 글은 판정불가(전환기 한계)
            h, m = map(int, hhmm.split(":"))
            when = dt.datetime(2026, 9, 20, h, m, tzinfo=KST)
            assert insights.discriminator_from_timestamp(when) == disc, hhmm

    def test_every_minute_of_new_windows_classifies(self):
        for slot, hhmm in config.PUBLISH_SLOT_TIMES.items():
            h, m = map(int, hhmm.split(":"))
            start = dt.datetime(2026, 9, 28, h, m, tzinfo=KST)
            for minute in range(config.PUBLISH_CLASSIFY_WINDOW_MIN + 1):
                got = insights.discriminator_from_timestamp(start + dt.timedelta(minutes=minute))
                assert got == config.DISCRIMINATOR_BY_SLOT[slot], (slot, minute)

    def test_windows_do_not_overlap(self):
        from scripts import verify_repo

        assert verify_repo.check_classify_windows() == 0

    def test_slot_choice_uniform(self):
        days = [DAY + dt.timedelta(days=i) for i in range(3650)]
        got = Counter(antibot.choose_slot(d, list(config.PUBLISH_SLOTS),
                                          config.ANTIBOT_SLOT_SALT_PUBLISH) for d in days)
        for slot in config.PUBLISH_SLOTS:
            assert got[slot] / len(days) == pytest.approx(1 / 7, abs=0.02)

    def test_watchdog_threshold_from_table(self):
        assert watchdog.SLOT_SPREAD_MAX_GAP_HOURS == pytest.approx(38.48, abs=0.01)
        worst_gap = watchdog.SLOT_SPREAD_MAX_GAP_HOURS + config.ANTIBOT_PUBLISH_JITTER[1] / 3600
        assert watchdog.stale_threshold_hours(0) > worst_gap


    def test_story_prediction_uses_new_slots(self):
        from src import run_story

        seen = set()
        for i in range(200):
            day = DAY + dt.timedelta(days=i)
            slot = antibot.choose_slot(day, list(config.PUBLISH_SLOTS),
                                       config.ANTIBOT_SLOT_SALT_PUBLISH)
            seen.add(slot)
            expected = ai_writer.pick_pillar(
                content.run_index(day, config.DISCRIMINATOR_BY_SLOT[slot]))
            with mock.patch.object(antibot, "is_rest_day", return_value=False):
                assert run_story._predicted_regular_pillar(day) == expected
        assert seen == set(config.PUBLISH_SLOTS)


class TestChatJitter:
    def test_jitter_fits_trigger_spacing_and_budget(self):
        marks = [int(t[:2]) * 60 + int(t[3:]) for t in config.CHAT_TRIGGERS]
        spacing = min(b - a for a, b in zip(marks, marks[1:], strict=False))
        assert config.CHAT_JITTER[1] / 60 < spacing
        low, high = chat_plan.jitter_range(config.CHAT_MAX_GAP_WAIT_SEC)
        assert high < config.CHAT_JOB_BUDGET_SEC


class TestUpcomingRegularGate:
    """리뷰 R3: 이벤트 글이 곧 나갈 정기 글과 EVENT_MIN_GAP_HOURS 안에 붙지 않게 한다."""

    def _slot_day(self, slot: str) -> dt.date:
        day = DAY
        while antibot.choose_slot(day, list(config.PUBLISH_SLOTS),
                                  config.ANTIBOT_SLOT_SALT_PUBLISH) != slot:
            day += dt.timedelta(days=1)
        return day

    def _at(self, day: dt.date, hhmm: str) -> dt.datetime:
        return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), tzinfo=KST)

    def test_minutes_ahead(self):
        from src import run_story

        day = self._slot_day("E")   # 15:07
        with mock.patch.object(antibot, "is_rest_day", return_value=False):
            assert run_story._minutes_to_regular_slot(day, self._at(day, "13:29")) == 98
            assert run_story._minutes_to_regular_slot(day, self._at(day, "15:30")) is None
        with mock.patch.object(antibot, "is_rest_day", return_value=True):
            assert run_story._minutes_to_regular_slot(day, self._at(day, "13:29")) is None

    def test_gate_blocks_when_regular_soon(self):
        from src import run_story

        day = self._slot_day("E")
        with (
            mock.patch.object(antibot, "is_rest_day", return_value=False),
            mock.patch.object(run_story, "_predicted_regular_pillar", return_value="OTHER"),
        ):
            got = run_story._gate([], self._at(day, "13:29"), day, 200)
        assert got and "정기 발행" in got

    def test_gate_passes_when_regular_far(self):
        from src import run_story

        day = self._slot_day("G")   # 21:43
        with (
            mock.patch.object(antibot, "is_rest_day", return_value=False),
            mock.patch.object(run_story, "_predicted_regular_pillar", return_value="OTHER"),
        ):
            assert run_story._gate([], self._at(day, "13:29"), day, 200) is None


class TestReviewFixes:
    def test_chat_jitter_capped_at_window_end(self):
        # v1.4.0: 지연 상한 540 → 300초. 11:58 은 여유(330초)가 상한보다 커서
        # 자르지 않으므로, 상한이 실제로 잘리는 12:01(여유 150초)로 옮겼다.
        # v1.5.0: 창 끝(12:05) → 오전 구역 끝(12:21, 정기 B 12:26 − 5분). 12:16 은 여유 210초.
        now = dt.datetime(2026, 9, 28, 12, 16, tzinfo=KST)
        low, high = chat_plan.jitter_range(0, now)
        room = (5 * 60) - chat_plan.WINDOW_END_MARGIN_SEC
        assert high == max(low, room) and high < config.CHAT_JITTER[1]
        early = dt.datetime(2026, 9, 28, 9, 4, tzinfo=KST)
        assert chat_plan.jitter_range(0, early) == config.CHAT_JITTER

    def test_chat_jitter_gap_wait_not_cut(self):
        # v1.5.0: 12:00 → 12:18 (오전 구역 끝 12:21 까지 여유 90초 = 종료 여유와 같음)
        now = dt.datetime(2026, 9, 28, 12, 18, tzinfo=KST)
        low, high = chat_plan.jitter_range(300, now)
        assert low == 330 and high == low   # 간격 대기는 유지, 재검증이 구역 밖을 보류

    def test_repeated_candidate_used_when_retry_hard_fails(self):
        seq = ["요즘 반복된 글", "여러분 금지어"]
        with mock.patch.object(ai_writer, "generate", side_effect=seq):
            assert content._generate_with_ai("k", "MARKET", "s", ["요즘 전 글"]) == "요즘 반복된 글"

    def test_chat_repeated_candidate(self):
        from src import mood_source, run_chat

        mood = mood_source.Mood(source="none", themes=())
        with mock.patch.object(ai_writer, "generate", side_effect=["요즘 금리 얘기뿐", "3번"]):
            got = run_chat.generate_chat("k", "s", ["요즘 전 글"], mood)
        assert got == "요즘 금리 얘기뿐"

    def test_followups_skipped_when_conversation_fetch_failed(self):
        client = mock.Mock()
        client.get_reply_quota.return_value = Quota(0, 1000)
        client.get_my_posts.return_value = [_post("p1", 3), _post("p2", 3)]
        client.get_conversation.side_effect = [[], ThreadsApiError(500, "x")]
        with (
            mock.patch.object(config, "FOLLOWUP_ENABLED", True),
            mock.patch.object(run_reply, "_followups", return_value=0) as fu,
        ):
            run_reply.sweep(client, _settings(), per_run_cap=4, dry_run=True, now=NOW,
                            allow_followup=True)
        assert not fu.called

    def test_publish_timeout_worst_path(self):
        data = yaml.safe_load((WF / "publish.yml").read_text(encoding="utf-8"))
        timeout = data["jobs"]["publish"]["timeout-minutes"]
        prep_min = 5
        worst = (config.ANTIBOT_PUBLISH_JITTER[1] + 3 * config.CONTAINER_POLL_MAX_SEC) / 60
        assert timeout >= worst + prep_min

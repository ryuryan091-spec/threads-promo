"""v1.2.0 고도화 회귀 테스트 (2026-09-26 검토 F1~F7, M1·M3·M6, Q1~Q5, 위생).

항목 번호는 threads-promo/REVIEW_2026-09-26.md 와 같다.
"""

from __future__ import annotations

import datetime as dt
import os
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import test_chat_gate as _tcg
from test_chat_gate import DAY, _client, _first_selected, _kst, _run_chat

from src import (
    ai_writer,
    chat_plan,
    config,
    content,
    insights,
    reply_engine,
    run_reply,
    run_story,
    run_weighting,
    weighting,
)
from src.reply_engine import Comment
from src.threads_client import ContainerNotReadyError, Quota, ThreadsClient

KST = ZoneInfo("Asia/Seoul")
chat_env = _tcg.chat_env   # pytest fixture 재사용(모듈 네임스페이스에 등록)


def _trigger_now(trig: int) -> dt.datetime:
    hhmm = config.CHAT_TRIGGERS[trig - 1]
    return _kst(int(hhmm[:2]), int(hhmm[3:]))


# ---------------------------------------------------------------------------
# F1 · F7  run_chat
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("chat_env")
class TestRunChatGuards:
    def test_f1_disabled_returns_before_any_api(self):
        client = _client([])
        with mock.patch.object(config, "CHAT_ENABLED", False):
            code, gen, sweep = _run_chat(client, now=_kst(10, 7), trigger="T4")
        assert code == 0
        assert not client.get_my_posts.called
        assert not gen.called and not sweep.called

    def test_f1_disabled_manual_dry_run_still_previews(self):
        os.environ.update({"DRY_RUN": "true", "EVENT_NAME": "workflow_dispatch"})
        client = _client([])
        with mock.patch.object(config, "CHAT_ENABLED", False):
            code, gen, _ = _run_chat(client, now=_kst(10, 7), trigger="")
        assert code == 0
        assert gen.called
        assert not client.publish_text_post.called

    def test_f7_scheduled_dry_run_blocked_skips_generation(self):
        """예약 실행 + DRY_RUN + 게이트 보류 → Claude 호출 없음(비용 차단)."""
        os.environ["DRY_RUN"] = "true"
        client = _client([])
        # 창 밖(12:30) → 게이트 보류
        code, gen, sweep = _run_chat(client, now=_kst(12, 30), trigger="T9")
        assert code == 0
        assert not gen.called
        sweep.assert_called_once()

    def test_f7_scheduled_dry_run_open_gate_previews(self):
        os.environ["DRY_RUN"] = "true"
        trig = _first_selected(DAY)
        client = _client([])
        code, gen, _ = _run_chat(client, now=_trigger_now(trig), trigger=f"T{trig}")
        assert code == 0
        assert gen.called
        assert not client.publish_text_post.called

    def test_q1_q2_seed_and_closing_passed(self):
        trig = _first_selected(DAY)
        client = _client([])
        _, gen, _ = _run_chat(client, now=_trigger_now(trig), trigger=f"T{trig}")
        assert gen.call_args.args[2] == chat_plan.seed_for(
            DAY, trig, ai_writer.CHAT_PILLAR.seeds
        )
        assert gen.call_args.kwargs["closing"] == chat_plan.closing_for(DAY, trig)


# ---------------------------------------------------------------------------
# Q1 · Q2 · Q4  chat_plan
# ---------------------------------------------------------------------------


class TestChatPlanV11:
    def test_q1_no_duplicate_seed_in_a_day(self):
        seeds = ai_writer.CHAT_PILLAR.seeds
        for offset in range(365):
            day = DAY + dt.timedelta(days=offset)
            picked = [chat_plan.seed_for(day, t, seeds)
                      for t in range(1, len(config.CHAT_TRIGGERS) + 1)]
            picked.append(chat_plan.seed_for(day, None, seeds))
            assert len(set(picked)) == len(picked), day

    def test_q1_deterministic(self):
        seeds = ai_writer.CHAT_PILLAR.seeds
        assert chat_plan.seed_for(DAY, 3, seeds) == chat_plan.seed_for(DAY, 3, seeds)

    def test_q1_empty_seeds_raises(self):
        with pytest.raises(ValueError):
            chat_plan.seed_for(DAY, 1, ())

    def test_q2_closing_ratio(self):
        days = [DAY + dt.timedelta(days=i) for i in range(365)]
        values = [chat_plan.closing_for(d, t) for d in days for t in range(1, 10)]
        ratio = values.count("question") / len(values)
        assert 0.50 <= ratio <= 0.60
        assert set(values) == {"question", "statement"}

    def test_q4_weekend_bounds(self):
        saturday = dt.date(2026, 9, 26)
        monday = dt.date(2026, 9, 28)
        assert chat_plan.is_weekend(saturday) and not chat_plan.is_weekend(monday)
        assert chat_plan.daily_bounds(saturday) == (
            config.CHAT_WEEKEND_MIN, config.CHAT_WEEKEND_MAX
        )
        assert chat_plan.daily_bounds(monday) == (config.CHAT_DAILY_MIN, config.CHAT_DAILY_MAX)
        assert chat_plan.daily_bounds() == (config.CHAT_DAILY_MIN, config.CHAT_DAILY_MAX)

    def test_q4_weekend_zero_blocks(self):
        saturday = dt.date(2026, 9, 26)
        with (
            mock.patch.object(config, "CHAT_WEEKEND_MIN", 0),
            mock.patch.object(config, "CHAT_WEEKEND_MAX", 0),
        ):
            assert chat_plan.daily_target(saturday) == 0
            assert chat_plan.selected_triggers(saturday) == ()
            now = _kst(10, 7, saturday)
            counts = chat_plan.PostCounts(0, None)
            got = chat_plan.gate(saturday, now, 4, counts, manual=False)
            assert got and "목표 0건" in got

    def test_q4_weekend_target_over_year(self):
        for offset in range(365):
            day = DAY + dt.timedelta(days=offset)
            n = chat_plan.daily_target(day)
            if chat_plan.is_weekend(day):
                assert config.CHAT_WEEKEND_MIN <= n <= config.CHAT_WEEKEND_MAX
            else:
                assert config.CHAT_DAILY_MIN <= n <= config.CHAT_DAILY_MAX


# ---------------------------------------------------------------------------
# Q2 · Q3  ai_writer / content
# ---------------------------------------------------------------------------


class TestClosingPrompt:
    def test_statement_prompt_has_no_question_instruction(self):
        pillar = ai_writer.get_pillar("MARKET")
        prompt = ai_writer._build_user_prompt(
            pillar, "씨앗", [], "", ai_writer.CLOSING_STATEMENT
        )
        assert "질문 없이 닫습니다" in prompt
        assert "질문으로 닫" not in prompt
        assert "질문으로 닫" not in ai_writer.SYSTEM_PROMPT

    def test_question_prompt(self):
        pillar = ai_writer.get_pillar("STORY")
        prompt = ai_writer._build_user_prompt(pillar, "씨앗", [], "# 기록\n- a")
        assert "질문 한 문장으로 닫습니다" in prompt

    @pytest.mark.parametrize("key", ["MARKET", "STORY", "PROMO", "CHAT"])
    def test_briefs_do_not_force_question(self, key):
        pillar = ai_writer.get_pillar(key)
        assert "질문으로 닫" not in pillar.brief
        assert "질문으로 닫" not in (pillar.evidence_note or "")

    def test_generate_sends_closing(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"content": [{"type": "text", "text": '{"text": "본문"}'}]}
        with mock.patch.object(ai_writer.requests, "post", return_value=resp) as post:
            ai_writer.generate("k", "MARKET", "씨앗", closing=ai_writer.CLOSING_STATEMENT)
        body = post.call_args.kwargs["json"]["messages"][0]["content"]
        assert "질문 없이 닫습니다" in body

    def test_closing_pattern_ratio(self):
        values = [content.closing_for(i) for i in range(1000)]
        ratio = values.count(ai_writer.CLOSING_QUESTION) / len(values)
        assert ratio == pytest.approx(0.6, abs=0.01)

    def test_closing_mixes_with_pillars(self):
        """마무리 패턴(5)과 로테이션(8)이 서로소 — 모든 기둥이 두 마무리를 다 겪는다."""
        seen = {(ai_writer.pick_pillar(i), content.closing_for(i)) for i in range(40)}
        for pillar in set(ai_writer.PILLAR_ROTATION):
            assert (pillar, ai_writer.CLOSING_QUESTION) in seen
            assert (pillar, ai_writer.CLOSING_STATEMENT) in seen

    def test_q3_reply_text_rotates_and_lints(self):
        texts = [content.build_reply_text(i) for i in range(len(config.REPLY_LEADS))]
        assert len(set(texts)) == len(config.REPLY_LEADS)
        for t in texts:
            content.lint(t)
            assert config.YOUTUBE_URL in t and config.X_URL in t

    def test_q3_default_is_backward_compatible(self):
        assert content.build_reply_text().startswith(config.REPLY_LEADS[0])

    def test_build_plan_forced_pillar(self, tmp_path):
        (tmp_path / "promo_01.png").write_bytes(b"x")
        with mock.patch.object(config, "AI_ENABLED", False):
            plan = content.build_plan(
                dt.date(2026, 9, 26), tmp_path, "https://raw.example/assets",
                discriminator=config.DISCRIMINATOR_EVENT, pillar="STORY",
            )
        assert plan.pillar == "STORY"
        assert plan.seed in ai_writer.get_pillar("STORY").seeds

    def test_build_plan_passes_closing(self, tmp_path):
        (tmp_path / "promo_01.png").write_bytes(b"x")
        day = dt.date(2026, 9, 26)
        with (
            mock.patch.object(config, "AI_ENABLED", True),
            mock.patch.object(ai_writer, "generate", return_value="담담한 본문입니다.") as gen,
        ):
            content.build_plan(day, tmp_path, "https://raw.example/assets",
                               claude_api_key="k", discriminator=0)
        idx = content.run_index(day, 0)
        assert gen.call_args.kwargs["closing"] == content.closing_for(idx)


# ---------------------------------------------------------------------------
# M3  AUTO 로테이션
# ---------------------------------------------------------------------------

AUTO_OK = "STORY,MARKET,STORY,PROMO,MARKET,STORY,MARKET,PROMO"


class TestAutoRotation:
    def test_auto_valid(self):
        assert not ai_writer.rotation_violations(tuple(AUTO_OK.split(",")))

    def test_precedence_override_over_auto(self):
        with (
            mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", "MARKET,STORY,PROMO,STORY"),
            mock.patch.object(config, "PILLAR_ROTATION_AUTO", AUTO_OK),
        ):
            assert ai_writer.active_rotation() == ("MARKET", "STORY", "PROMO", "STORY")

    def test_auto_used_when_no_override(self):
        with (
            mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", ""),
            mock.patch.object(config, "PILLAR_ROTATION_AUTO", AUTO_OK),
        ):
            assert ai_writer.active_rotation() == tuple(AUTO_OK.split(","))
            assert weighting.current_rotation() == tuple(AUTO_OK.split(","))

    def test_invalid_auto_falls_back(self):
        # PROMO 3칸 = S5 위반
        bad = "PROMO,STORY,PROMO,MARKET,PROMO,STORY,MARKET,STORY"
        with (
            mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", ""),
            mock.patch.object(config, "PILLAR_ROTATION_AUTO", bad),
        ):
            assert ai_writer.active_rotation() == ai_writer.PILLAR_ROTATION

    def test_invalid_override_falls_to_auto(self):
        with (
            mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", "NOPE"),
            mock.patch.object(config, "PILLAR_ROTATION_AUTO", AUTO_OK),
        ):
            assert ai_writer.active_rotation() == tuple(AUTO_OK.split(","))

    def test_gate_not_blocked_by_auto(self):
        with (
            mock.patch.object(config, "ADAPTIVE_WEIGHTS_ENABLED", True),
            mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", ""),
            mock.patch.object(config, "PILLAR_ROTATION_AUTO", AUTO_OK),
        ):
            ok, reason, _ = weighting._gate([], dt.date(2026, 9, 26), "")
        assert "수동 로테이션" not in reason

    def test_gate_blocked_by_override(self):
        with (
            mock.patch.object(config, "ADAPTIVE_WEIGHTS_ENABLED", True),
            mock.patch.object(config, "PILLAR_ROTATION_OVERRIDE", AUTO_OK),
        ):
            ok, reason, _ = weighting._gate([], dt.date(2026, 9, 26), "")
        assert not ok and "수동 로테이션" in reason

    def test_validate_delegates(self):
        rot = tuple("STORY,STORY,MARKET,PROMO".split(","))
        assert weighting.validate_rotation(rot) == ai_writer.rotation_violations(rot)

    def test_run_weighting_reports_auto_variable(self):
        result = weighting.AdjustResult(
            adjusted=True, reason="", before=ai_writer.PILLAR_ROTATION,
            after=tuple(AUTO_OK.split(",")), promoted="STORY", demoted="PROMO",
        )
        client = mock.Mock()
        client.get_user_insights.return_value = {"data": []}
        client.get_my_posts.return_value = []
        env = {"THREADS_APP_ID": "1", "THREADS_APP_SECRET": "s",
               "THREADS_LONG_LIVED_TOKEN": "t", "THREADS_USER_ID": "123456789012345"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(run_weighting, "ThreadsClient", return_value=client),
            mock.patch.object(run_weighting, "collect_post_stats", return_value=([], 0)),
            mock.patch.object(run_weighting.weighting, "decide", return_value=result),
            mock.patch.object(run_weighting.notifier, "send") as send,
        ):
            assert run_weighting.run() == 0
        report = send.call_args.args[2]
        assert "PILLAR_ROTATION_AUTO = " + AUTO_OK in report
        assert "PILLAR_ROTATION_OVERRIDE =" not in report
        assert "계정 링크 클릭 합계" in report


# ---------------------------------------------------------------------------
# F3 · 위생  run_story / insights
# ---------------------------------------------------------------------------


def _ts(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


class TestStoryV12:
    def test_event_window_restored_as_story(self):
        when = dt.datetime(2026, 9, 26, 13, 45, tzinfo=KST)
        assert insights.restore_pillar(when, "IMAGE") == "STORY"

    def test_story_published_today_detects_event_post(self):
        now = dt.datetime(2026, 9, 26, 18, 0, tzinfo=KST)
        posts = [{"id": "e", "timestamp": _ts(dt.datetime(2026, 9, 26, 13, 40, tzinfo=KST)),
                  "media_type": "IMAGE"}]
        assert run_story._story_published_today(posts, now)

    def test_story_published_today_ignores_chat_and_yesterday(self):
        now = dt.datetime(2026, 9, 26, 18, 0, tzinfo=KST)
        posts = [
            {"id": "c", "timestamp": _ts(dt.datetime(2026, 9, 26, 10, 0, tzinfo=KST)),
             "media_type": "TEXT_POST"},
            {"id": "y", "timestamp": _ts(dt.datetime(2026, 9, 25, 13, 40, tzinfo=KST)),
             "media_type": "IMAGE"},
        ]
        assert not run_story._story_published_today(posts, now)

    @pytest.mark.parametrize("hh,mm,expected", [
        (3, 11, 7.2),      # 23:17 → 03:11 = 3.9h × 1.2 < 7.2 → 하한
        (13, 29, 12.36),   # 03:11 → 13:29 = 10.3h × 1.2
        (14, 10, 13.04),   # cron 41분 지연분 포함
        (17, 41, 7.2),
        (23, 17, 7.2),
        (0, 5, 7.52),      # 자정 넘김: 17:41 → 23:17 = 5.6h × 1.2 + 48분
    ])
    def test_event_window_hours(self, hh, mm, expected):
        now = dt.datetime(2026, 9, 27, hh, mm, tzinfo=KST)
        assert run_story.event_window_hours(now) == pytest.approx(expected)

    def test_event_window_covers_all_gaps(self):
        """각 cron 의 창이 직전 cron 까지 거슬러 올라간다(누락 구간 0)."""
        marks = sorted(insights.EVENT_SLOTS)
        for i, hhmm in enumerate(marks):
            now = dt.datetime(2026, 9, 27, int(hhmm[:2]), int(hhmm[3:]), tzinfo=KST)
            prev = marks[i - 1]
            prev_dt = dt.datetime(2026, 9, 27, int(prev[:2]), int(prev[3:]), tzinfo=KST)
            if prev_dt >= now:
                prev_dt -= dt.timedelta(days=1)
            gap_h = (now - prev_dt).total_seconds() / 3600
            assert run_story.event_window_hours(now) >= gap_h

    def test_run_forces_story_pillar(self):
        """run() 이 build_plan(pillar='STORY') 로 호출하고, AI 실패(정적)면 보류한다."""
        plan = mock.Mock(pillar="STORY", seed="s", source="static")
        client = mock.Mock()
        client.get_post_quota.return_value = Quota(used=0, total=250)
        client.get_my_posts.return_value = []
        client.get_recent_texts.return_value = []
        settings = mock.Mock(can_fetch_episodes=True, threads_user_id="1",
                             dry_run=False, claude_api_key="k", notion_token="n")
        with (
            mock.patch.object(config, "EVENT_STORY_ENABLED", True),
            mock.patch.object(run_story, "_preflight"),
            mock.patch.object(run_story, "load_settings", return_value=settings),
            mock.patch.object(run_story.notion_source, "fetch_new_episodes",
                              return_value=["Ep.1 빌런 등장"]),
            mock.patch.object(run_story, "_acquire_token", return_value="tok"),
            mock.patch.object(run_story, "ThreadsClient", return_value=client),
            mock.patch.object(run_story, "_gate", return_value=None),
            mock.patch.object(run_story, "_resolve_raw_base_url", return_value="https://r"),
            mock.patch.object(run_story.content, "build_plan", return_value=plan) as bp,
        ):
            assert run_story.run() == 0
        assert bp.call_args.kwargs["pillar"] == "STORY"
        assert not client.publish_image_post.called
        assert not client.publish_text_post.called


# ---------------------------------------------------------------------------
# F4  대화 페이지네이션
# ---------------------------------------------------------------------------


class TestConversationPaging:
    @staticmethod
    def _page(ids, after=None):
        body = {"data": [{"id": i} for i in ids]}
        if after:
            body["paging"] = {"cursors": {"after": after}}
        return body

    def test_merges_pages(self):
        pages = [self._page(["a", "b"], "c1"), self._page(["c"], None)]
        with mock.patch("src.threads_client._request", side_effect=pages) as req:
            items = ThreadsClient("1", "t").get_conversation("p", 100)
        assert [i["id"] for i in items] == ["a", "b", "c"]
        assert req.call_args_list[1].kwargs["params"]["after"] == "c1"
        assert req.call_args_list[0].kwargs["params"]["limit"] == config.CONVERSATION_PAGE_SIZE

    def test_stops_at_limit(self):
        pages = [self._page([f"x{i}" for i in range(25)], "c1")]
        with mock.patch("src.threads_client._request", side_effect=pages) as req:
            items = ThreadsClient("1", "t").get_conversation("p", 25)
        assert len(items) == 25 and req.call_count == 1

    def test_stops_on_empty_batch(self):
        pages = [self._page(["a"], "c1"), self._page([], "c2")]
        with mock.patch("src.threads_client._request", side_effect=pages) as req:
            items = ThreadsClient("1", "t").get_conversation("p", 100)
        assert len(items) == 1 and req.call_count == 2

    def test_page_cap_warns(self, caplog):
        pages = [self._page([f"{n}-{i}" for i in range(25)], f"c{n}")
                 for n in range(config.CONVERSATION_MAX_PAGES + 2)]
        with mock.patch("src.threads_client._request", side_effect=pages) as req:
            items = ThreadsClient("1", "t").get_conversation("p", 1000)
        assert req.call_count == config.CONVERSATION_MAX_PAGES
        assert len(items) == 25 * config.CONVERSATION_MAX_PAGES
        assert "페이지 상한" in caplog.text

    def test_duplicate_reply_prevented_beyond_first_page(self):
        """내 답글이 26번째 이후에 있어도 '이미 답글함'으로 판정된다."""
        first = [{"id": f"o{i}", "text": "다른 댓글입니다", "username": f"u{i}",
                  "replied_to": {"id": "p1"}} for i in range(25)]
        target = {"id": "c1", "text": "한국어 댓글입니다", "username": "alice",
                  "replied_to": {"id": "p1"}}
        mine = {"id": "m1", "text": "답글", "username": "me", "replied_to": {"id": "c1"},
                "is_reply_owned_by_me": True}
        pages = [{"data": [target, *first[:24]], "paging": {"cursors": {"after": "x"}}},
                 {"data": [first[24], mine]}]
        with mock.patch("src.threads_client._request", side_effect=pages):
            raw = ThreadsClient("1", "t").get_conversation("p1", config.REPLY_SCAN_LIMIT)
        comments = [run_reply._parse_comment(r) for r in raw]
        assert "c1" in run_reply._already_replied_ids(comments)


# ---------------------------------------------------------------------------
# F5 · F6 · Q5  run_reply / reply_engine
# ---------------------------------------------------------------------------


def _cm(cid, *, parent, mine=False, user="alice"):
    return Comment(id=cid, text="한국어 댓글입니다", username=user, timestamp="",
                   replied_to_id=parent, owned_by_me=mine, hide_status="NOT_HUSHED")


class TestReplyV12:
    TARGETS = frozenset({"post1", "mine1"})

    def test_q5_reply_to_root_allowed(self):
        d = reply_engine.decide(_cm("c1", parent="post1"), already_replied=False,
                                author_used=0, reply_target_ids=self.TARGETS)
        assert d.strategy is not reply_engine.ReplyStrategy.SKIP

    def test_q5_reply_to_my_reply_allowed(self):
        d = reply_engine.decide(_cm("c2", parent="mine1"), already_replied=False,
                                author_used=0, reply_target_ids=self.TARGETS)
        assert d.strategy is not reply_engine.ReplyStrategy.SKIP

    def test_q5_third_party_thread_skipped(self):
        d = reply_engine.decide(_cm("c3", parent="other_comment"), already_replied=False,
                                author_used=0, reply_target_ids=self.TARGETS)
        assert d.strategy is reply_engine.ReplyStrategy.SKIP
        assert d.reason == "제3자 간 대화"

    def test_q5_missing_parent_keeps_old_behavior(self):
        d = reply_engine.decide(_cm("c4", parent=""), already_replied=False,
                                author_used=0, reply_target_ids=self.TARGETS)
        assert d.strategy is not reply_engine.ReplyStrategy.SKIP

    @staticmethod
    def _sweep(conv, publish_effect=None):
        from src.env import load_settings

        now = dt.datetime.now(dt.UTC)
        ts = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        client = mock.Mock()
        client.get_reply_quota.return_value = Quota(used=0, total=1000)
        client.get_my_posts.return_value = [{"id": "p1", "text": "원글", "timestamp": ts}]
        client.get_conversation.return_value = [dict(c, timestamp=ts) for c in conv]
        if publish_effect:
            client.publish_self_reply.side_effect = publish_effect
        else:
            client.publish_self_reply.return_value = "r"
        env = {"THREADS_APP_ID": "1", "THREADS_APP_SECRET": "s",
               "THREADS_LONG_LIVED_TOKEN": "t", "CLAUDE_AI_KEY": "k", "DRY_RUN": "false"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("src.reply_engine._generate_reply", return_value="답글입니다"),
            mock.patch("src.antibot.time.sleep"),
            mock.patch.object(run_reply.notifier, "send") as send,
        ):
            sent = run_reply.sweep(client, load_settings(), per_run_cap=4, dry_run=False)
        return sent, client, send

    @staticmethod
    def _raw(cid, parent, user, mine=False):
        return {"id": cid, "text": "한국어 댓글입니다", "username": user,
                "replied_to": {"id": parent}, "is_reply_owned_by_me": mine,
                "hide_status": "NOT_HUSHED"}

    def test_f5_container_not_ready_continues(self):
        conv = [self._raw("c1", "p1", "alice"), self._raw("c2", "p1", "bob")]
        effect = [ContainerNotReadyError("timeout"), "r2"]
        sent, client, send = self._sweep(conv, publish_effect=effect)
        assert client.publish_self_reply.call_count == 2
        assert sent == 1
        assert "발행 실패" in send.call_args.args[2]

    def test_q5_sweep_skips_third_party(self):
        conv = [
            self._raw("c1", "p1", "alice"),            # 원글 댓글 → 응답
            self._raw("c2", "c1", "bob"),              # alice 댓글에 bob → 제3자 대화
        ]
        sent, client, _ = self._sweep(conv)
        targets = [c.args[0] for c in client.publish_self_reply.call_args_list]
        assert targets == ["c1"] and sent == 1

    def test_f6_scheduled_cap_fits_timeout(self):
        worst = (config.REPLY_SCHEDULED_RUN_CAP - 1) * config.ANTIBOT_REPLY_JITTER[1]
        assert worst < 15 * 60    # reply.yml timeout 20분, 생성·조회 여유 5분


# ---------------------------------------------------------------------------
# M6  워치독
# ---------------------------------------------------------------------------


class TestWatchdogV12:
    def test_link_self_reply_excluded(self):
        from src import run_watchdog

        client = mock.Mock()
        client.get_conversation.return_value = [
            {"id": "m1", "is_reply_owned_by_me": True, "replied_to": {"id": "p1"},
             "timestamp": "2026-09-26T00:00:00+0000"},                     # 링크 리플
            {"id": "m2", "is_reply_owned_by_me": True, "replied_to": {"id": "c9"},
             "timestamp": "2026-09-25T00:00:00+0000"},                     # 엔진 답글
        ]
        stamps = run_watchdog._collect_owned_reply_stamps(client, [{"id": "p1"}])
        assert len(stamps) == 1
        assert stamps[0].day == 25
        assert client.get_conversation.call_args.args[1] == config.REPLY_SCAN_LIMIT


# ---------------------------------------------------------------------------
# M1  워크플로 기본값
# ---------------------------------------------------------------------------


class TestWorkflowDefaults:
    @staticmethod
    def _body(name):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / ".github" / "workflows" / name).read_text(
            encoding="utf-8"
        )

    def test_m1_insights_limit_matches_config_default(self):
        assert "INSIGHTS_POST_LIMIT: ${{ vars.INSIGHTS_POST_LIMIT || '70' }}" in self._body(
            "insights.yml"
        )

    def test_bootstrap_inputs_not_inlined_in_run(self):
        import yaml

        data = yaml.safe_load(self._body("bootstrap.yml"))
        for job in data["jobs"].values():
            for step in job["steps"]:
                assert "${{ inputs." not in str(step.get("run", ""))

    @pytest.mark.parametrize("name", ["publish.yml", "story.yml", "insights.yml",
                                      "weighting.yml", "golive_check.yml"])
    def test_auto_rotation_env_present(self, name):
        assert "PILLAR_ROTATION_AUTO: ${{ vars.PILLAR_ROTATION_AUTO }}" in self._body(name)


class TestVerifyRepoDryRun:
    def test_canonical_expression_semantics(self):
        """GitHub 식 의미론(&&/|| 가 피연산자 값 반환, '' 만 거짓)으로 표준 식을 평가한다."""

        def evaluate(event, mode, var):
            def truthy(v):
                return v not in ("", None, False)

            def and_(a, b):
                return b if truthy(a) else a

            def or_(a, b):
                return a if truthy(a) else b

            inner = or_(and_(mode == "live", "false"), "true")
            left = and_(event == "workflow_dispatch", inner)
            return or_(left, or_(var, "true"))

        assert evaluate("workflow_dispatch", "dry_run", "false") == "true"
        assert evaluate("workflow_dispatch", "live", "true") == "false"
        assert evaluate("workflow_dispatch", "", "false") == "true"
        assert evaluate("schedule", None, "false") == "false"
        assert evaluate("schedule", None, "") == "true"

    def test_old_expression_leaked(self):
        """참고: 이전 식은 수동 dry_run 에서 vars.DRY_RUN('false') 로 떨어졌다."""

        def old(event, mode, var):
            first = "false" if (event == "workflow_dispatch" and mode == "live") else ""
            return first or var or "true"

        assert old("workflow_dispatch", "dry_run", "false") == "false"



# ---------------------------------------------------------------------------
# 독립 리뷰 반영 (R1 이벤트 창·자정, R2 스윕 시간 예산, R3 휴식일, R4 문구 짝)
# ---------------------------------------------------------------------------


class TestReviewFixes:
    @pytest.mark.parametrize("hh,mm,expected", [
        (4, 10, "STORY"),    # 03:11 + 지터 50분 + 지연 → 이전 55분 창 밖(판정불가)
        (0, 5, "STORY"),     # 23:17 이벤트가 자정을 넘긴 경우
        (8, 50, None),       # 정기 A 08:23 + 27분 지연 → 정기로 복원(판정불가 아님)
    ])
    def test_r1_windows(self, hh, mm, expected):
        when = dt.datetime(2026, 9, 28, hh, mm, tzinfo=KST)
        got = insights.restore_pillar(when, "IMAGE")
        if expected:
            assert got == expected
        else:
            assert got not in (insights.UNKNOWN, insights.CHAT)

    def test_r1_windows_do_not_overlap(self):
        """정기·이벤트·CHAT 창이 서로 겹치지 않는다(분 단위 전수)."""
        owners: dict[int, str] = {}
        for hhmm in insights.PUBLISH_SLOTS:
            start = int(hhmm[:2]) * 60 + int(hhmm[3:])
            for m in range(insights.PUBLISH_WINDOW_MIN + 1):
                key = (start + m) % 1440
                assert key not in owners, (hhmm, m)
                owners[key] = "P"
        for hhmm in insights.EVENT_SLOTS:
            start = int(hhmm[:2]) * 60 + int(hhmm[3:])
            for m in range(insights.EVENT_WINDOW_MIN + 1):
                key = (start + m) % 1440
                assert key not in owners, (hhmm, m)
                owners[key] = "E"
        for key in owners:
            when = dt.datetime(2026, 9, 28, key // 60, key % 60, tzinfo=KST)
            assert not chat_plan.is_chat_time(when)

    def test_r1_recheck_uses_date_after_jitter(self):
        """지터 중 자정을 넘기면 재검증은 새 날짜 기준으로 한다."""
        src = open(run_story.__file__, encoding="utf-8").read()
        assert "recheck_today = recheck_now.astimezone(KST).date()" in src
        assert "fresh_posts, recheck_now, recheck_today" in src

    def test_r3_rest_day_skips_event(self):
        settings = mock.Mock(can_fetch_episodes=True)
        with (
            mock.patch.object(config, "EVENT_STORY_ENABLED", True),
            mock.patch.object(run_story, "_preflight"),
            mock.patch.object(run_story, "load_settings", return_value=settings),
            mock.patch.object(run_story.antibot, "is_rest_day", return_value=True),
            mock.patch.object(run_story.notion_source, "fetch_new_episodes") as fetch,
        ):
            assert run_story.run() == 0
        assert not fetch.called

    def test_r4_lead_and_closing_not_locked(self):
        pairs = {(content.build_reply_text(i).split("\n")[0], content.closing_for(i))
                 for i in range(60)}
        assert len(pairs) > len(config.REPLY_LEADS)

    # R2 스윕 시간 예산 ---------------------------------------------------

    @staticmethod
    def _budget_sweep(budget, clock):
        from src.env import load_settings

        now = dt.datetime.now(dt.UTC)
        ts = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        conv = [{"id": f"c{i}", "text": "한국어 댓글입니다", "username": f"u{i}",
                 "timestamp": ts, "replied_to": {"id": "p1"},
                 "is_reply_owned_by_me": False, "hide_status": "NOT_HUSHED"}
                for i in range(6)]
        client = mock.Mock()
        client.get_reply_quota.return_value = Quota(used=0, total=1000)
        client.get_my_posts.return_value = [{"id": "p1", "text": "원글", "timestamp": ts}]
        client.get_conversation.return_value = conv
        client.publish_self_reply.return_value = "r"
        env = {"THREADS_APP_ID": "1", "THREADS_APP_SECRET": "s",
               "THREADS_LONG_LIVED_TOKEN": "t", "CLAUDE_AI_KEY": "k", "DRY_RUN": "false"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("src.reply_engine._generate_reply", return_value="답글입니다"),
            mock.patch("src.antibot.time.sleep"),
            mock.patch.object(run_reply.time, "monotonic", side_effect=clock),
            mock.patch.object(run_reply.notifier, "send"),
        ):
            return run_reply.sweep(client, load_settings(), per_run_cap=6,
                                   dry_run=False, budget_sec=budget)

    def test_r2_budget_stops_new_replies(self):
        # 시작 0초, 이후 매 확인마다 200초씩 흐른 것으로 본다.
        ticks = iter([0, 0, 200, 400, 600, 800, 1000, 1200])
        sent = self._budget_sweep(900, lambda: next(ticks))
        # 최악 1건 = 150 + 300 + 5 + 40 = 495초(첫 건은 지연 없음 345초)
        #   0+345 ≤ 900 발행 / 200+495 ≤ 900 발행 / 400+495 ≤ 900 발행 / 600+495 > 900 중단
        assert sent == 3

    def test_r2_no_budget_unlimited(self):
        sent = self._budget_sweep(None, lambda: 0)
        assert sent == 6

    def test_r2_zero_budget_sends_nothing(self):
        sent = self._budget_sweep(0, lambda: 0)
        assert sent == 0

    def test_r2_run_passes_budget(self):
        os.environ.update({"REPLY_SLOTS": "A,B,C", "SLOT": "A", "EVENT_NAME": "schedule",
                           "THREADS_APP_ID": "1", "THREADS_APP_SECRET": "s",
                           "THREADS_LONG_LIVED_TOKEN": "t"})
        with (
            mock.patch.object(run_reply, "sweep", return_value=0) as sweep,
            mock.patch.object(run_reply, "_acquire_token", return_value="tok"),
            mock.patch.object(run_reply, "fetch_user_id", return_value=("1", "u")),
            mock.patch.object(run_reply, "ThreadsClient"),
        ):
            assert run_reply.run() == 0
        assert sweep.call_args.kwargs["budget_sec"] == config.REPLY_SWEEP_BUDGET_SEC

    def test_r2_budget_fits_workflow_timeouts(self):
        from pathlib import Path

        import yaml

        wf = Path(run_reply.__file__).resolve().parents[1] / ".github" / "workflows"
        reply_timeout = yaml.safe_load((wf / "reply.yml").read_text())["jobs"]
        reply_min = next(iter(reply_timeout.values()))["timeout-minutes"]
        assert config.REPLY_SWEEP_BUDGET_SEC <= (reply_min - 5) * 60
        chat_jobs = yaml.safe_load((wf / "chat.yml").read_text())["jobs"]
        chat_min = next(iter(chat_jobs.values()))["timeout-minutes"]
        assert config.CHAT_JOB_BUDGET_SEC <= (chat_min - 5) * 60

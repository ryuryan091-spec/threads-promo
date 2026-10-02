"""v1.5.0 CHAT 창 09:00~24:00 검증 — CHAT 구역 · 트리거 재배치 · 시간대 소재 · 워치독 · 시뮬레이션.

운영 시뮬레이션은 tests/test_chat_v14.py 모델(실제 chat_plan.gate·gap_wait_seconds·jitter_range,
run_chat.sweep_budget_sec 사용)에 두 가지를 더했다.
  - 정기 글: 그날 당첨 슬롯(antibot.choose_slot) 1건. CHAT 최소 간격은 종류 무관 직전 글 기준이다.
  - reply.yml 실행: 같은 concurrency 그룹(threads-reply)을 점유한다. 슬롯 시각 + cron 지연 →
    준비 45~90초 + 시작 지연 REPLY_START_JITTER(0~600초) + 스윕(시나리오별, 예산 25분 상한).
그룹 규칙: 실행 중 1건 + 대기 1건. 대기 중에 새 실행이 들어오면 기존 대기 실행이 취소된다
(cancel-in-progress: false — 실행 중인 것은 취소되지 않는다).
누락률·지연·스윕 소요는 가정값이다(실측 아님). 결과 수치는 DESIGN_V15_CHAT_WINDOW.md 에 기록했다.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import test_chat_gate as _tcg
import yaml
from test_chat_gate import _client, _run_chat

from src import (
    ai_writer,
    antibot,
    chat_plan,
    config,
    content,
    insights,
    mood_source,
    run_chat,
    run_story,
    run_weighting,
    watchdog,
)

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
DAY = dt.date(2026, 10, 5)   # 월요일
YEAR = [DAY + dt.timedelta(days=i) for i in range(365)]
chat_env = _tcg.chat_env     # pytest fixture 재사용

EXPECTED_ZONES = (
    ("09:00", "12:21"), ("15:55", "16:31"), ("19:12", "19:48"),
    ("20:41", "21:38"), ("22:31", "23:12"),
)


def _mark(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _kst(hhmm: str, day: dt.date = DAY, sec: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(0), tzinfo=KST) + dt.timedelta(
        minutes=_mark(hhmm), seconds=sec
    )


def _crons(name: str) -> list[str]:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    on = data.get(True) or data.get("on") or {}
    return [c["cron"] for c in on.get("schedule") or []]


def _utc_cron_to_kst_minute(cron: str) -> int:
    minute, hour = (int(x) for x in cron.split()[:2])
    return ((hour + 9) % 24) * 60 + minute


def _post(when: dt.datetime, media_type: str = "TEXT_POST", pid: str = "p") -> dict:
    return {"id": pid, "media_type": media_type,
            "timestamp": when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")}


# ---------------------------------------------------------------------------
# CHAT 구역 · 예약 구간
# ---------------------------------------------------------------------------


class TestZones:
    def test_window_constants(self):
        assert (config.CHAT_WINDOW_START, config.CHAT_WINDOW_END) == ("09:00", "24:00")
        assert config.CHAT_RESERVED_MARGIN_MIN == 5
        assert config.CHAT_MIN_ZONE_MIN == 30

    def test_zone_table(self):
        got = tuple((chat_plan.fmt_minutes(a), chat_plan.fmt_minutes(b))
                    for a, b in chat_plan.chat_zones())
        assert got == EXPECTED_ZONES

    def test_short_gaps_dropped(self):
        short = [z for z in chat_plan.free_zones() if z not in chat_plan.chat_zones()]
        assert [(chat_plan.fmt_minutes(a), chat_plan.fmt_minutes(b)) for a, b in short] == [
            ("13:14", "13:24"), ("15:00", "15:02"), ("17:24", "17:36"),
        ]
        assert all(b - a < config.CHAT_MIN_ZONE_MIN for a, b in short)

    def test_event_slots_single_source(self):
        assert insights.EVENT_SLOTS == config.EVENT_SLOT_TIMES
        assert insights.EVENT_WINDOW_MIN == config.EVENT_CLASSIFY_WINDOW_MIN == 90
        assert insights.PUBLISH_WINDOW_MIN == config.PUBLISH_CLASSIFY_WINDOW_MIN

    def test_every_classify_minute_is_not_chat(self):
        """정기·이벤트 판정 창의 모든 분과 앞 여유 5분은 CHAT 시각이 아니다(예약 우선)."""
        for day in (DAY, DAY + dt.timedelta(days=5)):
            for hhmm in insights.PUBLISH_SLOTS:
                start = _kst(hhmm, day)
                for m in range(-config.CHAT_RESERVED_MARGIN_MIN, insights.PUBLISH_WINDOW_MIN + 1):
                    assert not chat_plan.is_chat_time(start + dt.timedelta(minutes=m)), (hhmm, m)
            for hhmm in insights.EVENT_SLOTS:
                start = _kst(hhmm, day)
                for m in range(-config.CHAT_RESERVED_MARGIN_MIN, insights.EVENT_WINDOW_MIN + 1):
                    assert not chat_plan.is_chat_time(start + dt.timedelta(minutes=m)), (hhmm, m)

    def test_margin_boundary(self):
        # 정기 B 12:26 → 예약 시작 12:21. 12:20:59 는 CHAT, 12:21:00 은 아니다.
        assert chat_plan.is_chat_time(_kst("12:20", sec=59))
        assert not chat_plan.is_chat_time(_kst("12:21"))

    def test_never_crosses_midnight(self):
        assert not chat_plan.is_chat_time(_kst("23:59"))
        assert not chat_plan.is_chat_time(_kst("00:00"))
        assert not chat_plan.is_chat_time(_kst("08:59"))

    def test_seconds_left_in_zone(self):
        assert chat_plan.seconds_left_in_zone(_kst("16:30")) == 60
        assert chat_plan.seconds_left_in_zone(_kst("16:31")) is None


class TestReservedPrecedence:
    def test_text_fallback_regular_is_not_chat(self):
        """판정 창 안 텍스트 폴백 정기 글(15:30, E 15:07 창)은 CHAT 이 아니라 슬롯 기둥이다."""
        when = _kst("15:30")
        assert not chat_plan.is_chat_post(when, "TEXT_POST")
        pillar = insights.restore_pillar(when, "TEXT_POST")
        assert pillar not in (insights.CHAT, insights.UNKNOWN)

    def test_restore_prefers_reserved_even_if_chat_rule_breaks(self):
        """이중 장치: CHAT 판정이 잘못 True 여도 판정 창이 이긴다."""
        when = _kst("15:30")
        with mock.patch.object(chat_plan, "is_chat_post", return_value=True):
            assert insights.restore_pillar(when, "TEXT_POST") not in (insights.CHAT, insights.UNKNOWN)

    def test_chat_zone_text_is_chat(self):
        assert insights.restore_pillar(_kst("20:45"), "TEXT_POST") == insights.CHAT
        assert insights.restore_pillar(_kst("22:40"), "TEXT_POST") == insights.CHAT

    def test_event_text_post_detected_as_story(self):
        """이벤트 창(13:29 + 90분) 안 텍스트 STORY 는 'STORY 발행됨' 으로 잡힌다."""
        posts = [_post(_kst("13:45"))]
        assert run_story._story_published_today(posts, _kst("18:00"))
        assert run_story._non_chat_stamps(posts)

    def test_weighting_keeps_regular_text_fallback(self):
        post = _post(_kst("19:58"))   # C 19:53 판정 창
        assert not run_weighting._is_chat_post(post)
        assert run_weighting._is_chat_post(_post(_kst("19:20")))

    def test_scan_limits_cover_worst_day(self):
        """하루 최악(정기 7 + CHAT 15 + 이벤트 4 = 26)을 조회 상한이 덮는다."""
        worst_day = len(config.PUBLISH_SLOT_TIMES) + len(config.CHAT_TRIGGERS) + len(
            config.EVENT_SLOT_TIMES
        )
        assert worst_day == 26
        assert config.REPLY_SCAN_POSTS >= worst_day
        week = (7 * 7 + 5 * config.CHAT_DAILY_MAX + 2 * config.CHAT_WEEKEND_MAX
                + 7 * len(config.EVENT_SLOT_TIMES))
        assert config.INSIGHTS_POST_LIMIT >= week
        # 현실 최대(정기는 하루 1건 · 이벤트 일 상한) — run_chat/run_story 조회 25건
        realistic = 1 + config.CHAT_DAILY_MAX + config.EVENT_DAILY_CAP
        assert run_chat.POSTS_TO_SCAN >= realistic and run_story.POSTS_TO_SCAN >= realistic


# ---------------------------------------------------------------------------
# 지연 · 재검증
# ---------------------------------------------------------------------------


class TestJitterAndRecheck:
    @pytest.mark.parametrize("end", [b for _, b in EXPECTED_ZONES])
    def test_high_clipped_to_zone_end(self, end):
        now = _kst(end) - dt.timedelta(minutes=5)
        low, high = chat_plan.jitter_range(0, now)
        assert (low, high) == (config.CHAT_JITTER[0], 5 * 60 - chat_plan.WINDOW_END_MARGIN_SEC)

    def test_not_clipped_deep_in_zone(self):
        assert chat_plan.jitter_range(0, _kst("09:30")) == config.CHAT_JITTER
        assert chat_plan.jitter_range(0, _kst("20:44")) == config.CHAT_JITTER

    def test_outside_zone_collapses(self):
        low, high = chat_plan.jitter_range(0, _kst("13:00"))
        assert low == high == config.CHAT_JITTER[0]

    def test_final_recheck_rejects_near_zone_end(self):
        now = _kst("19:48") - dt.timedelta(seconds=chat_plan.PUBLISH_MIN_ROOM_SEC - 5)
        counts = chat_plan.PostCounts(0, None)
        got = chat_plan.gate(DAY, now, None, counts, manual=True)
        assert got and "종료" in got
        # 사전 판정(enforce_gap=False)은 여유를 보지 않는다(지연 상한이 구역 끝으로 잘린다).
        assert chat_plan.gate(DAY, now, None, counts, manual=True, enforce_gap=False) is None

    def test_final_recheck_rejects_outside_zone(self):
        counts = chat_plan.PostCounts(0, None)
        for hhmm in ("12:21", "13:20", "17:30", "23:12"):
            got = chat_plan.gate(DAY, _kst(hhmm), None, counts, manual=True)
            assert got and "구역" in got, hhmm


# ---------------------------------------------------------------------------
# 트리거 배치
# ---------------------------------------------------------------------------


def _zone_of(mark: int) -> tuple[int, int] | None:
    return next((z for z in chat_plan.chat_zones() if z[0] <= mark < z[1]), None)


class TestTriggerLayoutV15:
    def test_count_and_order(self):
        marks = [_mark(t) for t in config.CHAT_TRIGGERS]
        assert len(marks) == 15 and marks == sorted(marks)

    def test_inside_zone_with_room(self):
        for hhmm in config.CHAT_TRIGGERS:
            zone = _zone_of(_mark(hhmm))
            assert zone is not None, hhmm
            assert zone[1] - _mark(hhmm) >= chat_plan.TRIGGER_MIN_ROOM_MIN, hhmm

    def test_distribution_roughly_proportional(self):
        """구역별 트리거 수 = (9, 1, 1, 2, 2). 구역 길이 비례 몫과 1 이내 차이."""
        zones = chat_plan.chat_zones()
        counts = [sum(1 for t in config.CHAT_TRIGGERS if z[0] <= _mark(t) < z[1]) for z in zones]
        assert counts == [9, 1, 1, 2, 2]
        total = sum(b - a for a, b in zones)
        for (a, b), n in zip(zones, counts, strict=True):
            assert abs(n - 15 * (b - a) / total) <= 1.0, ((a, b), n)

    def test_same_zone_pairs_fit_gap(self):
        """같은 구역 연속 트리거: 첫 글 최악(+cron 지연 5 + 준비 1.5 + 생성 1 + 지연 상한)
        + 최소 간격이 구역 끝 − 종료 여유 안에 든다."""
        worst_first = 5 + 1.5 + 1 + config.CHAT_JITTER[1] / 60
        for a, b in zip(config.CHAT_TRIGGERS, config.CHAT_TRIGGERS[1:], strict=False):
            zone = _zone_of(_mark(a))
            if zone != _zone_of(_mark(b)):
                continue
            latest_second = _mark(a) + worst_first + config.CHAT_MIN_GAP_MIN
            assert latest_second <= zone[1] - chat_plan.WINDOW_END_MARGIN_SEC / 60, (a, b)

    def test_spacing_and_minutes(self):
        marks = [_mark(t) for t in config.CHAT_TRIGGERS]
        gaps = [b - a for a, b in zip(marks, marks[1:], strict=False)]
        assert min(gaps) >= 11
        assert len(set(gaps)) > 1
        assert all(t[3:] not in ("00", "30") for t in config.CHAT_TRIGGERS)

    def test_far_from_every_other_cron(self):
        """타 워크플로 cron(reply·watchdog·insights·publish·story 등 전부)과 6분 이상."""
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            if path.name == "chat.yml":
                continue
            for cron in _crons(path.name):
                other = _utc_cron_to_kst_minute(cron)
                for hhmm in config.CHAT_TRIGGERS:
                    diff = abs(other - _mark(hhmm))
                    assert min(diff, 1440 - diff) >= 6, (path.name, cron, hhmm)

    def test_not_right_after_reply_slot(self):
        """reply.yml 슬롯 직후 20분(시작 지연 0~10분 + 짧은 스윕) 안에 CHAT 트리거를 두지 않는다."""
        for cron in _crons("reply.yml"):
            slot = _utc_cron_to_kst_minute(cron)
            for hhmm in config.CHAT_TRIGGERS:
                assert not 0 <= _mark(hhmm) - slot <= 20, (cron, hhmm)

    def test_chat_yml_comments_match(self):
        body = (ROOT / ".github" / "workflows" / "chat.yml").read_text(encoding="utf-8")
        for idx, hhmm in enumerate(config.CHAT_TRIGGERS, start=1):
            assert f"# T{idx} KST {hhmm}" in body


class TestConstantsV15:
    def test_values(self):
        assert config.CHAT_MIN_GAP_MIN == 15
        assert config.CHAT_JITTER == (60, 600)
        assert config.CHAT_MAX_GAP_WAIT_SEC == 900
        assert (config.CHAT_DAILY_MIN, config.CHAT_DAILY_MAX) == (5, 15)
        assert (config.CHAT_WEEKEND_MIN, config.CHAT_WEEKEND_MAX) == (2, 3)

    def test_jitter_shorter_than_spacing(self):
        marks = [_mark(t) for t in config.CHAT_TRIGGERS]
        spacing = min(b - a for a, b in zip(marks, marks[1:], strict=False))
        assert (config.CHAT_JITTER[0] + config.CHAT_JITTER[1]) / 60 < spacing

    def test_gap_wait_covers_min_gap(self):
        assert config.CHAT_MAX_GAP_WAIT_SEC >= config.CHAT_MIN_GAP_MIN * 60

    def test_worst_pre_publish_fits_budget_and_timeout(self):
        _, high = chat_plan.jitter_range(config.CHAT_MAX_GAP_WAIT_SEC)
        assert high == max(config.CHAT_JITTER[1], config.CHAT_MAX_GAP_WAIT_SEC + 30 + 60) == 990
        assert high + 120 < config.CHAT_JOB_BUDGET_SEC
        jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "chat.yml").read_text())["jobs"]
        timeout_min = next(iter(jobs.values()))["timeout-minutes"]
        assert config.CHAT_JOB_BUDGET_SEC <= (timeout_min - 5) * 60


# ---------------------------------------------------------------------------
# 시간대(band) · 소재 · 프롬프트
# ---------------------------------------------------------------------------


class TestBands:
    def test_band_names_consistent(self):
        names = [b for b, _ in config.CHAT_TIME_BANDS]
        assert names == list(ai_writer.CHAT_SEEDS_BY_BAND) == list(ai_writer.CHAT_BAND_CONTEXT)

    def test_trigger_bands(self):
        got = {b: len(chat_plan.band_triggers(b)) for b, _ in config.CHAT_TIME_BANDS}
        assert got == {"오전": 9, "오후": 1, "저녁": 2, "밤": 3}

    @pytest.mark.parametrize("hhmm,band", [
        ("09:00", "오전"), ("12:20", "오전"), ("15:58", "오후"), ("19:17", "저녁"),
        ("20:59", "저녁"), ("21:00", "밤"), ("23:11", "밤"),
    ])
    def test_band_of_minute(self, hhmm, band):
        assert chat_plan.band_of_minute(_mark(hhmm)) == band

    def test_band_for_uses_trigger_time_then_now(self):
        # 예약 실행: cron 이 늦어도 트리거 예정 시각의 시간대(멱등).
        assert chat_plan.band_for(12, _kst("21:10")) == "저녁"     # T12 20:44
        assert chat_plan.band_for(None, _kst("21:10")) == "밤"     # 수동

    def test_morning_pool_unchanged(self):
        assert ai_writer.chat_seeds("오전") == ai_writer.CHAT_PILLAR.seeds
        assert len(ai_writer.CHAT_PILLAR.seeds) == 18
        assert ai_writer.chat_seeds("모름") == ai_writer.CHAT_PILLAR.seeds

    def test_pools_pass_lint_and_cover_triggers(self):
        for band, seeds in ai_writer.CHAT_SEEDS_BY_BAND.items():
            assert len(set(seeds)) == len(seeds), band
            assert len(seeds) >= len(chat_plan.band_triggers(band)) + 1, band
            for seed in seeds:
                content.lint_chat(seed)

    def test_no_duplicate_seed_per_band_in_a_day(self):
        for day in YEAR:
            for band, _ in config.CHAT_TIME_BANDS:
                pool = ai_writer.chat_seeds(band)
                picked = [chat_plan.seed_for(day, t, pool, band=band)
                          for t in chat_plan.band_triggers(band)]
                picked.append(chat_plan.seed_for(day, None, pool, band=band))
                assert len(set(picked)) == len(picked), (day, band)

    def test_seed_deterministic_and_varies(self):
        pool = ai_writer.chat_seeds("밤")
        assert chat_plan.seed_for(DAY, 15, pool, band="밤") == chat_plan.seed_for(
            DAY, 15, pool, band="밤")
        assert len({chat_plan.seed_for(d, 15, pool, band="밤") for d in YEAR[:30]}) > 3

    def test_no_morning_wording_outside_morning(self):
        brief = ai_writer.CHAT_PILLAR.brief
        assert "오늘 아침 시장" not in brief and "# 지금 시간대" in brief
        assert ai_writer.CHAT_PILLAR.label == "시장 잡담"
        for band in ("오후", "저녁", "밤"):
            for seed in ai_writer.chat_seeds(band):
                assert "출근길" not in seed and "오늘 아침" not in seed, seed

    def test_time_block_in_prompt(self):
        block = ai_writer.chat_time_block("저녁")
        assert block.startswith("# 지금 시간대") and "지금은 저녁" in block
        prompt = ai_writer._build_user_prompt(
            ai_writer.CHAT_PILLAR, "소재", [], "", time_block=block)
        assert "지금은 저녁" in prompt
        assert ai_writer.chat_time_block("모름") == ""

    def test_band_context_has_no_digits_or_forecast(self):
        for text in ai_writer.CHAT_BAND_CONTEXT.values():
            assert not any(ch.isdigit() for ch in text)
            assert not [t for t in config.CHAT_FORECAST_TERMS if t in text]

    def test_mood_prompts_time_neutral(self):
        mood = mood_source.Mood("web", ("금리",), "관망")
        assert "최근 24시간" in mood.to_prompt_block()
        assert "아침" not in mood.to_prompt_block()
        assert "오늘 아침" not in mood_source.WEB_SYSTEM_PROMPT

    def test_run_chat_night_trigger_uses_night_band(self, chat_env):
        trig = len(config.CHAT_TRIGGERS)                  # T15 22:54 — 0건이면 보충 발행
        now = _kst(config.CHAT_TRIGGERS[trig - 1], dt.date(2026, 9, 21))
        client = _client([])
        code, gen, _ = _run_chat(client, now=now, trigger=f"T{trig}",
                                 text="금리 얘기가 많은 밤이네요.\n다들 뭐 보세요?")
        assert code == 0
        assert "지금은 밤" in gen.call_args.kwargs["time_block"]
        assert gen.call_args.args[2] in ai_writer.chat_seeds("밤")
        client.publish_text_post.assert_called_once()


# ---------------------------------------------------------------------------
# 워치독
# ---------------------------------------------------------------------------


class TestWatchdogV15:
    def test_constants(self):
        assert (watchdog.CHAT_CHECK_GRACE_MIN, watchdog.CHAT_CHECK_MIN_DUE) == (30, 2)
        crons = sorted(_utc_cron_to_kst_minute(c) for c in _crons("watchdog.yml"))
        assert [chat_plan.fmt_minutes(m) for m in crons] == ["09:53", "21:37"]

    def test_morning_run_never_judges(self):
        """09:53 시점 기회는 T1(09:04) 하나뿐 → 1년 내내 경고하지 않는다."""
        for day in YEAR:
            f = watchdog.check_chat_activity(0, _kst("09:53", day), enabled=True)
            assert f.severity == watchdog.Severity.OK, day

    def test_evening_run_judges_every_weekday(self):
        for day in YEAR:
            f = watchdog.check_chat_activity(0, _kst("21:37", day), enabled=True)
            if not chat_plan.is_weekend(day):
                assert f.severity == watchdog.Severity.WARN, day
            ok = watchdog.check_chat_activity(1, _kst("21:37", day), enabled=True)
            assert ok.severity == watchdog.Severity.OK

    def test_due_counts_from_first_selected(self):
        day = next(d for d in YEAR if chat_plan.selected_triggers(d)[0] == 12)
        # T12 20:44 + 30분 = 21:14 ≤ 21:37 → 기회 1회 → 보류
        assert chat_plan.due_opportunities(day, _kst("21:37", day), 30) == 1
        f = watchdog.check_chat_activity(0, _kst("21:37", day), enabled=True)
        assert f.severity == watchdog.Severity.OK and "보류" in f.title
        assert chat_plan.due_opportunities(day, _kst("23:59", day), 30) == 4

    def test_due_other_days(self):
        assert chat_plan.due_opportunities(DAY, _kst("10:00", DAY - dt.timedelta(days=1)), 30) == 0
        nxt = _kst("01:00", DAY + dt.timedelta(days=1))
        first = chat_plan.selected_triggers(DAY)[0]
        assert chat_plan.due_opportunities(DAY, nxt, 30) == 15 - first + 1


# ---------------------------------------------------------------------------
# verify_repo 검사 10 — 실패 경로
# ---------------------------------------------------------------------------


class TestVerifyCheck10:
    @staticmethod
    def _check():
        import importlib
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        verify_repo = importlib.import_module("verify_repo")
        return verify_repo.check_classify_windows()

    def test_passes(self):
        assert self._check() == 0

    def test_trigger_outside_zone_fails(self):
        bad = (*config.CHAT_TRIGGERS[:-1], "23:20")
        with mock.patch.object(config, "CHAT_TRIGGERS", bad):
            assert self._check() >= 1

    def test_trigger_without_room_fails(self):
        bad = (*config.CHAT_TRIGGERS[:-1], "23:05")    # 구역 끝 23:12 까지 7분
        with mock.patch.object(config, "CHAT_TRIGGERS", bad):
            assert self._check() >= 1

    def test_overlapping_reserved_fails(self):
        with mock.patch.object(insights, "EVENT_SLOTS", ("03:11", "12:40", "17:41", "23:17")):
            assert self._check() >= 1

    def test_no_zone_fails(self):
        with mock.patch.object(config, "CHAT_MIN_ZONE_MIN", 1000):
            assert self._check() >= 1


# ---------------------------------------------------------------------------
# 운영 시뮬레이션 (정기 글 + reply.yml 실행 포함)
# ---------------------------------------------------------------------------


REPLY_SLOTS = tuple(sorted(chat_plan.fmt_minutes(_utc_cron_to_kst_minute(c))
                           for c in _crons("reply.yml")))


@dataclass
class Scenario:
    name: str
    miss: float                          # cron 누락 확률(chat·reply 공통)
    max_delay_min: float                 # cron 시작 지연 최대(분, 균등)
    sweep: tuple[int, int] = (5, 30)     # CHAT 실행 안 답글 스윕(초) — 예산으로 자른다
    reply: tuple[int, int] | None = None  # reply.yml 스윕(초). None = reply 실행 미모델


@dataclass
class DayResult:
    target: int
    posted: list[float] = field(default_factory=list)   # CHAT, KST 자정 기준 초
    others: list[float] = field(default_factory=list)   # 정기 글
    cancelled_chat: int = 0
    cancelled_reply: int = 0
    chat_waited_behind_reply: int = 0
    over_timeout: int = 0
    reply_over_timeout: int = 0


def _at(day: dt.date, sec: float) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(0), tzinfo=KST) + dt.timedelta(seconds=sec)


def _counts(day: dt.date, res: DayResult, sec: float) -> chat_plan.PostCounts:
    chats = [p for p in res.posted if p <= sec]
    every = chats + [p for p in res.others if p <= sec]
    n = sum(1 for p in chats if chat_plan.is_chat_time(_at(day, p)))
    return chat_plan.PostCounts(n, _at(day, max(every)) if every else None)


def _exec_chat(day, trig, start, res, sc, rng) -> float:
    t = start + rng.uniform(45, 90)                 # checkout · pip
    run_started = t
    now = _at(day, t)
    counts = _counts(day, res, t)
    blocked = chat_plan.gate(day, now, trig, counts, manual=False, enforce_gap=False)
    wait = chat_plan.gap_wait_seconds(counts, now)
    if blocked is None and wait > config.CHAT_MAX_GAP_WAIT_SEC:
        blocked = "gap-limit"
    if blocked is None:
        t += rng.uniform(20, 60)                    # 근거 수집 + 생성
        low, high = chat_plan.jitter_range(wait, _at(day, t))
        t += rng.randint(low, high)
        if chat_plan.gate(day, _at(day, t), trig, _counts(day, res, t), manual=False) is None:
            t += rng.uniform(3, 15)                 # 컨테이너 생성·발행
            res.posted.append(t)
    remaining = run_chat.sweep_budget_sec(_at(day, t), t - run_started)
    t += min(rng.uniform(*sc.sweep), max(0.0, remaining))
    end = t + 5
    if end - start > 25 * 60:
        res.over_timeout += 1
    return end


def _exec_reply(start, res, sc, rng) -> float:
    t = start + rng.uniform(45, 90)
    t += rng.uniform(*config.REPLY_START_JITTER)
    t += min(rng.uniform(*sc.reply), config.REPLY_SWEEP_BUDGET_SEC)
    end = t + 5
    if end - start > 40 * 60:
        res.reply_over_timeout += 1
    return end


def simulate_day(day: dt.date, sc: Scenario, rng: random.Random) -> DayResult:
    res = DayResult(chat_plan.daily_target(day))
    slot = antibot.choose_slot(day, list(config.PUBLISH_SLOTS), config.ANTIBOT_SLOT_SALT_PUBLISH)
    res.others.append(
        _mark(config.PUBLISH_SLOT_TIMES[slot]) * 60 + rng.uniform(0, sc.max_delay_min * 60)
        + rng.uniform(60, 90) + rng.uniform(*config.ANTIBOT_PUBLISH_JITTER)
    )
    arrivals: list[tuple[float, str, int]] = []
    for idx, hhmm in enumerate(config.CHAT_TRIGGERS, start=1):
        if rng.random() >= sc.miss:
            arrivals.append((_mark(hhmm) * 60 + rng.uniform(0, sc.max_delay_min * 60), "chat", idx))
    if sc.reply is not None:
        for hhmm in REPLY_SLOTS:
            if rng.random() >= sc.miss:
                arrivals.append((_mark(hhmm) * 60 + rng.uniform(0, sc.max_delay_min * 60),
                                 "reply", 0))
    arrivals.sort()

    running = ""
    busy_until = 0.0
    pending: tuple[float, str, int] | None = None

    def run(kind: str, trig: int, start: float) -> float:
        nonlocal running
        running = kind
        if kind == "chat":
            return _exec_chat(day, trig, start, res, sc, rng)
        return _exec_reply(start, res, sc, rng)

    for arrive, kind, trig in arrivals:
        if pending is not None and busy_until <= arrive:
            busy_until = run(pending[1], pending[2], max(busy_until, pending[0]))
            pending = None
        if busy_until <= arrive:
            busy_until = run(kind, trig, arrive)
            continue
        if kind == "chat" and running == "reply":
            res.chat_waited_behind_reply += 1
        if pending is not None:                     # 기존 대기 실행 취소
            if pending[1] == "chat":
                res.cancelled_chat += 1
            else:
                res.cancelled_reply += 1
        pending = (arrive, kind, trig)
    if pending is not None:
        run(pending[1], pending[2], max(busy_until, pending[0]))
    return res


def run_scenario(sc: Scenario, days: list[dt.date] = YEAR, seed: int = 7) -> dict:
    rng = random.Random(seed)
    results = [simulate_day(d, sc, rng) for d in days]
    weekday = [r for d, r in zip(days, results, strict=True) if not chat_plan.is_weekend(d)]
    gaps: list[float] = []
    near_other = 0
    for r in results:
        chats = sorted(r.posted)
        gaps += [b - a for a, b in zip(chats, chats[1:], strict=False)]
        near_other += sum(1 for p in chats for o in r.others
                          if abs(p - o) < config.CHAT_MIN_GAP_MIN * 60)
    full = [r for r in weekday if r.target == 15]
    zones = chat_plan.chat_zones()
    per_zone = [0] * len(zones)
    for r in results:
        for p in r.posted:
            for i, (a, b) in enumerate(zones):
                if a <= p // 60 < b:
                    per_zone[i] += 1
    return {
        "days_met": sum(len(r.posted) >= r.target for r in results) / len(results),
        "post_rate": sum(len(r.posted) for r in results) / sum(r.target for r in results),
        "weekday_avg_target": sum(r.target for r in weekday) / len(weekday),
        "weekday_avg_posted": sum(len(r.posted) for r in weekday) / len(weekday),
        "full15_days": len(full),
        "full15_met": sum(len(r.posted) >= 15 for r in full),
        "full15_avg_posted": sum(len(r.posted) for r in full) / len(full) if full else 0.0,
        "min_gap_sec": min(gaps) if gaps else None,
        "over_target": sum(len(r.posted) > r.target for r in results),
        "outside_zone": sum(not chat_plan.is_chat_time(_at(d, p))
                            for d, r in zip(days, results, strict=True) for p in r.posted),
        "posts": sum(len(r.posted) for r in results),
        "per_zone": per_zone,
        "near_regular": near_other,
        "cancelled_chat": sum(r.cancelled_chat for r in results),
        "cancelled_reply": sum(r.cancelled_reply for r in results),
        "chat_waited_behind_reply": sum(r.chat_waited_behind_reply for r in results),
        "over_timeout": sum(r.over_timeout for r in results),
        "reply_over_timeout": sum(r.reply_over_timeout for r in results),
    }


SCENARIOS = {
    "normal": Scenario("normal", 0.0, 5.0),
    "miss5": Scenario("miss5", 0.05, 10.0),
    "miss17": Scenario("miss17", 0.17, 20.0),
    "busy30": Scenario("busy30", 0.30, 45.0),
    "normal_heavy": Scenario("normal_heavy", 0.0, 5.0, (360, 900)),
    "reply_light": Scenario("reply_light", 0.0, 5.0, (5, 30), (5, 60)),
    "reply_typical": Scenario("reply_typical", 0.0, 5.0, (30, 360), (30, 360)),
    "reply_heavy": Scenario("reply_heavy", 0.0, 5.0, (30, 360), (360, 1500)),
    "miss17_reply_typical": Scenario("miss17_reply_typical", 0.17, 20.0, (30, 360), (30, 360)),
    "miss17_reply_heavy": Scenario("miss17_reply_heavy", 0.17, 20.0, (30, 360), (360, 1500)),
}


@pytest.fixture(scope="module")
def sim_results() -> dict[str, dict]:
    return {name: run_scenario(sc) for name, sc in SCENARIOS.items()}


class TestOperationalSimulationV15:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_invariants(self, sim_results, name):
        got = sim_results[name]
        assert got["over_target"] == 0
        assert got["outside_zone"] == 0
        assert got["over_timeout"] == 0 and got["reply_over_timeout"] == 0
        assert got["min_gap_sec"] >= config.CHAT_MIN_GAP_MIN * 60

    @pytest.mark.parametrize("name", ["normal", "normal_heavy", "reply_light", "reply_typical"])
    def test_normal_meets_every_target(self, sim_results, name):
        got = sim_results[name]
        assert got["days_met"] == 1.0
        assert got["full15_met"] == got["full15_days"] > 0
        assert got["cancelled_chat"] == got["cancelled_reply"] == 0

    def test_heavy_reply_runs(self, sim_results):
        """reply.yml 스윕 6~25분 가정: 대기 취소 0, 목표 달성일 ≥ 99%."""
        got = sim_results["reply_heavy"]
        assert got["days_met"] >= 0.99
        assert got["cancelled_chat"] == got["cancelled_reply"] == 0

    def test_spread_across_zones(self, sim_results):
        """정상 시나리오: 오전 구역 밖으로 전체 CHAT 의 30% 이상이 나간다(이전 0%)."""
        per_zone = sim_results["normal"]["per_zone"]
        assert all(n > 0 for n in per_zone)
        assert sum(per_zone[1:]) / sum(per_zone) >= 0.3

    def test_without_misses_every_day_full(self):
        days = [d for d in YEAR if chat_plan.daily_target(d) == 15]
        for seed in range(3):
            rng = random.Random(seed)
            for day in days:
                res = simulate_day(day, SCENARIOS["normal"], rng)
                assert len(res.posted) == 15, (day, seed)

"""v1.4.0 CHAT 증량 검증 — 트리거 15개 · 평일 5~15건 · 간격 10분 · 지연 30~300초.

운영 시뮬레이션은 실제 chat_plan.gate / gap_wait_seconds / jitter_range 를 그대로 호출한다.
cron 누락·지연, 실행 소요(준비·생성·지연·답글 스윕), GitHub concurrency 그룹
(실행 중 1건 + 대기 1건, 새 실행이 대기열에 들어오면 기존 대기 실행은 취소)을 모델링한다.
누락률·지연·스윕 소요는 가정값이다(실측 아님). 결과 수치는 DESIGN_V14_CHAT.md 에 기록했다.

v1.5.0: 창 09:00~24:00 · 트리거 재배치 · 간격 15분 · 지연 60~600초로 상수가 바뀌었다.
이 파일의 시뮬레이션·불변식은 새 상수로 그대로 돈다. 상수·배치에 묶인 3건만 고쳤다
(배치 범위·상수 값·스윕 절단 쌍). 정기 글·reply.yml 실행을 넣은 확장 시뮬레이션은 tests/test_chat_v15.py.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from src import ai_writer, chat_plan, config, run_chat, watchdog

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
DAY = dt.date(2026, 10, 5)   # 월요일
YEAR = [DAY + dt.timedelta(days=i) for i in range(365)]


def _mark(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _crons(name: str) -> list[str]:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    on = data.get(True) or data.get("on") or {}
    return [c["cron"] for c in on.get("schedule") or []]


def _utc_cron_to_kst_minute(cron: str) -> int:
    minute, hour = (int(x) for x in cron.split()[:2])
    return ((hour + 9) % 24) * 60 + minute


# ---------------------------------------------------------------------------
# 트리거 배치 · 상수
# ---------------------------------------------------------------------------


class TestTriggerLayout:
    def test_fifteen_triggers_inside_window(self):
        # v1.5.0: 09:00~11:55 범위 → CHAT 구역(창 09:00~24:00 − 예약 구간) 안.
        marks = [_mark(t) for t in config.CHAT_TRIGGERS]
        assert len(marks) == 15
        assert marks == sorted(marks)
        zones = chat_plan.chat_zones()
        assert all(any(a <= m < b for a, b in zones) for m in marks)

    def test_spacing_at_least_eleven_minutes(self):
        marks = [_mark(t) for t in config.CHAT_TRIGGERS]
        gaps = [b - a for a, b in zip(marks, marks[1:], strict=False)]
        assert min(gaps) >= 11, gaps
        assert len(set(gaps)) > 1, "간격이 전부 같으면 기계적 패턴"

    def test_not_on_hour_or_half_hour(self):
        for hhmm in config.CHAT_TRIGGERS:
            assert hhmm[3:] not in ("00", "30"), hhmm

    def test_far_from_other_workflow_crons(self):
        """타 워크플로우 cron 과 5분 이내로 붙지 않는다(v1.3.0 cron 감사 기준 유지)."""
        chat = {_mark(t) for t in config.CHAT_TRIGGERS}
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            if path.name == "chat.yml":
                continue
            for cron in _crons(path.name):
                other = _utc_cron_to_kst_minute(cron)
                for mine in chat:
                    diff = min(abs(other - mine), 1440 - abs(other - mine))
                    assert diff > 5, (path.name, cron, mine)

    def test_case_mapping_matches_order(self):
        body = (ROOT / ".github" / "workflows" / "chat.yml").read_text(encoding="utf-8")
        cases = re.findall(r'"([\d\* ]+)"\)\s*echo "slot=(T\d+)"', body)
        crons = _crons("chat.yml")
        assert [c for c, _ in cases] == crons
        assert [s for _, s in cases] == [f"T{i}" for i in range(1, len(crons) + 1)]

    def test_constants(self):
        # v1.5.0: 간격 10 → 15분, 지연 (30, 300) → (60, 600) (DESIGN_V15_CHAT_WINDOW.md)
        assert config.CHAT_MIN_GAP_MIN == 15
        assert config.CHAT_JITTER == (60, 600)
        assert (config.CHAT_DAILY_MIN, config.CHAT_DAILY_MAX) == (5, 15)
        assert (config.CHAT_WEEKEND_MIN, config.CHAT_WEEKEND_MAX) == (2, 3)

    def test_jitter_shorter_than_spacing(self):
        marks = [_mark(t) for t in config.CHAT_TRIGGERS]
        spacing = min(b - a for a, b in zip(marks, marks[1:], strict=False))
        assert config.CHAT_JITTER[1] / 60 < spacing - config.CHAT_JITTER[0] / 60

    def test_worst_pre_publish_delay_fits_budget_and_timeout(self):
        """간격 대기 최대 + 지연 상한 + 준비·생성 여유가 예산·timeout 안에 든다."""
        _, high = chat_plan.jitter_range(config.CHAT_MAX_GAP_WAIT_SEC)
        assert high == config.CHAT_MAX_GAP_WAIT_SEC + 30 + 60
        assert high + 120 < config.CHAT_JOB_BUDGET_SEC
        jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "chat.yml").read_text())["jobs"]
        timeout_min = next(iter(jobs.values()))["timeout-minutes"]
        assert config.CHAT_JOB_BUDGET_SEC <= (timeout_min - 5) * 60

    def test_watchdog_scan_covers_two_chat_windows(self):
        """정기 글 최대 간격(약 38.5시간) 안에 CHAT 창이 두 번 들어간다."""
        assert watchdog.SLOT_SPREAD_MAX_GAP_HOURS < 48
        assert watchdog.RECENT_POSTS_TO_SCAN >= 2 * len(config.CHAT_TRIGGERS) + 5


# ---------------------------------------------------------------------------
# 일일 계획 · 소재
# ---------------------------------------------------------------------------


class TestDailyPlanV14:
    def test_weekday_targets_cover_five_to_fifteen(self):
        weekday = [chat_plan.daily_target(d) for d in YEAR if not chat_plan.is_weekend(d)]
        assert min(weekday) == 5 and max(weekday) == 15
        assert set(weekday) == set(range(5, 16))

    def test_weekend_unchanged(self):
        weekend = {chat_plan.daily_target(d) for d in YEAR if chat_plan.is_weekend(d)}
        assert weekend <= {2, 3}

    def test_fifteen_target_selects_all_triggers(self):
        day = next(d for d in YEAR if chat_plan.daily_target(d) == 15)
        assert chat_plan.selected_triggers(day) == tuple(range(1, 16))

    def test_seed_pool_covers_triggers_and_manual(self):
        seeds = ai_writer.CHAT_PILLAR.seeds
        assert len(seeds) >= len(config.CHAT_TRIGGERS) + 1
        for day in YEAR[:60]:
            picked = [chat_plan.seed_for(day, t, seeds) for t in range(1, 16)]
            picked.append(chat_plan.seed_for(day, None, seeds))
            assert len(set(picked)) == 16, day


# ---------------------------------------------------------------------------
# 운영 시뮬레이션
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    miss: float            # cron 누락 확률
    max_delay_min: float   # cron 시작 지연 최대(분, 균등분포)
    sweep: tuple[int, int] = (5, 30)   # 답글 스윕 소요(초, 균등분포) — 예산으로 자른다


@dataclass
class DayResult:
    target: int
    posted: list[float] = field(default_factory=list)   # KST 자정 기준 초
    cancelled: int = 0
    over_timeout: int = 0


def _at(day: dt.date, sec: float) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(0), tzinfo=KST) + dt.timedelta(seconds=sec)


def _counts(day: dt.date, posted: list[float], sec: float) -> chat_plan.PostCounts:
    before = [p for p in posted if p <= sec]
    in_window = sum(1 for p in before if chat_plan.is_chat_time(_at(day, p)))
    return chat_plan.PostCounts(in_window, _at(day, max(before)) if before else None)


def _execute(day: dt.date, trig: int, start: float, res: DayResult,
             sc: Scenario, rng: random.Random) -> float:
    """run_chat 한 번. 끝난 시각(초)을 돌려준다."""
    t = start + rng.uniform(45, 90)          # checkout · pip
    run_started = t
    now = _at(day, t)
    counts = _counts(day, res.posted, t)
    blocked = chat_plan.gate(day, now, trig, counts, manual=False, enforce_gap=False)
    wait = chat_plan.gap_wait_seconds(counts, now)
    if blocked is None and wait > config.CHAT_MAX_GAP_WAIT_SEC:
        blocked = "gap-limit"
    if blocked is None:
        t += rng.uniform(20, 60)              # 근거 수집 + 생성
        low, high = chat_plan.jitter_range(wait, _at(day, t))
        t += rng.randint(low, high)
        if chat_plan.gate(day, _at(day, t), trig, _counts(day, res.posted, t),
                          manual=False) is None:
            t += rng.uniform(3, 15)           # 컨테이너 생성·발행
            res.posted.append(t)
    # v1.4.0: 실제 run_chat 과 같은 예산 함수(다음 트리거 전 절단)를 쓴다.
    remaining = run_chat.sweep_budget_sec(_at(day, t), t - run_started)
    t += min(rng.uniform(*sc.sweep), max(0.0, remaining))
    end = t + 5
    if end - start > 25 * 60:
        res.over_timeout += 1
    return end


def simulate_day(day: dt.date, sc: Scenario, rng: random.Random) -> DayResult:
    res = DayResult(chat_plan.daily_target(day))
    arrivals = []
    for idx, hhmm in enumerate(config.CHAT_TRIGGERS, start=1):
        if rng.random() < sc.miss:
            continue
        arrivals.append((_mark(hhmm) * 60 + rng.uniform(0, sc.max_delay_min * 60), idx))
    arrivals.sort()

    busy_until = 0.0
    pending: tuple[float, int] | None = None
    for arrive, trig in arrivals:
        if pending is not None and busy_until <= arrive:
            busy_until = _execute(day, pending[1], busy_until, res, sc, rng)
            pending = None
        if busy_until <= arrive:
            busy_until = _execute(day, trig, arrive, res, sc, rng)
        else:
            if pending is not None:
                res.cancelled += 1            # concurrency: 기존 대기 실행 취소
            pending = (arrive, trig)
    if pending is not None:
        _execute(day, pending[1], max(busy_until, pending[0]), res, sc, rng)
    return res


def run_scenario(sc: Scenario, days: list[dt.date], seed: int = 7) -> dict:
    rng = random.Random(seed)
    results = [simulate_day(d, sc, rng) for d in days]
    weekday = [(d, r) for d, r in zip(days, results, strict=True) if not chat_plan.is_weekend(d)]
    gaps = [
        b - a for r in results for a, b in zip(sorted(r.posted), sorted(r.posted)[1:], strict=False)
    ]
    full = [r for _, r in weekday if r.target == 15]
    return {
        "days_met": sum(len(r.posted) >= r.target for r in results) / len(results),
        "post_rate": sum(len(r.posted) for r in results) / sum(r.target for r in results),
        "weekday_avg_target": sum(r.target for _, r in weekday) / len(weekday),
        "weekday_avg_posted": sum(len(r.posted) for _, r in weekday) / len(weekday),
        "full15_days": len(full),
        "full15_avg_posted": (sum(len(r.posted) for r in full) / len(full)) if full else 0.0,
        "full15_met": sum(len(r.posted) >= 15 for r in full),
        "min_gap_sec": min(gaps) if gaps else None,
        "over_target": sum(len(r.posted) > r.target for r in results),
        "outside_window": sum(
            not chat_plan.is_chat_time(_at(d, p)) for d, r in zip(days, results, strict=True)
            for p in r.posted
        ),
        "posts": sum(len(r.posted) for r in results),
        "cancelled_runs": sum(r.cancelled for r in results),
        "over_timeout": sum(r.over_timeout for r in results),
    }


SCENARIOS = {
    "ideal": Scenario("ideal", 0.0, 0.0),
    "normal": Scenario("normal", 0.0, 5.0),
    "normal_sweep": Scenario("normal_sweep", 0.0, 5.0, (30, 360)),
    "miss5": Scenario("miss5", 0.05, 10.0),
    "miss17": Scenario("miss17", 0.17, 20.0),
    "busy30": Scenario("busy30", 0.30, 45.0),
    "miss17_sweep": Scenario("miss17_sweep", 0.17, 20.0, (30, 360)),
    # 답글 스윕이 실행마다 6~15분 걸리는 가정. 트리거 간격(11~13분)을 넘겨
    # concurrency 대기 실행 취소가 생기는 경우를 본다(DESIGN_V14_CHAT.md 위험 항목).
    "normal_heavy": Scenario("normal_heavy", 0.0, 5.0, (360, 900)),
    "miss17_heavy": Scenario("miss17_heavy", 0.17, 20.0, (360, 900)),
}


class TestOperationalSimulation:
    def test_fifteen_target_day_fully_achieved_without_misses(self):
        """누락·지연 없음: 목표 15건인 평일은 전부 15건을 낸다(간격 10분 이상)."""
        days = [d for d in YEAR if chat_plan.daily_target(d) == 15]
        assert days
        for seed in range(5):
            rng = random.Random(seed)
            for day in days:
                res = simulate_day(day, SCENARIOS["ideal"], rng)
                assert len(res.posted) == 15, (day, seed)
                gaps = [b - a for a, b in zip(res.posted, res.posted[1:], strict=False)]
                assert min(gaps) >= config.CHAT_MIN_GAP_MIN * 60
                assert all(chat_plan.is_chat_time(_at(day, p)) for p in res.posted)

    def test_normal_scenario_meets_every_target(self):
        """cron 지연 0~5분, 누락 없음: 1년 전 일자 목표 달성."""
        got = run_scenario(SCENARIOS["normal"], YEAR)
        assert got["days_met"] == 1.0
        assert got["full15_met"] == got["full15_days"] > 0

    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_invariants_all_scenarios(self, name):
        got = run_scenario(SCENARIOS[name], YEAR)
        assert got["over_target"] == 0
        assert got["over_timeout"] == 0
        assert got["min_gap_sec"] >= config.CHAT_MIN_GAP_MIN * 60
        # 재검증(12:05 직전 통과) 후 컨테이너 생성·발행 몇 초 사이에 창을 넘는 경우.
        # v1.3.0 부터 있던 경계 현상이다(창 밖 발행 = CHAT 으로 세지 않음). 빈도만 묶어 둔다.
        assert got["outside_window"] <= max(1, got["posts"] // 500)

    def test_no_window_edge_in_normal(self):
        """정상(누락 없음) 시나리오에서는 창 밖 발행이 없다."""
        got = run_scenario(SCENARIOS["normal"], YEAR)
        assert got["outside_window"] == 0


# ---------------------------------------------------------------------------
# v1.4.0: CHAT 실행의 답글 스윕 예산 — 다음 트리거 전 절단
# ---------------------------------------------------------------------------


class TestSweepBudget:
    def _kst(self, hhmm: str, sec: int = 0) -> dt.datetime:
        h, m = map(int, hhmm.split(":"))
        return dt.datetime(2026, 10, 5, h, m, sec, tzinfo=KST)

    def test_seconds_until_next_trigger(self):
        first, second = config.CHAT_TRIGGERS[0], config.CHAT_TRIGGERS[1]
        gap = (_mark(second) - _mark(first)) * 60
        assert chat_plan.seconds_until_next_trigger(self._kst(first)) == gap
        assert chat_plan.seconds_until_next_trigger(self._kst("08:00")) == (
            _mark(first) * 60 - 8 * 3600
        )

    def test_no_next_trigger_after_last(self):
        assert chat_plan.seconds_until_next_trigger(self._kst(config.CHAT_TRIGGERS[-1])) is None
        assert chat_plan.seconds_until_next_trigger(self._kst("23:00")) is None

    def test_budget_cut_before_next_trigger(self):
        # v1.5.0: T1→T2 간격(22분)에서는 절단이 job 예산보다 크다. 실제로 잘리는
        # 가장 가까운 연속 쌍(T8 11:39 → T9 11:57, 18분)으로 본다.
        marks = [_mark(t) for t in config.CHAT_TRIGGERS]
        i = min(range(len(marks) - 1), key=lambda k: marks[k + 1] - marks[k])
        first, second = config.CHAT_TRIGGERS[i], config.CHAT_TRIGGERS[i + 1]
        now = self._kst(first, 0) + dt.timedelta(minutes=5)
        until = (_mark(second) - _mark(first) - 5) * 60
        expected = until - config.CHAT_SWEEP_NEXT_TRIGGER_MARGIN_SEC
        assert run_chat.sweep_budget_sec(now, elapsed_sec=300) == expected
        assert expected < config.CHAT_JOB_BUDGET_SEC - 300

    def test_last_trigger_uses_job_budget(self):
        now = self._kst(config.CHAT_TRIGGERS[-1]) + dt.timedelta(minutes=2)
        assert run_chat.sweep_budget_sec(now, elapsed_sec=120) == config.CHAT_JOB_BUDGET_SEC - 120

    def test_never_negative(self):
        second = config.CHAT_TRIGGERS[1]
        now = self._kst(second) - dt.timedelta(seconds=10)
        assert run_chat.sweep_budget_sec(now, elapsed_sec=0) == 0.0

    def test_safe_sweep_passes_cut_budget(self, monkeypatch):
        from unittest import mock
        monkeypatch.setattr(config, "REPLY_ENABLED", True)
        monkeypatch.setattr(run_chat, "sweep_budget_sec", lambda now, elapsed: 123.0)
        with mock.patch.object(run_chat.run_reply, "sweep") as sweep:
            run_chat._safe_sweep(mock.Mock(), mock.Mock(dry_run=True))
        assert sweep.call_args.kwargs["budget_sec"] == 123.0

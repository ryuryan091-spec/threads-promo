"""
p76/run_novel.py
================
사건 소설(B형) 원고 자동 작성 (DSN-P76-NOVEL-001·002·003 · DN2026_0072 · DN2026_0119 Zero-Touch).
게시는 별도 파이프라인 p76.run_novel_publish 가 맡는다(이 모듈은 게시하지 않는다). 한 실행 = 아래 순서 1회.

  0 가드      PAGE76_ENABLED · PAGE76_NOVEL_ENABLED (전역 중단)
  1 중단 수집  DB-12 중단 체크 → 상태 '중단'
  2 이관      v1 잔존 상태 재평가(§5-3): 바이블 승인·명시/묵시승인대기 → 저장 JSON 2차 검수 → 정본 | 재생성
              회차 검토대기 + PASS + 작성 모델 → 승인(auto:migrate), 그 외 → 자동 수정 경로
  3 일정      novel_calendar(금 휴재 + 순연 shifts). 최종 실패·놓친 게시일 → 순연 + 휴재 안내 카드 행(A2)
              놓친 게시일 순연은 live 만 · '승인' 전 회차는 D-1 22:00 선제 순연 · 보류·거절확인필요·폐기는 순연 안 함
              순연 한도 초과 → 보류 + CONTROL "paused" + T5 1회 → 그 시즌 이후 순연·생성 멈춤, N_DUE 비움
              정지 회차를 사람이 승인·생성중·검사실패로 바꾸면 자동 해제
  4 생성      한 실행에 1건: 바이블 재시도 → 회차(정본 바이블, 게시일 D-3) → 다음 시즌 바이블(제1화 D-7)
              작성 → 규칙 검사 → 1차 검수 → PASS 면 승인/정본(auto:gate1)
              플래그·fallback·BLOCK → 자동 수정(최대 NOVEL_REPAIR_MAX) → 독립 2차 검수(MODEL_REVIEWER_2) → auto:gate2
  5 전날 안내  20시대 실행에서 내일 게시 회차 — 정보성(조치 불필요)

  모드     | DB-12 기록 | 텔레그램 | Claude API
  dry_run | 없음        | 없음     | 합성 응답(무료)
  shadow  | 함          | 함       | 함
  live    | 함          | 함       | 함

원고·프롬프트·노션 ID 는 로그에 남기지 않는다 (공개 레포).
사용: python -m p76.run_novel
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from p76 import (
    cli,
    notion,
    novel_calendar,
    novel_diagnostics,
    novel_llm,
    novel_policy,
    novel_prompts,
    novel_rules,
    novel_store,
    novel_tg,
    safety,
    sched,
    settings,
)
from p76.novel_store import (
    H_BIBLE,
    H_BODY,
    H_META,
    H_QUESTION,
    H_QUOTE,
    H_REVIEW,
    H_SUBTITLE,
    H_SUMMARY,
)
from p76.runlog import RunLog

VERSION = "1.5.0"  # 1.5.0: 순연 대상에서 보류·거절확인필요·폐기 제외 · 한도 초과 → 시즌 일시 정지(paused) · live 만 놓친
#        게시일 순연 · D-1 22:00 선제 순연 (리뷰 C2·W2·W3)
# 1.4.0: DN2026_0119 Zero-Touch — 매일 연재 일정·자동 승인·2차 검수·순연·이관
# 1.3.0 · 1.2.1: 바이블 arc 객체 수용 · R05 바이블 경고화 · 재시도 피드백 이월 · 예정 시각 기준 판정
# 1.2.0: 표지 인용·부제·열린 질문도 규칙 검사·AI 검수 대상

logger = logging.getLogger(__name__)
S = settings
SEVERITY = {"PASS": 0, "HUMAN_REQUIRED": 1, "BLOCK": 2}
VERDICTS = ("PASS", "HUMAN_REQUIRED", "BLOCK")
DONE_EPISODE_STATES = (S.NS_REVIEW, S.NS_APPROVED, S.NS_SCHEDULED, S.NS_PUBLISHED, S.NS_CHECK)
JOB_ESTIMATE_USD = {"bible": 2.0, "episode": 1.5}
# DN2026_0119: 게시 측에 넘어간(예약 등록·게시·대조 중) 회차 — 일정 재계산·순연 대상 아님
POSTED_STATES = (S.NS_SCHEDULED, S.NS_PUBLISHED, S.NS_CHECK)
# DN2026_0119 RC-1: 바이블이 이 밖이면 재평가 대상. 이관 가능한 v1 잔존 상태는 BIBLE_MIGRATE_STATES
BIBLE_OK_STATES = (S.NS_CANON, S.NS_GENERATING, S.NS_CHECK_FAILED, S.NS_STOPPED, S.NS_DISCARDED)
BIBLE_MIGRATE_STATES = (S.NS_APPROVED, S.NS_EXPLICIT, S.NS_IMPLICIT)
# 리뷰 C2: 사람 확인·폐기 상태 — 순연(postpone·catch_missed) 대상이 아니다
NO_SHIFT_STATES = (S.NS_HELD, S.NS_REFUSED, S.NS_DISCARDED)
HUMAN_STATES = (S.NS_HELD, S.NS_REFUSED)
# 리뷰 W3: 아직 자동 게이트를 못 넘은 회차 — 게시일 D-1 NOVEL_PREEMPT_TIME 이후면 선제 순연
PRE_APPROVAL_STATES = (S.NS_GENERATING, S.NS_CHECK_FAILED)
# 리뷰 C2(d): 일시 정지 회차가 사람에 의해 이 상태로 바뀌면 정지 해제
RESUME_STATES = (S.NS_APPROVED, S.NS_GENERATING, S.NS_CHECK_FAILED)
H_REVIEW2 = "2차 검수 (JSON)"


class BudgetStop(RuntimeError):
    """예산 상한 — 이번 실행의 LLM 호출 중단."""


@dataclass
class Ctx:
    log: RunLog
    now: datetime
    rows: list[dict]
    ctrl_page: dict
    ctrl: dict
    client: Any = None
    prompts: novel_prompts.Prompts | None = None
    run_cost: float = 0.0
    sent: list[str] = field(default_factory=list)
    sched_now: datetime | None = None  # 예약 실행의 예정 시각(STD-SCHED-03). None 이면 now

    @property
    def kst(self) -> datetime:
        """'오늘'·시간대 판정용 KST 시각 — 예정 시각 기준. 기록·비용 월·due 비교는 now(실제)를 쓴다."""
        return (self.sched_now or self.now).astimezone(notion.KST)

    @property
    def today(self) -> date:
        return self.kst.date()

    @property
    def shifts(self) -> tuple[date, ...]:
        """순연 날짜(CONTROL JSON 'shifts', DN2026_0119 A2)."""
        return novel_calendar.parse_shifts(self.ctrl.get("shifts"))


# ── 일정 (DN2026_0119: novel_calendar — 금요일 휴재 + 순연) ──────────
def season_start(n: int, shifts: tuple = ()) -> date:
    return novel_calendar.season_start(n, shifts)


def due_date(n: int, m: int, shifts: tuple = ()) -> date:
    return novel_calendar.due(n, m, shifts)


def episode_due_for_generation(n: int, m: int, today: date, shifts: tuple = ()) -> bool:
    if today >= due_date(n, m, shifts) - timedelta(days=S.NOVEL_EPISODE_LEAD_DAYS):
        return True
    return (m <= S.NOVEL_BUFFER_EPISODES
            and today >= season_start(n, shifts) - timedelta(days=S.NOVEL_EPISODE_LEAD_DAYS))


def publish_time(d: date) -> datetime:
    """게시일 d 의 FB 예약 게시 시각 T (KST, Variable PAGE76_NOVEL_PUBLISH_HHMM)."""
    return datetime.combine(d, time(*S.NOVEL_PUBLISH_TIME), tzinfo=notion.KST)


def slot_missed(ctx: Ctx, d: date) -> bool:
    """게시일 d 를 이미 놓쳤는가 — 지났거나, 오늘이지만 T 까지 예약 하한(NOVEL_SCHEDULE_MIN_LEAD_MIN)이 안 남음.

    미래 날짜는 False(순연하지 않는다). 실제 시각(now) 기준.
    """
    return publish_time(d) - ctx.now < timedelta(minutes=S.NOVEL_SCHEDULE_MIN_LEAD_MIN)


def preempt_due(ctx: Ctx, d: date) -> bool:
    """리뷰 W3: 게시일 d 의 전날 KST NOVEL_PREEMPT_TIME 이 지났는가 (실제 시각 기준). '승인' 전 회차의 선제 순연 판정."""
    cut = datetime.combine(d - timedelta(days=1), time(*S.NOVEL_PREEMPT_TIME), tzinfo=notion.KST)
    return ctx.now >= cut


def _claimed(page: dict) -> bool:
    """게시 측 선점 표시(posting:)가 남은 행 — FB 예약이 걸렸을 수 있다. 생성 측은 N_DUE·상태를 건드리지 않는다(R6-02 N2)."""
    return any(part.startswith("posting:") for part in notion.plain(page, S.N_RESULT).split("|"))


# ── 일시 정지 (리뷰 C2: CONTROL JSON "paused") ──────────────
def paused(ctx: Ctx) -> tuple[int, int] | None:
    """CONTROL JSON 'paused' → (시즌, 회차). 형식이 틀리면 None."""
    p = ctx.ctrl.get("paused")
    if not isinstance(p, dict):
        return None
    try:
        n, m = int(p.get("season")), int(p.get("episode"))
    except (TypeError, ValueError):
        return None
    return (n, m) if n >= 1 and m >= 1 else None


def in_paused(ctx: Ctx, n: int, m: int) -> bool:
    """회차 (n, m) 이 일시 정지 범위(같은 시즌의 정지 회차 이후)인가. 바이블(m=0)·다른 시즌은 False."""
    p = paused(ctx)
    return p is not None and m >= 1 and n == p[0] and m >= p[1]


def _clear_paused_due(ctx: Ctx) -> None:
    """정지 범위의 미게시 회차 게시예정일(N_DUE)을 비운다 — 게시 측은 N_DUE 없는 '승인'을 등록하지 않는다."""
    for page in _episodes(ctx):
        if (novel_store.state(page) in (S.NS_PUBLISHED, S.NS_CHECK) or novel_store.stopped(page)
                or _claimed(page)):
            continue  # 공개됐거나 대조 중·중단·선점 중 — 건드리지 않는다. 발행예고는 비운다(게시 측 재예약 계약)
        if not in_paused(ctx, novel_store.season(page), novel_store.episode(page)):
            continue
        if notion.kst_date(page, S.N_DUE) is not None:
            novel_store.update(page, {S.N_DUE: notion.v_date(None)})
            ctx.log.writes += 1
    # R6-02 N3: 정지 전에 만든 이 시즌의 휴재 안내(부록) 행은 "제m화는 X일 공개" 약속이 틀리게 되므로 거둔다
    p = paused(ctx)
    for page in ctx.rows:
        if (p is None or novel_store.kind(page) != S.NK_NOTICE or novel_store.stopped(page) or _claimed(page)
                or not in_paused(ctx, novel_store.season(page), novel_store.episode(page))):
            continue  # R6-03: 정지 범위(정지 회차 이후)의 안내 행만 — 앞 회차 순연 안내는 약속이 그대로 맞다
        st = novel_store.state(page)
        if st == S.NS_APPROVED:
            novel_store.transition(page, S.NS_STOPPED, {S.N_RESULT: notion.v_rich("paused_notice")})
            ctx.log.writes += 1
        elif st == S.NS_SCHEDULED and notion.kst_date(page, S.N_DUE) is not None:
            novel_store.update(page, {S.N_DUE: notion.v_date(None)})  # 게시 측이 예약 취소 → 승인
            ctx.log.writes += 1


def pause(ctx: Ctx, row: dict, reason: str) -> None:
    """자동 복구 한도 초과(C2-b): 회차 보류 + CONTROL paused 기록 + T5 1회. 이미 정지 중이면 아무것도 하지 않는다."""
    n, m, key = novel_store.season(row), novel_store.episode(row), novel_store.key(row)
    if paused(ctx) is not None:
        ctx.log.degrade(f"{key}:pause_pending")  # 다른 시즌이 이미 정지 중 — 그 해제 뒤 다시 판정
        return
    if novel_store.state(row) != S.NS_HELD and S.NS_HELD in novel_store.ALLOWED.get(novel_store.state(row), set()):
        novel_store.transition(row, S.NS_HELD, {S.N_RESULT: notion.v_rich(f"shift:limit:{reason}"[:1900])})
        ctx.log.writes += 1
    ctx.ctrl["paused"] = {"season": n, "episode": m, "since": ctx.now.isoformat()}
    _clear_paused_due(ctx)
    ctx.log.degrade(f"{key}:paused")
    logger.warning(f"[Novel] {key} 자동 복구 한도 초과 — 시즌 {n} 제{m}화부터 일시 정지 ({reason})")
    novel_tg.send("T5", f"⚠️ 사건 소설 시즌 {n} 제{m}화 자동 복구 한도 초과 — 연재 일시 정지. 노션 DB-12 확인 필요",
                  novel_tg.page_link(row["id"]))


def check_resume(ctx: Ctx) -> None:
    """C2-d: 정지 회차가 사람에 의해 승인·생성중·검사실패로 바뀌면 정지 해제(그 회차 순연 횟수 초기화) → 정상 흐름."""
    p = paused(ctx)
    if p is None:
        if "paused" in ctx.ctrl:
            ctx.ctrl.pop("paused")
            ctx.log.degrade("paused:invalid")
        return
    key = novel_store.episode_key(*p)
    page = novel_store.find(ctx.rows, key)
    if page is None or novel_store.stopped(page) or novel_store.state(page) not in RESUME_STATES:
        logger.info(f"[Novel] 시즌 {p[0]} 제{p[1]}화부터 일시 정지 중 — 순연·생성 멈춤")
        ctx.log.degrade(f"{key}:paused")
        _clear_paused_due(ctx)  # R6-03: 매 실행 — 게시 측이 예약 취소로 '승인'에 돌린 안내 행도 중단으로 정리
        return
    ctx.ctrl.pop("paused")
    ctx.ctrl.setdefault("shift_count", {}).pop(key, None)
    logger.info(f"[Novel] {key} 상태 {novel_store.state(page)} — 일시 정지 해제, 정상 흐름 재개")


# ── 1 중단 수집 ───────────────────────────────────────────
def _stop(ctx: Ctx, page: dict, reason: str) -> None:
    novel_store.transition(page, S.NS_STOPPED, {S.N_STOP: notion.v_checkbox(True),
                                                S.N_RESULT: notion.v_rich(f"stopped:{reason}")})
    ctx.log.writes += 1
    logger.warning(f"[Novel] {novel_store.key(page)} 중단 ({reason})")


def collect_stops(ctx: Ctx) -> None:
    """DB-12 중단 체크 → 상태 '중단'. 게시 파이프라인도 같은 함수를 쓴다."""
    stopped_keys = []
    for page in ctx.rows:
        if novel_store.kind(page) not in (S.NK_BIBLE, S.NK_EPISODE, S.NK_NOTICE):
            continue
        if S.NS_STOPPED not in novel_store.ALLOWED.get(novel_store.state(page), set()):
            continue
        if novel_store.state(page) in (S.NS_SCHEDULED, S.NS_CHECK) or _claimed(page):
            continue  # FB 예약 객체가 있거나 선점 중(posting:, R6-03)인 행 — 게시 파이프라인이 예약 취소(DELETE) 후 '중단'으로 옮긴다 (DN2026_0119)
        if notion.checkbox(page, S.N_STOP):
            _stop(ctx, page, "notion")
            stopped_keys.append(novel_store.key(page))
    if stopped_keys:
        novel_tg.send("T1d", f"⏸ 중단 접수 — {', '.join(stopped_keys)}\n"
                             f"재개는 노션 DB-12 에서 상태를 되돌리고 중단 체크를 풀어야 합니다.")


# ── LLM 호출 공통 ─────────────────────────────────────────
def _prompts(ctx: Ctx) -> novel_prompts.Prompts:
    if ctx.prompts is None:
        ctx.prompts = novel_prompts.load()
    return ctx.prompts


def _style(p: novel_prompts.Prompts) -> str:
    if not p.style_samples:
        p.style_samples = novel_prompts.style_samples()
    return p.style_samples


def _llm(ctx: Ctx, row: dict, *, label: str, model: str, effort: str, system: str, user_blocks: list[dict],
         schema: dict, max_tokens: int, expected_out: int, stream: bool) -> novel_llm.Result:
    candidate = {}
    repair_fields = set()
    if label.endswith(":write"):
        schema, user_blocks, candidate, repair_fields = novel_policy.repair_request(row, schema, user_blocks)
    chars = len(system) + sum(len(b["text"]) for b in user_blocks)
    est = novel_llm.estimate(model, chars, max_tokens, expected_out)
    if ctx.run_cost + est > S.NOVEL_RUN_CAP_USD:
        raise BudgetStop("budget:run")
    if novel_store.month_spent(ctx.ctrl, ctx.now) + est > S.NOVEL_BUDGET_USD:
        raise BudgetStop("budget:month")
    if ctx.client is None:
        ctx.client = novel_llm.make_client()
    res = novel_llm.call(ctx.client, label=label, model=model, effort=effort, system=system,
                         user_blocks=user_blocks, schema=schema, max_tokens=max_tokens, stream=stream)
    ctx.run_cost += res.cost
    logger.info("[NovelUsage] key=%s stage=%s attempt=%s input=%s output=%s thinking=%s cost=%s",
                novel_store.key(row), label.rsplit(":", 1)[-1], notion.number(row, S.N_ATTEMPTS),
                res.usage.get("input_tokens", 0), res.usage.get("output_tokens", 0),
                res.usage.get("thinking_tokens"), res.cost)
    novel_store.add_month_spent(ctx.ctrl, ctx.now, res.cost)
    novel_store.add_cost(row, res.cost)
    novel_policy.record_usage(row, label, res)
    if res.ok and repair_fields:
        if set(res.data) != repair_fields:
            raise novel_llm.LLMError("repair:unexpected_fields")
        res.data = {**candidate, **res.data}
        if repair_fields != set(candidate):
            _, origin_unverified = novel_policy.candidate_origin(row, candidate)
            res.fallback = res.fallback or origin_unverified
    return res


def computed_verdict(r: dict) -> str:
    fails = any(r.get(k) == "fail" for k in ("victim_blame", "graphic_harm", "vigilante", "premature_guilt"))
    ident, method = int(r.get("identifiability", 3)), int(r.get("method_detail", 3))
    meta = r.get("meta_policy", "high")
    if fails or ident >= 2 or method >= 2 or meta == "high":
        return "BLOCK"
    if r.get("legal_claims") or r.get("advice_tone") or ident == 1 or method == 1 or meta == "med":
        return "HUMAN_REQUIRED"
    return "PASS"


def _worse(a: str, b: str) -> str:
    return a if SEVERITY.get(a, 2) >= SEVERITY.get(b, 2) else b


def review_codes(r: dict) -> str:
    parts = [f"identifiability:{r.get('identifiability')}", f"method_detail:{r.get('method_detail')}",
             f"meta:{r.get('meta_policy')}", f"legal:{len(r.get('legal_claims') or [])}",
             f"advice:{len(r.get('advice_tone') or [])}"]
    parts += [k for k in ("victim_blame", "graphic_harm", "vigilante", "premature_guilt") if r.get(k) == "fail"]
    return ";".join(parts)


def _review_call(ctx: Ctx, row: dict, target: str, text: str, *, model: str, stage: str) -> tuple[str, dict, bool]:
    """검수 1회 — 1차·2차가 같은 시스템 프롬프트·스키마를 쓴다. (판정, 검수 JSON, fallback)."""
    p = _prompts(ctx)
    user = [{"text": f"검수 대상: {target}\n\n[원고]\n{text}"}]
    res = _llm(ctx, row, label=f"{novel_store.key(row)}:{stage}", model=model, effort="medium",
               system=p.systems["nr"], user_blocks=user, schema=p.schemas["nr"], max_tokens=8000,
               expected_out=1500, stream=False)
    verdict = "BLOCK"
    if res.ok:
        verdict = _worse(str(res.data.get("verdict", "BLOCK")), computed_verdict(res.data))
    if res.fallback:
        verdict = _worse(verdict, "HUMAN_REQUIRED")
    data = dict(res.data or {"error": res.refusal or res.stop_reason})
    data["final_verdict"] = verdict
    data["models"] = [res.model]
    data["stage"] = stage
    return verdict, data, res.fallback


def review(ctx: Ctx, row: dict, target: str, text: str, escalate: bool) -> tuple[str, dict, bool]:
    """1차 검수(MODEL_REVIEWER) 1회. 동일 모델 중복 검수 없음 — 2차는 review2(다른 모델)."""
    return _review_call(ctx, row, target, text, model=S.MODEL_REVIEWER, stage="review")


def review2(ctx: Ctx, row: dict, target: str, text: str) -> tuple[str, dict]:
    """독립 2차 검수 (DN2026_0119 §5-1) — 작성·1차 검수와 다른 모델, 1차와 같은 프롬프트·스키마."""
    if S.MODEL_REVIEWER_2 in (S.MODEL_WRITER, S.MODEL_REVIEWER):
        raise novel_llm.LLMError("review2:same_model")
    verdict, data, _ = _review_call(ctx, row, target, text, model=S.MODEL_REVIEWER_2, stage="review2")
    return verdict, data


def write_effort(ctx: Ctx) -> str:
    """DN2026_0119 AC6: 월 누적이 예산 × NOVEL_BUDGET_SAVE_RATIO 이상이면 작성 effort 를 낮춘다."""
    if novel_store.month_spent(ctx.ctrl, ctx.now) >= S.NOVEL_BUDGET_USD * S.NOVEL_BUDGET_SAVE_RATIO:
        logger.info(f"[Novel] 예산 절감 — 작성 effort={S.NOVEL_WRITE_EFFORT_SAVE}")
        return S.NOVEL_WRITE_EFFORT_SAVE
    return S.NOVEL_WRITE_EFFORT


def _fail_row(ctx: Ctx, row: dict, new_state: str, code: str, extra: dict | None = None, note: str = "",
              alert: bool | None = None) -> None:
    """실패 기록. DN2026_0119 AC1: 자동 재시도로 이어지는 실패는 텔레그램 없이 로그만, 사람 확인이 필요한
    종단 상태(보류·거절·진단 보류·시도 소진 바이블)만 T5."""
    novel_store.transition(row, new_state, {S.N_RESULT: notion.v_rich(code[:1900]), **(extra or {})})
    ctx.log.writes += 1
    ctx.log.degrade(f"{novel_store.key(row)}:{code}")
    attempts = notion.number(row, S.N_ATTEMPTS) or 0
    exhausted = attempts >= S.NOVEL_MAX_ATTEMPTS
    if alert is None:
        alert = (new_state in (S.NS_HELD, S.NS_REFUSED) or code.endswith(":hold")
                 or (exhausted and novel_store.kind(row) == S.NK_BIBLE))
    if not alert:
        logger.info(f"[Novel] {novel_store.key(row)} {new_state} — {code} (시도 {attempts}/{S.NOVEL_MAX_ATTEMPTS}, 자동 재시도)")
        return
    tail = f" · 생성 {attempts}/{S.NOVEL_MAX_ATTEMPTS}회"
    if exhausted:
        tail += " · 자동 생성 종료, 미완성·안전 검수 실패 원고는 발행하지 않음"
    if note:
        tail += f" · {note}"
    novel_tg.send("T5", f"⚠️ {novel_store.key(row)} {new_state} — {code}{tail}", novel_tg.page_link(row["id"]))


RULES_PREFIX = "rules:"
REPEAT_NOTE = "동일 규칙 반복 — 자동 재시도 중지, 확인 필요"


def prev_rule_codes(row: dict) -> list[str]:
    """직전 실행이 규칙 검사로 실패한 행이면 그 규칙 코드 목록(결과코드 'rules:<코드,…>'), 아니면 []."""
    if novel_store.state(row) != S.NS_CHECK_FAILED:
        return []
    result = notion.plain(row, S.N_RESULT)
    if not result.startswith(RULES_PREFIX):
        return []
    return [c for c in result[len(RULES_PREFIX):].split(",") if c]


def carry_note(codes: list[str]) -> str:
    """첫 시도에 붙이는 직전 실행 피드백 — 코드 문자열만(원고 없음, STD-LLM-11)."""
    if not codes:
        return ""
    return f"\n\n직전 실행이 다음 규칙에 걸렸다: {', '.join(codes)}. 해당 부분을 고쳐 다시 만들어 주세요."


def _rule_kinds(codes: list[str]) -> set[str]:
    """'R01:length:3012' → 'R01:length'. 값(글자 수 등)이 바뀌어도 같은 결함으로 본다(R6 W5)."""
    return {":".join(c.split(":")[:2]) for c in codes}


def _fail_rules(ctx: Ctx, row: dict, errors: list[str], prev_codes: list[str]) -> None:
    """규칙 실패를 다음 실행의 부분 수정 입력으로 보존한다. 총 3회 상한은 next_job이 적용."""
    code = RULES_PREFIX + ",".join(errors)
    _fail_row(ctx, row, S.NS_CHECK_FAILED, code)
    if novel_store.kind(row) == S.NK_EPISODE and (notion.number(row, S.N_ATTEMPTS) or 0) >= S.NOVEL_MAX_ATTEMPTS:
        _final_fail(ctx, row, "rules:exhausted")  # DN2026_0119 A2(a)


def _start_attempt(ctx: Ctx, row: dict, tag: str) -> None:
    if novel_store.state(row) == S.NS_CHECK_FAILED:
        novel_store.transition(row, S.NS_GENERATING)
    attempts = (notion.number(row, S.N_ATTEMPTS) or 0) + 1
    if attempts > S.NOVEL_MAX_ATTEMPTS:
        raise novel_llm.LLMError("attempts:exhausted")
    novel_store.update(row, {S.N_ATTEMPTS: notion.v_number(attempts), S.N_PROMPT_VER: notion.v_rich(tag)})
    ctx.log.writes += 1


# ── DN2026_0119 자동 게이트 · 순연 · 이관 ─────────────────
def _approve(ctx: Ctx, row: dict, code: str) -> None:
    """자동 게이트 통과 → 회차 '승인' / 바이블 '정본' (A1: 게이트 통과 = 승인 위임). 알림은 정보성."""
    n = novel_store.season(row)
    if novel_store.kind(row) == S.NK_BIBLE:
        novel_store.transition(row, S.NS_CANON, {S.N_RESULT: notion.v_rich(code)})
        ctx.log.writes += 1
        logger.info(f"[Novel] {novel_store.key(row)} 정본 ({code})")
        first = season_start(n, ctx.shifts)
        novel_tg.send("T1c", f"✅ 사건 소설 시즌 {n} 바이블 정본 자동 등록({code}) — 제1화 게시 예정 "
                             f"{novel_calendar.label(first)}, 원고 생성 "
                             f"{first - timedelta(days=S.NOVEL_EPISODE_LEAD_DAYS):%m-%d}부터 (조치 불필요)")
        return
    m = novel_store.episode(row)
    novel_store.transition(row, S.NS_APPROVED, {S.N_RESULT: notion.v_rich(code)})
    ctx.log.writes += 1
    chars = notion.number(row, S.N_CHARS) or 0
    logger.info(f"[Novel] {novel_store.key(row)} 승인 ({code}, {chars}자)")
    novel_tg.send("T2", f"✅ 시즌 {n} 제{m}화 자동 승인({code}) — 게시 {novel_calendar.label(due_date(n, m, ctx.shifts))}"
                        f" / {chars}자 / 검수 {notion.select(row, S.N_VERDICT) or '-'} (조치 불필요)",
                  novel_tg.page_link(row["id"]))


def _record_review2(ctx: Ctx, row: dict, verdict: str, data: dict) -> None:
    novel_store.append(row, novel_store.section_blocks([(H_REVIEW2, json.dumps(data, ensure_ascii=False, indent=1),
                                                         "json")]))
    codes = notion.plain(row, S.N_REVIEW_CODES)
    novel_store.update(row, {S.N_VERDICT: notion.v_select(verdict),
                             S.N_REVIEW_CODES: notion.v_rich(f"{codes}|r2:{review_codes(data)}"[:1900])})
    ctx.log.writes += 2


def _gate(ctx: Ctx, row: dict, verdict: str, review_json: dict, target: str, text: str) -> None:
    """1차 검수 뒤 상태 기계 (§5-1). verdict 는 작성 fallback 이 반영된 값(fallback → HUMAN_REQUIRED 이상)."""
    if verdict == "PASS":
        _approve(ctx, row, "auto:gate1")
        return
    gate = novel_policy.gate(row)
    attempts = notion.number(row, S.N_ATTEMPTS) or 0
    if gate["repairs"] < S.NOVEL_REPAIR_MAX and attempts < S.NOVEL_MAX_ATTEMPTS:
        gate["repairs"] += 1
        novel_policy.save_gate(row, gate)
        code = novel_policy.review_result_code(verdict, review_codes(review_json))
        novel_store.transition(row, S.NS_CHECK_FAILED, {S.N_RESULT: notion.v_rich(code[:1900])})
        ctx.log.writes += 2
        logger.info(f"[Novel] {novel_store.key(row)} 검수 {verdict} — 자동 수정 {gate['repairs']}/{S.NOVEL_REPAIR_MAX}")
        return
    if verdict == "BLOCK":  # 수정 뒤에도 BLOCK → 2차 검수로 뒤집지 않는다(설계 §5-1)
        _final_fail(ctx, row, "gate1:BLOCK")
        return
    v2, data = review2(ctx, row, target, text)
    _record_review2(ctx, row, v2, data)
    if v2 == "PASS":
        _approve(ctx, row, "auto:gate2")
        return
    _final_fail(ctx, row, f"gate2:{v2}")


def _final_fail(ctx: Ctx, row: dict, code: str) -> None:
    """자동 게이트 최종 실패. 회차: 순연(A2-a) + 시도·수정 횟수 초기화 → 재생성 대상. 바이블: 보류 + T5."""
    if novel_store.kind(row) == S.NK_BIBLE:
        if S.NS_HELD in novel_store.ALLOWED.get(novel_store.state(row), set()):
            _fail_row(ctx, row, S.NS_HELD, f"final:{code}", alert=True)
        return
    if novel_store.state(row) == S.NS_GENERATING:
        novel_store.transition(row, S.NS_CHECK_FAILED)
    novel_store.update(row, {S.N_ATTEMPTS: notion.v_number(0), S.N_RESULT: notion.v_rich(f"final:{code}")})
    novel_policy.save_gate(row, {"repairs": 0})
    ctx.log.writes += 2
    ctx.log.degrade(f"{novel_store.key(row)}:final:{code}")
    postpone(ctx, row, f"final:{code}")


def _notice_row(ctx: Ctx, n: int, m: int, d: date, next_d: date) -> None:
    """순연 슬롯 d 의 휴재 안내 카드 행 (DB-12 구분 '부록', 상태 '승인'). 원고 없음 — 고정 문구만."""
    key = f"N{n}-NOTICE-{d:%Y%m%d}"
    if novel_store.find(ctx.rows, key) is not None:
        return
    page = novel_store.create(ctx.rows, key, S.NK_NOTICE, n=n, m=m, due=d)
    caption = (f"📢 사건 소설 휴재 안내\n\n오늘은 연재를 하루 쉬어 갑니다.\n"
               f"시즌 {n} 제{m}화는 {novel_calendar.label(next_d)}에 공개됩니다.\n\n#사건파일76 #사건소설")
    novel_store.append(page, novel_store.section_blocks([
        (H_BODY, caption, "code"),
        (H_QUOTE, "오늘은 하루 쉬어 갑니다", "code"),
        (H_SUBTITLE, f"시즌 {n} 제{m}화 · 다음 공개 {novel_calendar.label(next_d)}", "code"),
        (H_META, json.dumps({"kind": "hiatus_notice", "season": n, "episode": m, "next_due": next_d.isoformat()},
                            ensure_ascii=False), "json"),
    ]))
    novel_store.transition(page, S.NS_APPROVED, {S.N_RESULT: notion.v_rich("auto:notice")})
    ctx.log.writes += 3


def postpone(ctx: Ctx, row: dict, reason: str) -> None:
    """회차 순연(A2): 그 게시일(+ 이어서 놓친 게시일)을 shifts 에 넣고, 휴재 안내 행을 만들고, 미게시 회차 N_DUE 를
    다시 계산한다. 보류·거절확인필요·폐기 회차와 일시 정지 범위는 순연하지 않는다(C2-a). 같은 회차 순연이
    NOVEL_SHIFT_MAX 회를 넘으려 하면 순연 대신 보류 + 시즌 일시 정지 + T5 1회(C2-b, 유일한 사람 확인 예외)."""
    n, m, key = novel_store.season(row), novel_store.episode(row), novel_store.key(row)
    if S.MODE != "live":  # W2 확장(통합 결정): shadow·dry_run 은 게시가 없으므로 최종 실패 순연도 일정에 남기지 않는다
        logger.info(f"[Novel] {key} 순연 생략 (mode={S.MODE}, {reason})")
        return
    if novel_store.state(row) in NO_SHIFT_STATES or in_paused(ctx, n, m):
        logger.info(f"[Novel] {key} 순연 생략 (상태 {novel_store.state(row)} · 정지 {paused(ctx)})")
        return
    counts = ctx.ctrl.setdefault("shift_count", {})
    if int(counts.get(key, 0)) + 1 > S.NOVEL_SHIFT_MAX:  # C2-b: 한도 초과 — 더 순연하지 않고 시즌 일시 정지
        counts[key] = int(counts.get(key, 0)) + 1
        pause(ctx, row, reason)
        return
    shifts = list(ctx.shifts)
    added: list[date] = []
    pre = novel_store.state(row) in PRE_APPROVAL_STATES
    for _ in range(60):  # 이어서 놓친(승인 전이면 선제 순연 시각이 지난) 게시일까지 한 번의 순연으로 처리
        d = due_date(n, m, tuple(shifts))
        if added and not (slot_missed(ctx, d) or (pre and preempt_due(ctx, d))):
            break
        shifts.append(d)
        added.append(d)
    ctx.ctrl["shifts"] = [d.isoformat() for d in novel_calendar.parse_shifts(shifts)]
    counts[key] = int(counts.get(key, 0)) + 1
    new_due = due_date(n, m, ctx.shifts)
    for d in added:
        if slot_missed(ctx, d):
            continue  # R6-02 P4: 이미 지난 슬롯에는 안내 행을 만들지 않는다(곧바로 만료될 행) — shifts 에만 반영
        _notice_row(ctx, n, m, d, new_due)
    sync_due(ctx)
    logger.info(f"[Novel] {key} 순연 {len(added)}일 ({reason}) → {new_due:%m-%d} · 누적 {counts[key]}회")
    ctx.log.degrade(f"{key}:shift")
    with_card = [novel_calendar.label(d) for d in added if not slot_missed(ctx, d)]
    no_card = [novel_calendar.label(d) for d in added if slot_missed(ctx, d)]
    parts = ([f"휴재 안내 {', '.join(with_card)}"] if with_card else []) + \
            ([f"휴재(안내 없음) {', '.join(no_card)}"] if no_card else [])
    novel_tg.send("T8", f"🗓 {key} 순연 — {' · '.join(parts)}, "
                        f"다음 공개 {novel_calendar.label(new_due)} (자동 처리, 조치 불필요)")


def _episodes(ctx: Ctx) -> list[dict]:
    eps = [p for p in ctx.rows if novel_store.kind(p) == S.NK_EPISODE
           and novel_store.season(p) >= 1 and novel_store.episode(p) >= 1]
    return sorted(eps, key=lambda p: novel_calendar.global_index(novel_store.season(p), novel_store.episode(p)))


def sync_due(ctx: Ctx) -> None:
    """게시 전 회차의 게시예정일(N_DUE)을 일정 엔진 값으로 맞춘다 (shifts 반영)."""
    for page in _episodes(ctx):
        # 발행예고(예약 등록됨)도 다시 계산한다 — 게시 측이 날짜 불일치를 보고 예약 취소 후 재등록(DN2026_0119 P9).
        # 게시완료·확인필요는 이미 공개됐거나 공개 여부 확인 중이라 건드리지 않는다.
        if novel_store.state(page) in (S.NS_PUBLISHED, S.NS_CHECK) or novel_store.stopped(page) or _claimed(page):
            continue  # 선점 중(posting:) 행은 FB 예약이 걸렸을 수 있어 날짜를 옮기지 않는다(R6-02 N2)
        if in_paused(ctx, novel_store.season(page), novel_store.episode(page)):
            continue  # C2-c: 정지 범위는 N_DUE 를 비워 둔다(_clear_paused_due)
        calc = due_date(novel_store.season(page), novel_store.episode(page), ctx.shifts)
        if notion.kst_date(page, S.N_DUE) != calc:
            novel_store.update(page, {S.N_DUE: notion.v_date(calc)})
            ctx.log.writes += 1


def _late(ctx: Ctx) -> tuple[dict, str] | None:
    """순서상 첫 번째로 손봐야 할 회차와 사유. 게시 측에 넘어간 행·중단·폐기·정지 범위는 보지 않는다."""
    for p in _episodes(ctx):
        st, n, m = novel_store.state(p), novel_store.season(p), novel_store.episode(p)
        if (st in POSTED_STATES or st == S.NS_DISCARDED or novel_store.stopped(p) or notion.plain(p, S.N_FB_ID)
                or _claimed(p)
                or in_paused(ctx, n, m)):
            continue
        d = due_date(n, m, ctx.shifts)
        if st in HUMAN_STATES:
            # 사람 확인 회차는 순연하지 않는다(C2-a). 게시일을 놓쳤고 정지가 없으면 시즌 일시 정지(T5 1회)
            if slot_missed(ctx, d) and paused(ctx) is None:
                return p, "held"
            continue
        if slot_missed(ctx, d):
            return p, "missed"
        if st in PRE_APPROVAL_STATES and preempt_due(ctx, d):
            return p, "preempt"
    return None


def catch_missed(ctx: Ctx) -> None:
    """놓친 게시일(A2-b): 게시 측에 넘어가지 않은 회차의 게시일이 지났거나 오늘인데 예약 하한을 못 맞춤 → 순연.
    '승인' 전(생성중·검사실패) 회차는 게시일 D-1 KST NOVEL_PREEMPT_TIME 이후 선제 순연(W3) — 휴재 안내 행의
    게시예정일은 원래 게시일이라 게시 측이 그날 아침 실행에서 등록할 수 있다.
    live 가 아니면 건너뛴다(W2): shadow·dry_run 은 게시 측이 예약하지 않아 순연이 매일 쌓이기 때문."""
    if S.MODE != "live":
        logger.info(f"[Novel][{S.MODE}] 놓친 게시일 순연 판정 생략 (live 만)")
        return
    for _ in range(20):
        found = _late(ctx)
        if found is None:
            return
        page, reason = found
        if reason == "held":
            pause(ctx, page, "held")
        else:
            postpone(ctx, page, reason)


def recover_exhausted(ctx: Ctx) -> None:
    """시도를 다 쓴 채 남은 회차(v1 잔존 포함) → 최종 실패 처리(A2-a). 진단 보류(api:…:hold)는 제외."""
    for page in _episodes(ctx):
        result = notion.plain(page, S.N_RESULT)
        if (novel_store.state(page) == S.NS_CHECK_FAILED and not novel_store.stopped(page)
                and not in_paused(ctx, novel_store.season(page), novel_store.episode(page))
                and (notion.number(page, S.N_ATTEMPTS) or 0) >= S.NOVEL_MAX_ATTEMPTS
                and not (result.startswith("api:") and result.endswith(":hold"))):
            _final_fail(ctx, page, "attempts")


def _regenerate(ctx: Ctx, page: dict, code: str) -> None:
    if S.NS_GENERATING not in novel_store.ALLOWED.get(novel_store.state(page), set()):
        # novel_store 1.1.0 전이 표에 '승인'→'생성중' 이 없다 — 재생성 대신 보류 + T5 (사람 확인)
        _fail_row(ctx, page, S.NS_HELD, code, alert=True)
        return
    novel_store.transition(page, S.NS_GENERATING, {S.N_RESULT: notion.v_rich(code[:1900]),
                                                   S.N_ATTEMPTS: notion.v_number(0)})
    novel_policy.save_gate(page, {"repairs": 0})
    ctx.log.writes += 2


def _migrate_bible(ctx: Ctx, page: dict) -> None:
    bible = novel_store.json_section(page, H_BIBLE)
    errors = ["bible:unreadable"] if not bible else novel_rules.check_bible(
        bible, banned_terms=S.BANNED_TERMS, journal_numbers=set(novel_prompts.published_journals()))
    if errors:
        _regenerate(ctx, page, "migrate:rules")
        logger.info(f"[Novel] {novel_store.key(page)} 이관 — 규칙 미통과, 재생성")
        return
    v2, data = review2(ctx, page, "바이블", json.dumps(bible, ensure_ascii=False))
    _record_review2(ctx, page, v2, data)
    if v2 == "PASS":
        _approve(ctx, page, "auto:migrate")
        return
    _regenerate(ctx, page, f"migrate:{v2}")
    logger.info(f"[Novel] {novel_store.key(page)} 이관 — 2차 검수 {v2}, 재생성")


def _migrate_episode(ctx: Ctx, page: dict) -> None:
    verdict = notion.select(page, S.N_VERDICT)
    if verdict == "PASS" and notion.plain(page, S.N_MODEL) == S.MODEL_WRITER:
        novel_store.transition(page, S.NS_APPROVED, {S.N_RESULT: notion.v_rich("auto:migrate")})
        ctx.log.writes += 1
        logger.info(f"[Novel] {novel_store.key(page)} 이관 — 승인(auto:migrate)")
        return
    code = novel_policy.review_result_code(verdict or "HUMAN_REQUIRED", notion.plain(page, S.N_REVIEW_CODES) or "-")
    _regenerate(ctx, page, code)
    logger.info(f"[Novel] {novel_store.key(page)} 이관 — 자동 수정 경로")


def migrate(ctx: Ctx) -> None:
    """v1 잔존 상태 이관(§5-3) · RC-1 방지(바이블이 허용 상태 밖이면 재평가)."""
    for page in list(ctx.rows):
        if novel_store.stopped(page):
            continue
        st, kd = novel_store.state(page), novel_store.kind(page)
        if kd == S.NK_BIBLE and st not in BIBLE_OK_STATES:
            if st in BIBLE_MIGRATE_STATES:
                _migrate_bible(ctx, page)
            elif S.NS_GENERATING in novel_store.ALLOWED.get(st, set()):
                _regenerate(ctx, page, f"migrate:{st}")
            else:  # 보류·거절확인필요 — 사람 확인 상태, 자동으로 바꾸지 않는다
                ctx.log.degrade(f"{novel_store.key(page)}:bible_state")
        elif kd == S.NK_EPISODE and st == S.NS_REVIEW:
            _migrate_episode(ctx, page)


def _guarded(ctx: Ctx, step) -> None:
    """LLM 을 부를 수 있는 보조 단계 — 예산·API 오류는 이번 실행에서 이 단계만 멈춘다."""
    try:
        step(ctx)
    except BudgetStop as e:
        _budget_alert(ctx, str(e))
    except novel_llm.LLMError as e:
        ctx.log.fail(str(e))


# ── 3 생성: 바이블 ────────────────────────────────────────
def _bible_user(ctx: Ctx, n: int, journals: dict[int, str]) -> str:
    prev = []
    for page in ctx.rows:
        if novel_store.kind(page) == S.NK_BIBLE and novel_store.season(page) < n:
            b = novel_store.json_section(page, H_BIBLE)
            aliases = ",".join(c.get("alias", "") for c in b.get("cast") or [])
            prev.append(f"시즌 {novel_store.season(page)}: {','.join(b.get('case_types') or [])} / {aliases}")
    pms = novel_prompts.memo_patterns()
    pm_text = "\n".join(f"{p['id']} | {p['types']} | {p['pattern']} | {p['direction']}" for p in pms) or "없음"
    j_text = "\n\n".join(f"#{no:03d}\n{text}" for no, text in sorted(journals.items())) or "없음"
    return (f"시즌 번호: {n}\n직전 사건 소설 시즌: {'; '.join(prev) or '없음'}\n"
            f"최근 수사일지 시즌의 사건유형: 수사일지 본문에서 판단\n\n"
            f"[소재 메모 패턴 — 추상 요약만]\n{pm_text}\n\n[게시된 수사일지 — 승격 후보]\n{j_text}\n\n"
            f"위 재료로 시즌 {n} 바이블을 만들어 주세요.")


ARC_KEY = re.compile(r"^e(\d+)$")


def normalize_arc(bible: dict) -> dict:
    """arc 가 객체({'e1': {...}, …})면 키 정수 순 목록으로 바꾸고 episode 를 키의 정수로 덮어쓴다.

    키 형식이 틀린 항목은 버리지 않고 episode=0 으로 넣어 B01(arc_incomplete)이 잡히게 한다. 목록이면 그대로.
    """
    arc = bible.get("arc") if isinstance(bible, dict) else None
    if not isinstance(arc, dict):
        return bible
    good: list[tuple[int, dict]] = []
    bad: list[dict] = []
    for k, v in arc.items():
        m = ARC_KEY.match(str(k))
        item = dict(v) if isinstance(v, dict) else {"value": v}
        if m and isinstance(v, dict):
            item["episode"] = int(m.group(1))
            good.append((int(m.group(1)), item))
        else:
            item["episode"] = 0
            bad.append(item)
    return {**bible, "arc": [it for _, it in sorted(good, key=lambda x: x[0])] + bad}


def generate_bible(ctx: Ctx, n: int, row: dict | None) -> None:
    p = _prompts(ctx)
    if row is None:
        row = novel_store.create(ctx.rows, novel_store.bible_key(n), S.NK_BIBLE, n=n)
        ctx.log.writes += 1
    prev_codes = prev_rule_codes(row)
    journals = novel_prompts.published_journals()
    candidate = novel_store.json_section(row, novel_policy.H_DRAFT) if prev_codes else {}
    candidate_errors = (novel_rules.check_bible(candidate, banned_terms=S.BANNED_TERMS,
                        journal_numbers=set(journals)) if candidate else [])
    # Reuse only a complete draft from this prompt version after fresh checks.
    reuse = bool(candidate and not candidate_errors
                 and notion.plain(row, S.N_PROMPT_VER) == p.tag("nb"))
    if reuse:
        model, fallback = novel_policy.candidate_origin(row, candidate)
        res = novel_llm.Result(candidate, "end_turn", model, 0.0, fallback=fallback)
        novel_store.transition(row, S.NS_GENERATING)
        bible = candidate
        logger.info("[Novel] saved bible revalidated; writer skipped")
    else:
        if candidate_errors:
            # Upgrade legacy location-less codes before selecting repair fields.
            novel_store.update(row, {S.N_RESULT: notion.v_rich(RULES_PREFIX + ",".join(candidate_errors))})
            prev_codes = candidate_errors
        _start_attempt(ctx, row, p.tag("nb"))
        system = p.systems["nb"] + "\n\n[문체 견본]\n" + (_style(p) or "없음")
        user = _bible_user(ctx, n, journals)
        note = carry_note(prev_codes)
        res = _llm(ctx, row, label=f"{novel_store.key(row)}:write", model=S.MODEL_WRITER, effort=write_effort(ctx),
                   system=system, user_blocks=[{"text": user + note}], schema=p.schemas["nb"], max_tokens=S.NOVEL_WRITE_MAX_TOKENS,
                   expected_out=12000, stream=True)
        if res.refusal:
            _fail_row(ctx, row, S.NS_REFUSED, f"refusal:{res.refusal}")
            return
        if not res.ok:
            _fail_row(ctx, row, S.NS_CHECK_FAILED, f"llm:{res.stop_reason}")
            return
        bible = normalize_arc(res.data)
        novel_policy.save_candidate(row, bible, res)
    errors = novel_rules.check_bible(bible, banned_terms=S.BANNED_TERMS, journal_numbers=set(journals))
    if any(e.startswith("R03") for e in errors):
        _fail_row(ctx, row, S.NS_HELD, "rules:R03")
        return
    if errors:
        _fail_rules(ctx, row, errors, prev_codes)
        return
    warnings = novel_rules.bible_warnings(bible)
    if warnings:  # 코드만 남긴다(본문·매칭 문자열 없음, SEC RR-06)
        logger.warning(f"[Novel] {novel_store.key(row)} 경고 {','.join(warnings)}")
    writer_fallback = res.fallback
    verdict, review_json, review_fallback = review(ctx, row, "바이블", json.dumps(bible, ensure_ascii=False),
                                                   escalate=False)
    if writer_fallback:
        verdict = _worse(verdict, "HUMAN_REQUIRED")
        review_json["final_verdict"] = verdict
    src = bible.get("source") or {}
    summary = (f"{bible.get('season_title', '')}\n{bible.get('logline', '')}\n"
               f"사건유형 {','.join(bible.get('case_types') or [])} · 소재 {src.get('kind', '')}")
    novel_store.append(row, novel_store.section_blocks([
        (H_SUMMARY, summary, "text"),
        (H_BIBLE, json.dumps(bible, ensure_ascii=False, indent=1), "json"),
        (H_REVIEW, json.dumps(review_json, ensure_ascii=False, indent=1), "json"),
    ]))
    props = {S.N_VERDICT: notion.v_select(verdict), S.N_REVIEW_CODES: notion.v_rich(review_codes(review_json)),
             S.N_MODEL: notion.v_rich(res.model), S.N_QUOTED: notion.v_rich(
                 ",".join(f"#{x:03d}" for x in src.get("promoted_journals") or []))}
    novel_store.update(row, props)
    ctx.log.writes += 2
    # DN2026_0119: 묵시 2시간 대기 삭제 — PASS 면 즉시 정본, 그 외 자동 수정 → 2차 검수
    _gate(ctx, row, verdict, review_json, "바이블", json.dumps(bible, ensure_ascii=False))


# ── 3 생성: 회차 ──────────────────────────────────────────
def _episode_inputs(ctx: Ctx, n: int, m: int) -> tuple[dict, dict, list[dict]]:
    bible_page = novel_store.find(ctx.rows, novel_store.bible_key(n))
    bible = novel_store.json_section(bible_page, H_BIBLE) if bible_page else {}
    arc = next((a for a in bible.get("arc") or [] if int(a.get("episode", 0)) == m), {})
    prevs = [novel_store.find(ctx.rows, novel_store.episode_key(n, k)) for k in (m - 1, m - 2) if k >= 1]
    return bible, arc, [p for p in prevs if p is not None]


PUBLIC_LABELS = {"card_quote": "카드 표지 인용", "subtitle": "부제", "question": "열린 질문(캡션 꼬리말)"}


def public_fields(ep: dict) -> dict[str, str]:
    """본문 밖에서 공개되는 문구 — 카드 이미지(표지 인용·부제)와 캡션 꼬리말(열린 질문)."""
    return {k: str(ep.get(k) or "") for k in novel_rules.PUBLIC_FIELDS}


def review_text(body: str, fields: dict[str, str]) -> str:
    """AI 검수 입력 = 본문 + 공개 문구 전부(게시되는 텍스트는 모두 검수를 거친다)."""
    extra = "\n\n".join(f"[{PUBLIC_LABELS[k]}]\n{fields[k]}" for k in novel_rules.PUBLIC_FIELDS if fields.get(k))
    return body if not extra else f"{body}\n\n{extra}"


def generate_episode(ctx: Ctx, n: int, m: int, row: dict | None) -> None:
    p = _prompts(ctx)
    bible, arc, prevs = _episode_inputs(ctx, n, m)
    if not bible or not arc:
        ctx.log.fail(f"N{n}-E{m:02d}:bible_unreadable")
        return
    if row is None:
        row = novel_store.create(ctx.rows, novel_store.episode_key(n, m), S.NK_EPISODE, n=n, m=m,
                                 due=due_date(n, m, ctx.shifts))
        ctx.log.writes += 1
    prev_codes = prev_rule_codes(row)
    _start_attempt(ctx, row, p.tag("ne"))
    prev_meta = novel_store.json_section(prevs[0], H_META) if prevs else {}
    prev_bodies = tuple(novel_store.sections(pg).get(H_BODY, "") for pg in prevs)
    summaries = "\n".join(f"제{m - i}화: {novel_store.json_section(pg, H_META).get('summary', '-')}"
                          for i, pg in enumerate(prevs, start=1)) or "없음"
    quote_nos = [int(x) for x in arc.get("quote_journals") or []]
    journals = novel_prompts.journal_text(quote_nos) if quote_nos else {}
    system = (p.systems["ne"].replace("{cliches}", "\n".join(p.cliches))
              .replace("{style_samples}", _style(p) or "없음"))
    head = [{"text": "[바이블]\n" + json.dumps(bible, ensure_ascii=False), "cache": True}]
    rest = (f"[연속성 대장 — 직전 화까지]\n{json.dumps(prev_meta.get('continuity'), ensure_ascii=False) if prevs else '없음'}"
            f"\n\n[직전 2화 요약]\n{summaries}\n\n[이번 화]\n제{m}화 / {arc.get('stage', '')} / "
            f"D+{arc.get('d_plus', '')} / {arc.get('place', '')}\n핵심 장면: {' / '.join(arc.get('beats') or [])}\n"
            f"인용할 수사일지 원문: "
            + ("\n".join(f"#{k:03d}\n{v}" for k, v in journals.items()) if journals else "없음"))
    prev_d = (prev_meta.get("continuity") or {}).get("d_plus_end") if prevs else None
    note = carry_note(prev_codes)
    res = _llm(ctx, row, label=f"{novel_store.key(row)}:write", model=S.MODEL_WRITER, effort=write_effort(ctx),
               system=system, user_blocks=head + [{"text": rest + note}], schema=p.schemas["ne"],
               max_tokens=S.NOVEL_WRITE_MAX_TOKENS, expected_out=9000, stream=True)
    if res.refusal:
        _fail_row(ctx, row, S.NS_REFUSED, f"refusal:{res.refusal}")
        return
    if not res.ok:
        _fail_row(ctx, row, S.NS_CHECK_FAILED, f"llm:{res.stop_reason}")
        return
    ep = res.data
    novel_policy.save_candidate(row, ep, res)
    errors = novel_rules.check_episode(
        ep.get("body", ""), m, banned_terms=S.BANNED_TERMS, cliches=p.cliches, prev_bodies=prev_bodies,
        journals=journals, d_plus_end=(ep.get("continuity") or {}).get("d_plus_end"), prev_d_plus_end=prev_d,
    ) + novel_rules.question_ok(ep.get("question", "")) + novel_rules.check_public_fields(
        public_fields(ep), m, banned_terms=S.BANNED_TERMS)
    if any(e.startswith("R03") for e in errors):
        _fail_row(ctx, row, S.NS_HELD, "rules:R03")
        return
    if errors and not novel_policy.allow_quality(row, errors, ep):
        _fail_rules(ctx, row, errors, prev_codes)
        return
    writer_fallback = res.fallback
    final = m >= S.NOVEL_EPISODES
    target = f"제{m}화 (최종화 여부: {'예' if final else '아니오'})"
    text = review_text(ep["body"], public_fields(ep))
    verdict, review_json, _ = review(ctx, row, target, text, escalate=m >= S.NOVEL_EPISODES - 1)
    if writer_fallback:
        verdict = _worse(verdict, "HUMAN_REQUIRED")
        review_json["final_verdict"] = verdict
    body = ep["body"]
    meta = {k: ep.get(k) for k in ("continuity", "summary", "quoted_journals", "placeholders_used", "self_check")}
    meta["review"] = review_json
    if errors:
        meta["quality_warnings"] = errors
        meta["quality_hash"] = novel_policy.content_hash(ep)
    novel_store.append(row, novel_store.section_blocks([
        (H_BODY, body, "code"),
        (H_QUESTION, ep.get("question", ""), "code"),
        (H_QUOTE, ep.get("card_quote", ""), "code"),
        (H_SUBTITLE, ep.get("subtitle", ""), "code"),
        (H_META, json.dumps(meta, ensure_ascii=False, indent=1), "json"),
    ]))
    novel_store.update(row, {
        S.N_VERDICT: notion.v_select(verdict),
        S.N_REVIEW_CODES: notion.v_rich(review_codes(review_json)),
        S.N_MODEL: notion.v_rich(res.model),
        S.N_HASH: notion.v_rich(hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]),
        S.N_CHARS: notion.v_number(novel_rules.body_chars(body)),
        S.N_QUOTED: notion.v_rich(",".join(f"#{int(x):03d}" for x in ep.get("quoted_journals") or [])),
    })
    ctx.log.writes += 2
    # DN2026_0119 §5-1: '검토대기'(회장 검토) 없음 — 자동 게이트(gate1 → 자동 수정 → 2차 검수)
    _gate(ctx, row, verdict, review_json, target, text)
    if errors and novel_store.state(row) == S.NS_APPROVED:
        novel_policy.notify_quality(row, ctx.log)


# ── 3 생성: 다음 작업 고르기 ──────────────────────────────
def _retryable(page: dict) -> bool:
    result = notion.plain(page, S.N_RESULT)
    if result.startswith("api:") and result.endswith(":hold"):
        return False
    return (novel_store.state(page) in (S.NS_GENERATING, S.NS_CHECK_FAILED)
            and (notion.number(page, S.N_ATTEMPTS) or 0) < S.NOVEL_MAX_ATTEMPTS
            and not notion.checkbox(page, S.N_STOP))


def next_job(ctx: Ctx) -> tuple[str, int, int, dict | None] | None:
    bibles = {novel_store.season(p): p for p in ctx.rows if novel_store.kind(p) == S.NK_BIBLE}
    for n, page in sorted(bibles.items()):
        if _retryable(page):
            return "bible", n, 0, page
    for n, bible in sorted(bibles.items()):
        if novel_store.state(bible) != S.NS_CANON or novel_store.stopped(bible):
            continue
        for m in range(1, S.NOVEL_EPISODES + 1):
            if in_paused(ctx, n, m):
                break  # C2-c: 일시 정지 시즌은 이후 회차 원고를 만들지 않는다
            row = novel_store.find(ctx.rows, novel_store.episode_key(n, m))
            if row is None:
                if episode_due_for_generation(n, m, ctx.today, ctx.shifts):
                    return "episode", n, m, None
                break
            if _retryable(row) and episode_due_for_generation(n, m, ctx.today, ctx.shifts):
                return "episode", n, m, row
            if novel_store.state(row) not in DONE_EPISODE_STATES:
                break
    if any(novel_store.stopped(p) for p in bibles.values()):
        return None
    n = max(bibles, default=0) + 1
    if n > 1 and novel_store.state(bibles[n - 1]) != S.NS_CANON:
        return None
    if ctx.today >= season_start(n, ctx.shifts) - timedelta(days=S.NOVEL_BIBLE_LEAD_DAYS):
        return "bible", n, 0, None
    return None


def generate(ctx: Ctx) -> None:
    job = next_job(ctx)
    if job is None:
        if any(notion.plain(p, S.N_RESULT).startswith("api:")
               and notion.plain(p, S.N_RESULT).endswith(":hold")
               and novel_store.state(p) == S.NS_CHECK_FAILED for p in ctx.rows):
            ctx.log.fail("api:held_for_diagnosis")
        logger.info("[Novel] 생성할 원고 없음")
        return
    what, n, m, row = job
    if novel_store.month_spent(ctx.ctrl, ctx.now) + JOB_ESTIMATE_USD[what] > S.NOVEL_BUDGET_USD:
        _budget_alert(ctx, "budget:month")
        return
    label = novel_store.bible_key(n) if what == "bible" else novel_store.episode_key(n, m)
    logger.info(f"[Novel] 생성 {label}")
    try:
        if what == "bible":
            generate_bible(ctx, n, row)
        else:
            generate_episode(ctx, n, m, row)
    except BudgetStop as e:
        _budget_alert(ctx, str(e))
    except novel_llm.LLMError as e:
        target = row or novel_store.find(ctx.rows, label)
        if target is not None and novel_store.state(target) == S.NS_GENERATING:
            note = ""
            if e.diagnostic:
                note = (f"원인={e.diagnostic['reason_hint']} · 요청={e.diagnostic['request_id']}"
                        + (" · 자동 재시도 보류, 진단 확인 필요" if str(e).endswith(":hold") else ""))
            _fail_row(ctx, target, S.NS_CHECK_FAILED, str(e), note=note)
            ctx.log.fail(str(e))
            if (novel_store.kind(target) == S.NK_EPISODE and not str(e).endswith(":hold")
                    and (notion.number(target, S.N_ATTEMPTS) or 0) >= S.NOVEL_MAX_ATTEMPTS):
                _final_fail(ctx, target, "attempts")  # DN2026_0119 A2(a)
            if e.diagnostic:
                evidence = {**e.diagnostic, "message_redacted": e.private_detail,
                            "recorded_at": ctx.now.isoformat(), "stage": label,
                            "attempt": notion.number(target, S.N_ATTEMPTS)}
                try:
                    novel_store.append(target, novel_store.section_blocks([
                        (novel_diagnostics.H_API_ERROR, json.dumps(evidence, ensure_ascii=False), "json")]))
                except Exception:  # noqa: BLE001 — evidence failure must not undo the hold or leak content
                    logger.error("[Novel] private API diagnostic persistence failed")
                    ctx.log.fail("diagnostic:persist_failed")
        else:
            ctx.log.fail(str(e))


def _budget_alert(ctx: Ctx, code: str) -> None:
    ctx.log.degrade(code)
    day = ctx.today.isoformat()
    if ctx.ctrl.get("budget_alert_day") != day:
        ctx.ctrl["budget_alert_day"] = day
        novel_tg.send("T5", f"⚠️ 예산 상한 — {code} (이번 달 ${novel_store.month_spent(ctx.ctrl, ctx.now):.2f} / "
                            f"${S.NOVEL_BUDGET_USD:.0f})")


# ── 5 전날 안내 (정보성) ───────────────────────────────
def auto_publishable(page: dict) -> bool:
    """게시 대상 회차 (DN2026_0119): 자동 게이트를 통과한 '승인' + 품질 경고 알림 영수증(있을 때).
    이관 전 v1 잔존 '검토대기' + 검수 PASS + 요청 모델 그대로도 같은 뜻으로 본다(게시 측 호환, DN2026_0072 Q1)."""
    if novel_store.state(page) == S.NS_APPROVED:
        return novel_policy.release_ok(page)
    return (novel_store.state(page) == S.NS_REVIEW and notion.select(page, S.N_VERDICT) == "PASS"
            and notion.plain(page, S.N_MODEL) == S.MODEL_WRITER
            and novel_policy.release_ok(page))


def remind(ctx: Ctx) -> None:
    """DN2026_0119 AC1: 사람 조치 요구 없음 — 내일 게시 예정·자동 검수 진행 상황만 알린다."""
    if ctx.kst.hour != S.NOVEL_REMIND_HOUR:
        return
    tomorrow = ctx.today + timedelta(days=1)
    due = [p for p in ctx.rows if novel_store.kind(p) == S.NK_EPISODE and notion.kst_date(p, S.N_DUE) == tomorrow
           and not novel_store.stopped(p) and novel_store.state(p) not in (S.NS_PUBLISHED, S.NS_SCHEDULED)]
    auto = [novel_store.key(p) for p in due if auto_publishable(p)]
    pending = [novel_store.key(p) for p in due if novel_store.key(p) not in auto]
    when = novel_calendar.label(tomorrow)
    if auto:
        novel_tg.send("T4", f"🗓 내일 {when} 게시 예정: {', '.join(auto)} — 자동 예약 게시됩니다(조치 불필요).")
    if pending:
        novel_tg.send("T4", f"ℹ️ 내일 {when} 게시 예정 회차가 아직 자동 검수 중: {', '.join(pending)} — "
                            f"게시 시각까지 통과하지 못하면 자동 순연됩니다(조치 불필요).")


# ── 진입점 ────────────────────────────────────────────────
def body(log: RunLog, now: datetime | None = None, client: Any = None) -> None:
    if not S.NOVEL_ENABLED:
        logger.info("[Novel] PAGE76_NOVEL_ENABLED=false — 종료")
        return
    if not S.DB12_ID or S.NOVEL_START is None:
        log.fail("novel:not_configured")
        return
    now = now or datetime.now(UTC)
    rows = [p for p in novel_store.all_rows()
            if novel_store.kind(p) in (S.NK_BIBLE, S.NK_EPISODE, S.NK_NOTICE, S.NK_CONTROL)]
    ctrl_page, ctrl = novel_store.control(rows)
    ctx = Ctx(log=log, now=now, rows=rows, ctrl_page=ctrl_page, ctrl=ctrl, client=client,
              sched_now=sched.effective_now(now))
    logger.info(f"[Novel] v{VERSION} 행 {len(rows)}건 · mode={S.MODE} · 이번 달 "
                f"${novel_store.month_spent(ctrl, now):.2f} · 순연 {len(ctx.shifts)}일")
    try:
        collect_stops(ctx)
        _guarded(ctx, migrate)     # DN2026_0119 §5-3 이관 · RC-1
        check_resume(ctx)          # C2(d) 일시 정지 해제
        recover_exhausted(ctx)     # A2(a)
        catch_missed(ctx)          # A2(b)
        sync_due(ctx)
        for page in ctx.rows:
            if (novel_store.kind(page) == S.NK_EPISODE and novel_store.state(page) in (S.NS_REVIEW, S.NS_APPROVED)
                    and not novel_store.stopped(page)):
                novel_policy.notify_quality(page, ctx.log)
        generate(ctx)
        remind(ctx)
        ctx.ctrl["last_run"] = now.isoformat()
    except novel_prompts.PromptError as e:
        log.fail(str(e))
        novel_tg.send("T5", f"⚠️ 프롬프트 정본을 읽지 못함 — {e}")
    finally:
        novel_store.save_control(ctrl_page, ctx.ctrl)
        logger.info(f"[Novel] 이번 실행 비용 ${ctx.run_cost:.4f}")
    if safety.tripped():
        log.degrade("graph:tripped")


def main() -> int:
    cli.setup_logging()
    return cli.execute("novel", VERSION, body)


if __name__ == "__main__":
    sys.exit(main())


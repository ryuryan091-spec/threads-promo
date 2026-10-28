"""
p76/run_novel.py
================
사건 소설(B형) 원고 자동 작성 (DSN-P76-NOVEL-001 v0.2 · 002 v0.1 · DN2026_0072). 한 실행 = 아래 순서 1회.
게시는 별도 파이프라인 p76.run_novel_publish 가 맡는다(이 모듈은 게시하지 않는다).

  0 가드      PAGE76_ENABLED · PAGE76_NOVEL_ENABLED (전역 중단)
  1 중단 수집  DB-12 중단 체크 → 상태 '중단' (텔레그램 명령은 읽지 않는다 — DN2026_0072)
  2 묵시 등록  바이블 '묵시승인대기' + 등록예정시각 경과 + 중단 없음 → '정본' (D-N7, 바이블만 D-N10)
              직전 실행이 6시간보다 오래됐으면 2시간 연기
  3 생성      한 실행에 1건: 바이블 재시도 → 회차(정본 바이블, 게시일 D-14) → 다음 시즌 바이블(제1화 D-20)
              작성 Opus 5.5 → 규칙 검사(재작성 1회) → 검수 Sonnet 5.5(6·7화·경계값은 Opus 재검수)
  4 알림      묵시 승인 알림은 KST 09~18시에만, 보낸 시각 + 2시간이 등록예정시각
  6 전날 안내  20시대 실행에서 내일 게시 회차 — 자동 발행 예정 / 회장 '승인'이 있어야 발행되는 회차

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
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from p76 import (
    cli,
    notion,
    novel_llm,
    novel_prompts,
    novel_rules,
    novel_store,
    novel_tg,
    safety,
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

VERSION = "1.1.0"

logger = logging.getLogger(__name__)
S = settings
SEVERITY = {"PASS": 0, "HUMAN_REQUIRED": 1, "BLOCK": 2}
VERDICTS = ("PASS", "HUMAN_REQUIRED", "BLOCK")
DONE_EPISODE_STATES = (S.NS_REVIEW, S.NS_APPROVED, S.NS_SCHEDULED, S.NS_PUBLISHED, S.NS_CHECK)
JOB_ESTIMATE_USD = {"bible": 2.0, "episode": 1.5}


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

    @property
    def kst(self) -> datetime:
        return self.now.astimezone(notion.KST)

    @property
    def today(self) -> date:
        return self.kst.date()


# ── 일정 ──────────────────────────────────────────────────
def season_start(n: int) -> date:
    return S.NOVEL_START + timedelta(weeks=S.NOVEL_EPISODES * (n - 1))


def due_date(n: int, m: int) -> date:
    return season_start(n) + timedelta(weeks=m - 1)


def episode_due_for_generation(n: int, m: int, today: date) -> bool:
    if today >= due_date(n, m) - timedelta(days=S.NOVEL_EPISODE_LEAD_DAYS):
        return True
    return m <= S.NOVEL_BUFFER_EPISODES and today >= season_start(n) - timedelta(days=S.NOVEL_EPISODE_LEAD_DAYS)


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(notion.KST).strftime("%m-%d %H:%M")


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
        if novel_store.kind(page) not in (S.NK_BIBLE, S.NK_EPISODE):
            continue
        if S.NS_STOPPED not in novel_store.ALLOWED.get(novel_store.state(page), set()):
            continue
        if notion.checkbox(page, S.N_STOP):
            _stop(ctx, page, "notion")
            stopped_keys.append(novel_store.key(page))
    if stopped_keys:
        novel_tg.send("T1d", f"⏸ 중단 접수 — {', '.join(stopped_keys)}\n"
                             f"재개는 노션 DB-12 에서 상태를 되돌리고 중단 체크를 풀어야 합니다.")


# ── 2 묵시 등록 ───────────────────────────────────────────
def _stale(ctx: Ctx) -> bool:
    raw = ctx.ctrl.get("last_run")
    try:
        last = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        last = None
    return last is None or ctx.now - last > timedelta(hours=S.NOVEL_STALE_HOURS)


def register_implicit(ctx: Ctx) -> None:
    for page in ctx.rows:
        if novel_store.kind(page) != S.NK_BIBLE or novel_store.state(page) != S.NS_IMPLICIT:
            continue
        notified = notion.date_value(page, S.N_NOTIFIED_AT)
        due = notion.date_value(page, S.N_REGISTER_AT)
        if notified is None or due is None or due > ctx.now:
            continue
        label = novel_store.key(page)
        if _stale(ctx):
            later = ctx.now + timedelta(hours=S.NOVEL_IMPLICIT_HOURS)
            novel_store.update(page, {S.N_REGISTER_AT: notion.v_date(later)})
            ctx.log.writes += 1
            ctx.log.degrade("implicit:deferred")
            novel_tg.send("T1e", f"⏳ {label} 정본 등록 연기 — 직전 점검이 오래됨. "
                                 f"{_hhmm(later)} 이후 첫 점검에서 다시 확인합니다.")
            continue
        fresh = novel_store.refresh(page)
        page["properties"] = fresh.get("properties", page.get("properties", {}))
        if novel_store.stopped(page) or novel_store.state(page) != S.NS_IMPLICIT:
            continue
        novel_store.transition(page, S.NS_CANON, {S.N_RESULT: notion.v_rich("implicit:registered")})
        ctx.log.writes += 1
        n = novel_store.season(page)
        logger.info(f"[Novel] {label} 정본 등록(묵시)")
        novel_tg.send("T1c", f"✅ 사건 소설 시즌 {n} 바이블 정본 등록 — 제1화 게시 예정 {season_start(n):%m-%d}, "
                             f"원고 생성 {season_start(n) - timedelta(days=S.NOVEL_EPISODE_LEAD_DAYS):%m-%d}부터")


# ── 4 알림 ────────────────────────────────────────────────
def _bible_summary(page: dict) -> str:
    bible = novel_store.json_section(page, H_BIBLE)
    review = novel_store.json_section(page, H_REVIEW)
    src = bible.get("source") or {}
    extra = ""
    if src.get("pm_ids"):
        extra += " · " + ",".join(src["pm_ids"])
    if src.get("promoted_journals"):
        extra += " · 승격 " + ",".join(f"#{n:03d}" for n in src["promoted_journals"])
    return (f"제목: {bible.get('season_title', '-')}\n로그라인: {bible.get('logline', '-')}\n"
            f"사건유형: {','.join(bible.get('case_types') or []) or '-'} / 소재: {src.get('kind', '-')}{extra}\n"
            f"검수: {review.get('final_verdict', notion.select(page, S.N_VERDICT) or '-')}")


def notify_pending(ctx: Ctx) -> None:
    lo, hi = S.NOVEL_NOTIFY_HOURS
    if not lo <= ctx.kst.hour < hi:
        return
    for page in ctx.rows:
        if novel_store.kind(page) != S.NK_BIBLE or novel_store.state(page) != S.NS_IMPLICIT:
            continue
        if notion.date_value(page, S.N_NOTIFIED_AT) is not None:
            continue
        register_at = ctx.now + timedelta(hours=S.NOVEL_IMPLICIT_HOURS)
        n = novel_store.season(page)
        text = (f"📖 사건 소설 시즌 {n} 바이블 — 묵시 승인 대기\n{_bible_summary(page)}\n"
                f"{_hhmm(register_at)} 이후 첫 점검에서 정본으로 등록됩니다.\n"
                f"멈추려면 노션 DB-12 이 행의 '중단'을 체크해 주세요.")
        if not novel_tg.send("T1", text, novel_tg.page_link(page["id"])):
            ctx.log.degrade("tg:notify_failed")
            continue  # 알림이 안 갔으면 타이머를 시작하지 않는다
        novel_store.update(page, {S.N_NOTIFIED_AT: notion.v_date(ctx.now),
                                  S.N_REGISTER_AT: notion.v_date(register_at)})
        ctx.log.writes += 1


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
    novel_store.add_month_spent(ctx.ctrl, ctx.now, res.cost)
    novel_store.add_cost(row, res.cost)
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


def review(ctx: Ctx, row: dict, target: str, text: str, escalate: bool) -> tuple[str, dict, bool]:
    """(최종 판정, 검수 JSON, fallback 여부). escalate=True 면 처음부터 Opus 로도 한 번 더 본다."""
    p = _prompts(ctx)
    user = [{"text": f"검수 대상: {target}\n\n[원고]\n{text}"}]
    results: list[novel_llm.Result] = []
    res = _llm(ctx, row, label=f"{novel_store.key(row)}:review", model=S.MODEL_REVIEWER, effort="medium",
               system=p.systems["nr"], user_blocks=user, schema=p.schemas["nr"], max_tokens=8000,
               expected_out=1500, stream=False)
    results.append(res)
    verdict = "BLOCK"
    if res.ok:
        verdict = _worse(str(res.data.get("verdict", "BLOCK")), computed_verdict(res.data))
    if res.ok and (escalate or verdict == "HUMAN_REQUIRED"):
        res2 = _llm(ctx, row, label=f"{novel_store.key(row)}:review2", model=S.MODEL_WRITER, effort="medium",
                    system=p.systems["nr"], user_blocks=user, schema=p.schemas["nr"], max_tokens=8000,
                    expected_out=1500, stream=False)
        results.append(res2)
        v2 = _worse(str(res2.data.get("verdict", "BLOCK")), computed_verdict(res2.data)) if res2.ok else "BLOCK"
        verdict = _worse(verdict, v2)
        if res2.ok and SEVERITY[v2] >= SEVERITY.get(str(res.data.get("verdict")), 0):
            res = res2
    fallback = any(r.fallback for r in results)
    if fallback:
        verdict = _worse(verdict, "HUMAN_REQUIRED")
    data = dict(res.data or {"error": res.refusal or res.stop_reason})
    data["final_verdict"] = verdict
    data["models"] = [r.model for r in results]
    return verdict, data, fallback


def _fail_row(ctx: Ctx, row: dict, new_state: str, code: str) -> None:
    novel_store.transition(row, new_state, {S.N_RESULT: notion.v_rich(code[:1900])})
    ctx.log.writes += 1
    ctx.log.degrade(f"{novel_store.key(row)}:{code}")
    novel_tg.send("T5", f"⚠️ {novel_store.key(row)} {new_state} — {code}", novel_tg.page_link(row["id"]))


def _start_attempt(ctx: Ctx, row: dict, tag: str) -> None:
    if novel_store.state(row) == S.NS_CHECK_FAILED:
        novel_store.transition(row, S.NS_GENERATING)
    attempts = (notion.number(row, S.N_ATTEMPTS) or 0) + 1
    novel_store.update(row, {S.N_ATTEMPTS: notion.v_number(attempts), S.N_PROMPT_VER: notion.v_rich(tag)})
    ctx.log.writes += 1


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


def generate_bible(ctx: Ctx, n: int, row: dict | None) -> None:
    p = _prompts(ctx)
    if row is None:
        row = novel_store.create(ctx.rows, novel_store.bible_key(n), S.NK_BIBLE, n=n)
        ctx.log.writes += 1
    _start_attempt(ctx, row, p.tag("nb"))
    journals = novel_prompts.published_journals()
    system = p.systems["nb"] + "\n\n[문체 견본]\n" + (_style(p) or "없음")
    user = _bible_user(ctx, n, journals)
    note = ""
    for attempt in range(2):
        res = _llm(ctx, row, label=f"{novel_store.key(row)}:write", model=S.MODEL_WRITER, effort="high",
                   system=system, user_blocks=[{"text": user + note}], schema=p.schemas["nb"], max_tokens=32000,
                   expected_out=12000, stream=True)
        if res.refusal:
            _fail_row(ctx, row, S.NS_REFUSED, f"refusal:{res.refusal}")
            return
        if not res.ok:
            _fail_row(ctx, row, S.NS_CHECK_FAILED, f"llm:{res.stop_reason}")
            return
        bible = res.data
        errors = novel_rules.check_bible(bible, banned_terms=S.BANNED_TERMS, journal_numbers=set(journals))
        if any(e.startswith("R03") for e in errors):
            _fail_row(ctx, row, S.NS_HELD, "rules:R03")
            return
        if not errors:
            break
        note = f"\n\n직전 바이블이 다음 규칙에 걸렸다: {', '.join(errors)}. 해당 부분을 고쳐 다시 만들어 주세요."
        if attempt == 1:
            _fail_row(ctx, row, S.NS_CHECK_FAILED, "rules:" + ",".join(errors))
            return
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
    if verdict == "BLOCK":
        _fail_row(ctx, row, S.NS_HELD, "review:BLOCK")
        return
    if verdict == "HUMAN_REQUIRED":
        novel_store.transition(row, S.NS_EXPLICIT, {S.N_RESULT: notion.v_rich("review:HUMAN_REQUIRED")})
        ctx.log.writes += 1
        novel_tg.send("T1b", f"📖 사건 소설 시즌 {n} 바이블 — 검수 플래그로 자동 등록하지 않습니다.\n"
                             f"{_bible_summary(row)}\n노션에서 상태를 '정본'으로 바꿔야 진행됩니다.",
                      novel_tg.page_link(row["id"]))
        return
    novel_store.transition(row, S.NS_IMPLICIT, {S.N_RESULT: notion.v_rich("review:PASS")})
    ctx.log.writes += 1
    logger.info(f"[Novel] {novel_store.key(row)} 묵시승인대기")


# ── 3 생성: 회차 ──────────────────────────────────────────
def _episode_inputs(ctx: Ctx, n: int, m: int) -> tuple[dict, dict, list[dict]]:
    bible_page = novel_store.find(ctx.rows, novel_store.bible_key(n))
    bible = novel_store.json_section(bible_page, H_BIBLE) if bible_page else {}
    arc = next((a for a in bible.get("arc") or [] if int(a.get("episode", 0)) == m), {})
    prevs = [novel_store.find(ctx.rows, novel_store.episode_key(n, k)) for k in (m - 1, m - 2) if k >= 1]
    return bible, arc, [p for p in prevs if p is not None]


def generate_episode(ctx: Ctx, n: int, m: int, row: dict | None) -> None:
    p = _prompts(ctx)
    bible, arc, prevs = _episode_inputs(ctx, n, m)
    if not bible or not arc:
        ctx.log.fail(f"N{n}-E{m:02d}:bible_unreadable")
        return
    if row is None:
        row = novel_store.create(ctx.rows, novel_store.episode_key(n, m), S.NK_EPISODE, n=n, m=m,
                                 due=due_date(n, m))
        ctx.log.writes += 1
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
    note = ""
    for attempt in range(2):
        res = _llm(ctx, row, label=f"{novel_store.key(row)}:write", model=S.MODEL_WRITER, effort="medium",
                   system=system, user_blocks=head + [{"text": rest + note}], schema=p.schemas["ne"],
                   max_tokens=32000, expected_out=9000, stream=True)
        if res.refusal:
            _fail_row(ctx, row, S.NS_REFUSED, f"refusal:{res.refusal}")
            return
        if not res.ok:
            _fail_row(ctx, row, S.NS_CHECK_FAILED, f"llm:{res.stop_reason}")
            return
        ep = res.data
        errors = novel_rules.check_episode(
            ep.get("body", ""), m, banned_terms=S.BANNED_TERMS, cliches=p.cliches, prev_bodies=prev_bodies,
            journals=journals, d_plus_end=(ep.get("continuity") or {}).get("d_plus_end"), prev_d_plus_end=prev_d,
        ) + novel_rules.question_ok(ep.get("question", ""))
        if any(e.startswith("R03") for e in errors):
            _fail_row(ctx, row, S.NS_HELD, "rules:R03")
            return
        if not errors:
            break
        if attempt == 1:
            _fail_row(ctx, row, S.NS_CHECK_FAILED, "rules:" + ",".join(errors))
            return
        note = f"\n\n직전 원고가 다음 규칙에 걸렸다: {', '.join(errors)}. 해당 부분을 고쳐 다시 써 주세요."
    writer_fallback = res.fallback
    final = m >= S.NOVEL_EPISODES
    verdict, review_json, _ = review(ctx, row, f"제{m}화 (최종화 여부: {'예' if final else '아니오'})",
                                     ep["body"], escalate=m >= S.NOVEL_EPISODES - 1)
    if writer_fallback:
        verdict = _worse(verdict, "HUMAN_REQUIRED")
        review_json["final_verdict"] = verdict
    body = ep["body"]
    meta = {k: ep.get(k) for k in ("continuity", "summary", "quoted_journals", "placeholders_used", "self_check")}
    meta["review"] = review_json
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
    if verdict == "BLOCK":
        _fail_row(ctx, row, S.NS_HELD, "review:BLOCK")
        return
    novel_store.transition(row, S.NS_REVIEW, {S.N_RESULT: notion.v_rich(f"review:{verdict}")})
    ctx.log.writes += 1
    logger.info(f"[Novel] {novel_store.key(row)} 검토대기 ({verdict}, {novel_rules.body_chars(body)}자)")
    novel_tg.send("T2", f"📝 시즌 {n} 제{m}화 검토대기 — 게시 {due_date(n, m):%m-%d}(일) / "
                        f"{novel_rules.body_chars(body)}자 / 검수 {verdict}", novel_tg.page_link(row["id"]))


# ── 3 생성: 다음 작업 고르기 ──────────────────────────────
def _retryable(page: dict) -> bool:
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
            row = novel_store.find(ctx.rows, novel_store.episode_key(n, m))
            if row is None:
                if episode_due_for_generation(n, m, ctx.today):
                    return "episode", n, m, None
                break
            if _retryable(row) and episode_due_for_generation(n, m, ctx.today):
                return "episode", n, m, row
            if novel_store.state(row) not in DONE_EPISODE_STATES:
                break
    if any(novel_store.stopped(p) for p in bibles.values()):
        return None
    n = max(bibles, default=0) + 1
    if n > 1 and novel_store.state(bibles[n - 1]) != S.NS_CANON:
        return None
    if ctx.today >= season_start(n) - timedelta(days=S.NOVEL_BIBLE_LEAD_DAYS):
        return "bible", n, 0, None
    return None


def generate(ctx: Ctx) -> None:
    job = next_job(ctx)
    if job is None:
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
            _fail_row(ctx, target, S.NS_CHECK_FAILED, str(e))
        else:
            ctx.log.fail(str(e))


def _budget_alert(ctx: Ctx, code: str) -> None:
    ctx.log.degrade(code)
    day = ctx.today.isoformat()
    if ctx.ctrl.get("budget_alert_day") != day:
        ctx.ctrl["budget_alert_day"] = day
        novel_tg.send("T5", f"⚠️ 예산 상한 — {code} (이번 달 ${novel_store.month_spent(ctx.ctrl, ctx.now):.2f} / "
                            f"${S.NOVEL_BUDGET_USD:.0f})")


# ── 6 전날 안내 ───────────────────────────────────────────
def auto_publishable(page: dict) -> bool:
    """회장 '승인' 없이 예고 후 자동 발행되는 회차 (DN2026_0072 Q1): 검토대기 + 검수 PASS + 요청 모델 그대로."""
    return (novel_store.state(page) == S.NS_REVIEW and notion.select(page, S.N_VERDICT) == "PASS"
            and notion.plain(page, S.N_MODEL) == S.MODEL_WRITER)


def remind(ctx: Ctx) -> None:
    if ctx.kst.hour != S.NOVEL_REMIND_HOUR:
        return
    tomorrow = ctx.today + timedelta(days=1)
    due = [p for p in ctx.rows if novel_store.kind(p) == S.NK_EPISODE and notion.kst_date(p, S.N_DUE) == tomorrow
           and not novel_store.stopped(p) and novel_store.state(p) not in (S.NS_PUBLISHED, S.NS_SCHEDULED)]
    auto = [novel_store.key(p) for p in due if novel_store.state(p) == S.NS_APPROVED or auto_publishable(p)]
    pending = [novel_store.key(p) for p in due if novel_store.key(p) not in auto]
    if auto:
        novel_tg.send("T4", f"🗓 내일 게시 예정: {', '.join(auto)} — 08시대 발행 예고 후 2시간 뒤 자동 발행됩니다. "
                            f"멈추려면 노션 DB-12 중단 체크.")
    if pending:
        novel_tg.send("T4", f"⏰ 내일 게시 예정인데 자동 발행 대상이 아님(검수 플래그·미완성): {', '.join(pending)} — "
                            f"노션에서 '승인'해야 발행됩니다.")


# ── 진입점 ────────────────────────────────────────────────
def body(log: RunLog, now: datetime | None = None, client: Any = None) -> None:
    if not S.NOVEL_ENABLED:
        logger.info("[Novel] PAGE76_NOVEL_ENABLED=false — 종료")
        return
    if not S.DB12_ID or S.NOVEL_START is None:
        log.fail("novel:not_configured")
        return
    now = now or datetime.now(UTC)
    rows = [p for p in novel_store.all_rows() if novel_store.kind(p) in (S.NK_BIBLE, S.NK_EPISODE, S.NK_CONTROL)]
    ctrl_page, ctrl = novel_store.control(rows)
    ctx = Ctx(log=log, now=now, rows=rows, ctrl_page=ctrl_page, ctrl=ctrl, client=client)
    logger.info(f"[Novel] 행 {len(rows)}건 · mode={S.MODE} · 이번 달 ${novel_store.month_spent(ctrl, now):.2f}")
    try:
        collect_stops(ctx)
        register_implicit(ctx)
        generate(ctx)
        notify_pending(ctx)
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

"""
p76/run_novel_publish.py
========================
사건 소설(B형) 회차 게시 — 원고 생성(p76.run_novel)과 나눈 별도 파이프라인 (DN2026_0072).
게시일(일요일)에만 돈다. 한 실행 = 아래 순서 1회.

  0 가드      PAGE76_ENABLED · PAGE76_NOVEL_ENABLED · PAGE76_NOVEL_PUBLISH_ENABLED
  1 중단 수집  DB-12 중단 체크 → 상태 '중단'
  2 발행 예고  오늘 게시 회차 1건(검토대기+PASS+요청 모델 그대로, 또는 회장 '승인') · KST 08시 이후
              → 텔레그램 "HH:MM 이후 발행됩니다" → 보냈을 때만 '발행예고' + 발행 예정 시각(예고 + 2시간, 10시 이전 불가)
              → 알림이 안 갔으면 타이머를 시작하지 않는다(게시 없음)
  3 게시      '발행예고' + 발행 예정 시각 경과 → 행을 다시 읽어 중단·상태 확인 → 규칙·법률 블록 재검사 → 카드 사진 게시
              하루 1건 · 실패는 다음 실행에서 재시도 · 검사 실패는 '보류'

  중단: ① DB-12 행 '중단' 체크(게시 직전에 다시 읽는다) ② Variable PAGE76_NOVEL_PUBLISH_ENABLED=false
        ③ Actions 에서 p76-novel-publish 워크플로 비활성화

  모드     | DB-12 기록 | 텔레그램 | 게시
  dry_run | 없음        | 없음     | 없음 (검사·카드만)
  shadow  | 함          | 함       | 없음 (검사·카드만, 예고 문구에 shadow 표시)
  live    | 함          | 함       | 함

CONTROL 행(월 비용·마지막 생성 실행)은 읽지도 쓰지도 않는다 — 생성 파이프라인의 기록이다.
원고·프롬프트·노션 ID 는 로그에 남기지 않는다 (공개 레포).
사용: python -m p76.run_novel_publish
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from p76 import (
    card_render,
    cli,
    graph,
    notion,
    novel_prompts,
    novel_rules,
    novel_store,
    novel_tg,
    safety,
    settings,
)
from p76.novel_store import H_BODY, H_META, H_QUESTION, H_QUOTE, H_SUBTITLE
from p76.run_novel import Ctx, _prompts, auto_publishable, collect_stops
from p76.runlog import RunLog

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
S = settings


def _bible_ok(ctx: Ctx, page: dict) -> bool:
    bible = novel_store.find(ctx.rows, novel_store.bible_key(novel_store.season(page)))
    return bible is not None and not novel_store.stopped(bible)


def _today_episodes(ctx: Ctx) -> list[dict]:
    out = [p for p in ctx.rows
           if novel_store.kind(p) == S.NK_EPISODE and notion.kst_date(p, S.N_DUE) == ctx.today
           and not notion.checkbox(p, S.N_STOP) and not notion.plain(p, S.N_FB_ID) and _bible_ok(ctx, p)]
    out.sort(key=lambda p: (novel_store.season(p), novel_store.episode(p)))
    return out


def _published_today(ctx: Ctx) -> bool:
    return any(novel_store.state(p) == S.NS_PUBLISHED and notion.kst_date(p, S.N_PUBLISHED_AT) == ctx.today
               for p in ctx.rows)


def _hm(dt: datetime) -> str:
    return dt.astimezone(notion.KST).strftime("%H:%M")


def publish_at(ctx: Ctx) -> datetime:
    """예고 시각 + 2시간. 게시일 10시 이전이면 10시로 미룬다."""
    earliest = datetime.combine(ctx.today, time(S.NOVEL_PUBLISH_HOUR), tzinfo=notion.KST)
    return max(ctx.now + timedelta(hours=S.NOVEL_PUBLISH_DELAY_HOURS), earliest)


# ── 2 발행 예고 ───────────────────────────────────────────
def notice(ctx: Ctx) -> None:
    if ctx.kst.hour < S.NOVEL_PUBLISH_NOTICE_HOUR or _published_today(ctx):
        return
    episodes = _today_episodes(ctx)
    if any(novel_store.state(p) == S.NS_SCHEDULED for p in episodes):
        return  # 이미 예고함 — 하루 1건
    row = next((p for p in episodes if novel_store.state(p) == S.NS_APPROVED or auto_publishable(p)), None)
    if row is None:
        return
    label, n, m = novel_store.key(row), novel_store.season(row), novel_store.episode(row)
    at = publish_at(ctx)
    if settings.MODE == "dry_run":
        logger.info(f"[NovelPub][dry_run] {label} 발행 예고 생략 — 예정 {_hm(at)}")
        return
    shadow = " (shadow — 실제 게시 없음)" if settings.MODE != "live" else ""
    text = (f"📢 사건 소설 시즌 {n} 제{m}화 — {_hm(at)} 이후 발행됩니다{shadow}.\n"
            f"멈추려면 노션 DB-12 이 행의 '중단'을 체크해 주세요"
            f"(또는 GitHub Variable PAGE76_NOVEL_PUBLISH_ENABLED=false).")
    if not novel_tg.send("T6", text, novel_tg.page_link(row["id"])):
        ctx.log.degrade("tg:notice_failed")
        return  # 알림이 안 갔으면 타이머를 시작하지 않는다
    novel_store.transition(row, S.NS_SCHEDULED, {S.N_NOTIFIED_AT: notion.v_date(ctx.now),
                                                 S.N_REGISTER_AT: notion.v_date(at),
                                                 S.N_RESULT: notion.v_rich("notice:sent")})
    ctx.log.writes += 1
    logger.info(f"[NovelPub] {label} 발행 예고 — {_hm(at)} 이후 게시")


# ── 3 게시 ────────────────────────────────────────────────
def _hold(ctx: Ctx, row: dict, code: str) -> None:
    novel_store.transition(row, S.NS_HELD, {S.N_RESULT: notion.v_rich(code[:1900])})
    ctx.log.writes += 1
    ctx.log.fail(f"{novel_store.key(row)}:publish_check")
    novel_tg.send("T5", f"⚠️ {novel_store.key(row)} 게시 전 검사 실패 — {code}. 보류로 옮겼습니다. "
                        f"고친 뒤 '승인'으로 바꾸면 게시예정일 당일 다음 실행에서 예고부터 다시 진행합니다"
                        f"(오늘 남은 실행이 없으면 게시예정일을 바꿔 주세요).", novel_tg.page_link(row["id"]))


def publish(ctx: Ctx) -> None:
    if _published_today(ctx):
        return
    row = next((p for p in _today_episodes(ctx) if novel_store.state(p) == S.NS_SCHEDULED), None)
    if row is None:
        return
    due = notion.date_value(row, S.N_REGISTER_AT)
    if due is None or due > ctx.now:
        return
    label, n, m = novel_store.key(row), novel_store.season(row), novel_store.episode(row)
    row["properties"] = novel_store.refresh(row).get("properties", row.get("properties", {}))
    if novel_store.state(row) != S.NS_SCHEDULED or notion.checkbox(row, S.N_STOP):
        logger.info(f"[NovelPub] {label} 게시 직전 확인 — 중단·상태 변경으로 게시하지 않음")
        if notion.checkbox(row, S.N_STOP):
            collect_stops(ctx)
        return
    p = _prompts(ctx)
    secs = novel_store.sections(row)
    body, question = secs.get(H_BODY, ""), secs.get(H_QUESTION, "")
    quoted = [int(x) for x in novel_store.json_section(row, H_META).get("quoted_journals") or []]
    journals = novel_prompts.journal_text(quoted) if quoted else {}
    errors = [e for e in novel_rules.check_episode(body, m, banned_terms=S.BANNED_TERMS, journals=journals)
              if not e.startswith(("R01:length", "R09"))]  # 회장 편집으로 분량이 바뀌는 것은 허용
    errors += novel_rules.question_ok(question)
    filled, law_errors = novel_rules.fill_laws(body, p.laws, ctx.today)
    errors += law_errors
    if errors:
        _hold(ctx, row, "publish_check:" + ",".join(errors))
        return
    caption = f"{filled.strip()}\n\n{novel_rules.footer(n, m, question)}"
    work = Path(tempfile.mkdtemp(prefix="p76n_"))
    try:
        subtitle = secs.get(H_SUBTITLE, "").strip()
        headline = secs.get(H_QUOTE, "").strip() or subtitle
        try:
            image = card_render.render(f"사건 소설 시즌 {n} · 제{m}화", headline, subtitle, str(work))
        except card_render.RenderError as e:
            ctx.log.fail(str(e))
            novel_tg.send("T5", f"⚠️ {label} 카드 렌더 실패 — {e}. 다음 실행에서 다시 시도합니다.")
            return
        if settings.MODE != "live":
            logger.info(f"[NovelPub][{settings.MODE}] {label} 검사·카드 통과 — 게시 생략")
            return
        res = graph.post_photo(image, caption)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    status, reason = res["status"], res.get("reason", "")
    if status == "ok":
        novel_store.transition(row, S.NS_PUBLISHED, {S.N_FB_ID: notion.v_rich(res["object_id"]),
                                                     S.N_PUBLISHED_AT: notion.v_date(ctx.now),
                                                     S.N_RESULT: notion.v_rich("published")})
        ctx.log.writes += 2
        novel_tg.send("T3", f"📤 시즌 {n} 제{m}화 게시 — 24시간 동안 댓글을 직접 확인해 주세요(실명·회사명 숨김).")
        return
    if status == "unknown":
        novel_store.transition(row, S.NS_CHECK, {S.N_RESULT: notion.v_rich(f"unknown:{reason}")})
        ctx.log.fail(f"unknown:{reason}")
        novel_tg.send("T5", f"⚠️ {label} 게시 응답 미확정({reason}) — 페이지에서 게시 여부 확인")
        return
    ctx.log.fail(f"failed:{reason}")
    novel_store.update(row, {S.N_RESULT: notion.v_rich(f"failed:{reason}")})
    novel_tg.send("T5", f"⚠️ {label} 게시 실패 — {reason}. 다음 실행에서 다시 시도합니다.")


# ── 진입점 ────────────────────────────────────────────────
def body(log: RunLog, now: datetime | None = None) -> None:
    if not S.NOVEL_ENABLED or not S.NOVEL_PUBLISH_ENABLED:
        logger.info("[NovelPub] PAGE76_NOVEL_ENABLED / PAGE76_NOVEL_PUBLISH_ENABLED 꺼짐 — 종료")
        return
    if not S.DB12_ID or S.NOVEL_START is None:
        log.fail("novel:not_configured")
        return
    now = now or datetime.now(UTC)
    rows = [p for p in novel_store.all_rows() if novel_store.kind(p) in (S.NK_BIBLE, S.NK_EPISODE)]
    ctx = Ctx(log=log, now=now, rows=rows, ctrl_page={}, ctrl={})
    logger.info(f"[NovelPub] 행 {len(rows)}건 · mode={S.MODE}")
    try:
        collect_stops(ctx)
        publish(ctx)
        notice(ctx)
    except novel_prompts.PromptError as e:
        log.fail(str(e))
        novel_tg.send("T5", f"⚠️ 프롬프트 정본을 읽지 못함 — {e}")
    if safety.tripped():
        log.degrade("graph:tripped")


def main() -> int:
    cli.setup_logging()
    return cli.execute("novel_publish", VERSION, body)


if __name__ == "__main__":
    sys.exit(main())

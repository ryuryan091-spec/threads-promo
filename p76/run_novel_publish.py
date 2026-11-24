"""
p76/run_novel_publish.py
========================
사건 소설(B형) 회차·휴재 안내 카드 게시 v2 — FB 예약 게시 (DN2026_0119, DSN-P76-NOVEL-003 §6).
게시 시각을 GitHub cron 에서 분리한다: 실행은 "하루 안에 한 번만 성공하면 되는 일"(예약 등록·취소·확인)만 한다.
한 실행 = 아래 순서 1회 (매일 6회 cron, 요일 필터 없음 — 금요일 휴재는 행 단위로 막는다).

  0 가드      PAGE76_ENABLED · PAGE76_NOVEL_ENABLED · PAGE76_NOVEL_PUBLISH_ENABLED
  1 대조      '발행예고'·'확인필요' 인데 FB게시ID 가 없는 행(응답 미확정·기록 누락) → scheduled_posts 에서
              같은 예약 시각 T 의 게시물이 정확히 1건이면 그 ID 를 적는다(재등록 없음, STD-PUB-02)
  2 예약 취소  FB게시ID 가 있고 T 이전인 행 중 ① '발행예고'·'확인필요' + 중단 체크 ② '중단'인데 취소 기록이 없는 행
              (생성 파이프라인이 먼저 '중단'으로 옮긴 경우) → DELETE → '중단'(결과코드 cancelled). 실패 → 정보 알림,
              단 T−now ≤ URGENT_CANCEL_WINDOW(6시간)이면 긴급 알림 1회(사람 조치 예외 — 페이지에서 직접 삭제, 리뷰 W1)
  2b 재예약   '발행예고' 인데 게시예정일 ≠ T 의 KST 날짜(생성 측 순연으로 날짜가 바뀜) + T 이후 아님 + 중단 아님
              → DELETE 성공 → '승인' + FB게시ID·등록예정시각 비움 + 결과코드 rescheduled → 5 가 새 날짜로 다시 등록.
              삭제 실패 → 그대로 두고 reschedule_cancel_failed + 정보 알림(6시간 이내 긴급) + 그 옛 T 날짜에는
              이번 실행에서 어떤 행도 등록하지 않는다(리뷰 C1). T ≤ now 면 건드리지 않는다
              게시예정일이 비었으면(생성 측 연재 일시 정지) 같은 경로로 예약 취소 → '승인'(paused_unscheduled)
  3 중단 수집  FB 객체가 없는 행의 중단 체크 → '중단' (run_novel.collect_stops · 부록 행)
  4 게시 확인  T 경과한 '발행예고' → is_published=true → '게시완료' + 게시일시(created_time) + T3 정보 알림.
              T+30분까지 미게시 → '확인필요' + 정보 알림. '확인필요' 는 매 실행 재조회 → 게시됐으면 '게시완료'
              중단 체크(또는 생성 측 '중단')인데 T 경과 후 공개됨 → unpublish_post(is_published=false) 성공 '중단'
              (unpublished) / 실패 긴급 알림(리뷰 W1 b)
              FB게시ID 없는 확인필요(T+30분 경과) → page_posts(T±2시간) 캡션 해시 대조(본문은 메모리에서만):
              1건 → 그 ID 로 '게시완료' · 0건 → '승인'(unconfirmed_not_found, 생성 측 catch_missed 가 순연) ·
              2건 이상 → 확인필요 유지 + 정보 알림 1회(리뷰 W4 b)
  5 예약 등록  구분 '회차'(바이블 정상) 또는 '부록'(휴재 안내 카드) + '승인' + 중단 아님 + FB게시ID 없음
              T = 게시예정일 KST + NOVEL_PUBLISH_TIME(Variable PAGE76_NOVEL_PUBLISH_HHMM, 기본 12:00)
              조건 NOVEL_SCHEDULE_MIN_LEAD_MIN(15분) ≤ T−now ≤ NOVEL_REGISTER_AHEAD_HOURS(36시간)
              부록 행이 등록 하한(15분)을 이미 넘겼으면 '중단'(결과코드 notice_expired) — 늦은 휴재 안내는 쓸모없다
              선점 표시(posting:)가 남은 행은 다른 판정보다 먼저 대조로 넘긴다(리뷰 W4 a)
              T 의 요일이 금(NOVEL_REST_WEEKDAY) → 등록 안 함(rest_day_row) · 하루 1건: 발행예고·확인필요·게시완료
              (·FB 객체가 남은) 행의 등록예정시각 날짜 ∪ 게시예정일 ∪ 이번 실행 재예약 취소 실패 날짜(리뷰 C1)에는
              등록 안 함 · 게시예정일 없는 행 제외 · T−now < 15분 미등록 → 게시 안 함(missed_slot,
              순연은 생성 측) · 게시 직전 재검사(규칙·열린 질문·공개 문구·법률 블록·품질 경고) → 카드 렌더 →
              선점 posting:<run_id> → schedule_photo(2단계) → '발행예고' + FB게시ID + 등록예정시각=T
              + 결과코드 scheduled:<mode> → scheduled_posts 로 존재 확인(없으면 schedule_unverified)
              정보 알림: "MM-DD HH:MM 게시 예약됨 — 멈추려면 DB-12 중단 체크" (조치 요구 아님)

  중단: ① DB-12 행 '중단' 체크(T 이전이면 예약 취소) ② Variable PAGE76_NOVEL_PUBLISH_ENABLED=false
        ③ Actions 에서 p76-novel-publish 워크플로 비활성화

  모드     | DB-12 기록 | 텔레그램 | Graph 쓰기
  dry_run | 없음        | 없음     | 없음 (판정·카드·로그만)
  shadow  | 없음        | 없음     | 없음 (판정·카드·로그만 + 실행 기록 DB-11)
  live    | 함          | 함       | 함
  probe   | 없음        | 실패 시만 | 예약 점검 1건 등록 → 확인 → 삭제 (E-1, 수동 실행 전용)

CONTROL 행(월 비용·마지막 생성 실행)은 읽지도 쓰지도 않는다 — 생성 파이프라인의 기록이다.
원고·캡션·노션 ID·FB ID 는 로그에 남기지 않는다 (공개 레포).
사용: python -m p76.run_novel_publish
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
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
    novel_policy,
    novel_prompts,
    novel_rules,
    novel_store,
    novel_tg,
    safety,
    settings,
)
from p76.notify import send as notify_send
from p76.novel_store import H_BODY, H_META, H_QUESTION, H_QUOTE, H_SUBTITLE
from p76.run_novel import Ctx, _prompts, collect_stops
from p76.runlog import RunLog

VERSION = "2.2.0"  # 2.2.0: DN2026_0126 게시 직전 검사는 차단 규칙만
# 2.1.2: DN2026_0125 회장 직접 승인 회차는 품질 경고 게이트 통과(novel_policy.release_ok)
# 2.1.1: DN2026_0124 live 금칙어 비면 새 예약 등록 중단(fail-closed)
# 2.1.0: 리뷰 C1 하루 1건 판정(T 날짜·취소 실패 날짜) · W1 긴급·비공개 전환 · W4 대조 · 일시 정지
# 2.0.0: DN2026_0119 FB 예약 게시(D-1 등록·취소·확인) · 지연 가드·텔레그램 예고 대기 제거 · 부록 · probe
# 2.0.0 P9: 순연으로 게시예정일이 바뀐 예약 재등록(rescheduled) · 부록 만료(notice_expired)
# 1.1.1: 예약 지연 가드(MAX_LAG_MIN) · 1.1.0: 게시 직전 선점(posting:) · 미확정 재게시 차단 · 공개 문구 재검사

logger = logging.getLogger(__name__)
S = settings

PUB_KINDS = (S.NK_EPISODE, S.NK_NOTICE)          # DN2026_0119: 회차 + 순연 슬롯 휴재 안내 카드(부록)
HOLDS_SLOT = (S.NS_SCHEDULED, S.NS_PUBLISHED, S.NS_CHECK)  # 그 게시예정일을 차지한 상태(하루 1건)
FB_STATES = (S.NS_SCHEDULED, S.NS_CHECK)          # FB 예약 객체가 있을 수 있는 상태 — 중단 수집에서 뺀다
CONFIRM_GRACE = timedelta(minutes=30)             # T 경과 후 이 시간까지 미게시면 '확인필요'
POSTING = "posting:"  # 결과코드 선점 표시 — 예약 호출 직전에 쓰고 결과로 덮어쓴다(STD-PUB-01·02)
CANCELLED = "cancelled"
UNPUBLISHED = "unpublished"
URGENT_SENT = "cancel_failed:urgent"                # 긴급 알림을 이미 보낸 행 표시(행당 1회)
INFO_SENT = "cancel_failed:info"                    # 정보 알림(6시간 초과)을 이미 보낸 행 표시(행당 1회, R6-02 P2)
URGENT_CANCEL_WINDOW = timedelta(hours=6)          # 취소 실패 시 T 까지 이 시간 이내면 긴급(사람 조치 예외, W1)
MATCH_WINDOW = timedelta(hours=2)                  # FB ID 없는 확인필요 대조: page_posts T±2시간(W4)
MATCH_GIVE_UP = timedelta(hours=48)                # FB ID 없는 확인필요 대조를 그만두는 시점(R6-03)
STOP_DONE = (CANCELLED, UNPUBLISHED, "unpublish_failed", "stopped:not_published")  # 중단 행 후속 처리 끝
PROBE_TEXT = "사건파일76 예약 점검"
PROBE_LEAD_DAYS = 29


def _live() -> bool:
    return settings.MODE == "live"


def _tg(template: str, text: str, row: dict | None = None) -> None:
    """정보성 알림 — live 만(shadow 는 판정·카드·로그만, DN2026_0119)."""
    if _live():
        novel_tg.send(template, text, novel_tg.page_link(row["id"]) if row else "")


def _hm(dt: datetime) -> str:
    return dt.astimezone(notion.KST).strftime("%m-%d %H:%M")


def slot_time(row: dict) -> datetime | None:
    """예약 시각 T = 게시예정일 KST + NOVEL_PUBLISH_TIME."""
    due = notion.kst_date(row, S.N_DUE)
    if due is None:
        return None
    hh, mm = S.NOVEL_PUBLISH_TIME
    return datetime.combine(due, time(hh, mm), tzinfo=notion.KST)


def _t(row: dict) -> datetime | None:
    """등록된 행은 등록예정시각(=FB 예약 시각), 없으면 게시예정일로 계산."""
    return notion.date_value(row, S.N_REGISTER_AT) or slot_time(row)


def _label(row: dict) -> str:
    return novel_store.key(row) or "?"


def _bible_ok(ctx: Ctx, page: dict) -> bool:
    bible = novel_store.find(ctx.rows, novel_store.bible_key(novel_store.season(page)))
    return bible is not None and not novel_store.stopped(bible)


def _pub_rows(ctx: Ctx) -> list[dict]:
    return [p for p in ctx.rows if novel_store.kind(p) in PUB_KINDS]


# ── 1 대조 (FB게시ID 없는 발행예고·확인필요) ─────────────────
class _Sched:
    """scheduled_posts 1회 조회 캐시 (실행당)."""

    def __init__(self) -> None:
        self.loaded = False
        self.ok = False
        self.posts: list[dict] = []

    def get(self) -> tuple[bool, list[dict]]:
        if not self.loaded:
            self.ok, self.posts, _ = graph.scheduled_posts()
            self.loaded = True
        return self.ok, self.posts

    def invalidate(self) -> None:
        self.loaded = False


def _at(post: dict) -> int | None:
    try:
        return int(post.get("scheduled_publish_time"))
    except (TypeError, ValueError):
        return None


def reconcile(ctx: Ctx, sp: _Sched) -> None:
    used = {notion.plain(p, S.N_FB_ID) for p in _pub_rows(ctx)} - {""}
    for row in _pub_rows(ctx):
        if novel_store.state(row) not in FB_STATES or notion.plain(row, S.N_FB_ID):
            continue
        t = _t(row)
        if t is None or t <= ctx.now:
            continue  # T 경과 후에는 예약 목록에서 사라진다 — 게시 확인 단계가 확인필요로 보낸다
        ok, posts = sp.get()
        if not ok:
            ctx.log.degrade("reconcile:read_failed")
            return
        hits = [p for p in posts if _at(p) == int(t.timestamp()) and str(p["id"]) not in used]
        if len(hits) != 1:
            ctx.log.degrade(f"reconcile:{'none' if not hits else 'ambiguous'}")
            continue
        post_id = str(hits[0]["id"])
        used.add(post_id)
        logger.info(f"[NovelPub] {_label(row)} 예약 대조 — 같은 시각 예약 1건 확인")
        if _live():
            novel_store.update(row, {S.N_FB_ID: notion.v_rich(post_id),
                                     S.N_RESULT: notion.v_rich(f"reconciled:{settings.RUN_ID}")})
            ctx.log.writes += 1


# ── 2 예약 취소 ───────────────────────────────────────────
def _cancel_target(row: dict, now: datetime) -> bool:
    t = _t(row)
    if not notion.plain(row, S.N_FB_ID) or t is None or t <= now:
        return False
    st = novel_store.state(row)
    if st in FB_STATES:
        return notion.checkbox(row, S.N_STOP)
    # 생성 파이프라인(collect_stops)이 FB 예약을 지우지 않고 '중단'으로 옮긴 행 — 예약을 여기서 취소한다
    return st == S.NS_STOPPED and notion.plain(row, S.N_RESULT) != CANCELLED


def cancel(ctx: Ctx) -> None:
    for row in _pub_rows(ctx):
        if not _cancel_target(row, ctx.now):
            if (novel_store.state(row) == S.NS_SCHEDULED and notion.checkbox(row, S.N_STOP)
                    and not notion.plain(row, S.N_FB_ID)):
                ctx.log.degrade("cancel:no_fb_id")  # 대조 실패 — 취소할 대상을 모른다
            continue
        label = _label(row)
        if not _live():
            logger.info(f"[NovelPub][{settings.MODE}] {label} 예약 취소 대상 — 생략")
            continue
        if not safety.graph_write_allowed():
            ctx.log.degrade("cancel:graph_write_blocked")
            return
        res = graph.delete_post(notion.plain(row, S.N_FB_ID))
        if res["status"] == "ok":
            props = {S.N_RESULT: notion.v_rich(CANCELLED)}
            if novel_store.state(row) == S.NS_STOPPED:
                novel_store.update(row, props)
            else:
                novel_store.transition(row, S.NS_STOPPED, {S.N_STOP: notion.v_checkbox(True), **props})
            ctx.log.writes += 2
            logger.info(f"[NovelPub] {label} 예약 취소 → 중단")
            _tg("T1d", f"⏸ {label} 예약 게시를 취소했습니다(중단).", row)
            continue
        ctx.log.degrade(f"cancel_failed:{res['status']}")
        logger.warning(f"[NovelPub] {label} 예약 취소 실패 {res.get('reason', '')}")
        _cancel_failed(ctx, row, _t(row), f"⚠️ {label} 예약 취소 실패({res.get('reason', '')}) — {_hm(_t(row))} "
                                          f"게시 예정 상태입니다. 다음 실행에서 다시 취소를 시도합니다.")


def _cancel_failed(ctx: Ctx, row: dict, t: datetime, info: str) -> None:
    """예약 취소 실패 알림(live 전용 경로에서만 호출). T 까지 6시간 이내면 긴급 1회 — 다음 실행이 T 전에
    없을 수 있어 사람이 페이지에서 직접 지워야 한다(리뷰 W1 a, 정상 경로 조치 요구 0의 예외)."""
    if t - ctx.now > URGENT_CANCEL_WINDOW:
        if notion.plain(row, S.N_RESULT).split("|")[0] == INFO_SENT:
            return  # R6-02 P2: 정보 알림도 행당 1회
        novel_tg.send("T5", info, novel_tg.page_link(row["id"]))
        if _live():
            novel_store.update(row, {S.N_RESULT: notion.v_rich(INFO_SENT)})
            ctx.log.writes += 1
        return
    ctx.log.degrade("cancel_failed:urgent")
    if notion.plain(row, S.N_RESULT) == URGENT_SENT:
        return  # 이미 보냄(행당 1회)
    notify_send(f"{_label(row)} 예약 취소 실패 — {_hm(t)} 공개 예정, 페이지에서 직접 삭제 필요", urgent=True)
    novel_store.update(row, {S.N_RESULT: notion.v_rich(URGENT_SENT)})
    ctx.log.writes += 1


# ── 2b 재예약 (DN2026_0119 A2 순연으로 게시예정일이 바뀐 예약) ──
def reschedule(ctx: Ctx) -> set:
    """반환: 이번 실행에서 예약 취소에 실패한 옛 T 의 KST 날짜 — register 가 그 날짜에 아무 행도 등록하지 않는다(C1).
    게시예정일이 비면(생성 측 연재 일시 정지 계약) 같은 경로로 취소 → '승인'(paused_unscheduled)."""
    blocked: set = set()
    for row in _pub_rows(ctx):
        if novel_store.state(row) != S.NS_SCHEDULED or notion.checkbox(row, S.N_STOP):
            continue
        t, due = notion.date_value(row, S.N_REGISTER_AT), notion.kst_date(row, S.N_DUE)
        paused = due is None
        if t is None or t <= ctx.now or (not paused and t.astimezone(notion.KST).date() == due):
            continue
        label, post_id = _label(row), notion.plain(row, S.N_FB_ID)
        if not post_id:
            ctx.log.degrade("reschedule:no_fb_id")  # 대조 전 — 다음 실행에서 다시 본다(그 T 날짜는 계속 차지)
            continue
        if not _live():
            logger.info(f"[NovelPub][{settings.MODE}] {label} 게시예정일 {'비움' if paused else '변경'} — "
                        f"재예약 대상(생략)")
            continue
        if not safety.graph_write_allowed():
            ctx.log.degrade("reschedule:graph_write_blocked")
            return blocked
        res = graph.delete_post(post_id)
        if res["status"] != "ok":
            ctx.log.degrade("reschedule_cancel_failed")
            blocked.add(t.astimezone(notion.KST).date())
            _cancel_failed(ctx, row, t, f"ℹ️ {label} 게시예정일이 {'비었지만' if paused else '바뀌었지만'} 기존 예약"
                                        f"({_hm(t)}) 취소에 실패했습니다 — 다음 실행에서 다시 시도합니다.")
            continue
        code = "paused_unscheduled" if paused else "rescheduled"
        novel_store.transition(row, S.NS_APPROVED, {S.N_FB_ID: notion.v_rich(""), S.N_REGISTER_AT: notion.v_date(None),
                                                    S.N_RESULT: notion.v_rich(code)})
        ctx.log.writes += 2
        logger.info(f"[NovelPub] {label} 게시예정일 {'비움(일시 정지)' if paused else '변경'} — 기존 예약 취소")
    return blocked


# ── 3 중단 수집 (FB 객체 없는 행) ──────────────────────────
def stops(ctx: Ctx) -> None:
    if not _live():
        return  # shadow·dry_run 은 DB-12 를 쓰지 않는다
    collect_stops(dataclasses.replace(ctx, rows=[p for p in ctx.rows if novel_store.state(p) not in FB_STATES]))
    for row in ctx.rows:
        if (novel_store.kind(row) == S.NK_NOTICE and notion.checkbox(row, S.N_STOP)
                and novel_store.state(row) == S.NS_APPROVED):
            novel_store.transition(row, S.NS_STOPPED, {S.N_RESULT: notion.v_rich("stopped:notion")})
            ctx.log.writes += 1


# ── 4 게시 확인 ───────────────────────────────────────────
def _created(body: dict, fallback: datetime) -> datetime:
    raw = str(body.get("created_time") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _to_check(ctx: Ctx, row: dict, code: str, text: str) -> None:
    if _live():
        novel_store.transition(row, S.NS_CHECK, {S.N_RESULT: notion.v_rich(_keep_hash(row, code))})
        ctx.log.writes += 1
    ctx.log.degrade(code.split(":", 1)[0])
    _tg("T5", text, row)


def confirm(ctx: Ctx) -> None:
    for row in _pub_rows(ctx):
        st = novel_store.state(row)
        t, label, post_id = _t(row), _label(row), notion.plain(row, S.N_FB_ID)
        if st == S.NS_STOPPED:
            _stopped_after_t(ctx, row, t, post_id)
            continue
        if st not in FB_STATES:
            continue
        if t is None or t > ctx.now:
            continue
        late = ctx.now > t + CONFIRM_GRACE
        if not post_id:
            if late:
                if st == S.NS_SCHEDULED:
                    _to_check(ctx, row, "unconfirmed:no_fb_id",
                              f"ℹ️ {label} {_hm(t)} 예약 결과를 확인할 수 없습니다(FB게시ID 없음) — 확인필요로 두었습니다.")
                _match_posted(ctx, row, t)
            continue
        ok, body, reason = graph.get_post(post_id)
        if ok and body.get("is_published") is True:
            if notion.checkbox(row, S.N_STOP):  # 중단 체크인데 공개됨(취소 실패·T 이후 체크) → 비공개 전환(W1 b)
                _unpublish(ctx, row, post_id, t)
                continue
            at = _created(body, t)
            if _live():
                novel_store.transition(row, S.NS_PUBLISHED, {S.N_PUBLISHED_AT: notion.v_date(at),
                                                             S.N_RESULT: notion.v_rich("published")})
                ctx.log.writes += 1
            logger.info(f"[NovelPub] {label} 게시 확인 → 게시완료")
            _tg("T3", f"📤 {label} 게시됨 — {_hm(at)}.", row)
            continue
        if not ok:
            ctx.log.degrade("confirm:read_failed")
            logger.warning(f"[NovelPub] {label} 게시 확인 조회 실패 {reason}")
        if st == S.NS_SCHEDULED and late:
            _to_check(ctx, row, "unconfirmed:not_published" if ok else "unconfirmed:read_failed",
                      f"ℹ️ {label} {_hm(t)} 예약 게시가 아직 확인되지 않았습니다 — 확인필요로 두고 매 실행 다시 조회합니다.")


def _stopped_after_t(ctx: Ctx, row: dict, t: datetime | None, post_id: str) -> None:
    """'중단' 행인데 FB 예약 객체가 남은 채 T 가 지났다(생성 측이 중단으로 옮긴 뒤 취소 실패가 이어짐).
    공개됐으면 비공개 전환, T+30분까지 미공개면 끝 표시(stopped:not_published) — 매 실행 재조회하지 않는다."""
    if not post_id or t is None or t > ctx.now or notion.plain(row, S.N_RESULT) in STOP_DONE:
        return
    ok, body, reason = graph.get_post(post_id)
    if ok and body.get("is_published") is True:
        _unpublish(ctx, row, post_id, t)
        return
    if not ok:
        ctx.log.degrade("confirm:read_failed")
        logger.warning(f"[NovelPub] {_label(row)} 중단 행 게시 확인 조회 실패 {reason}")
    elif ctx.now > t + CONFIRM_GRACE and _live():
        novel_store.update(row, {S.N_RESULT: notion.v_rich("stopped:not_published")})
        ctx.log.writes += 1


def _unpublish(ctx: Ctx, row: dict, post_id: str, t: datetime) -> None:
    """중단 체크된 게시물이 T 이후 공개됨 → POST /{post_id} is_published=false (리뷰 W1 b).
    성공 → '중단'(unpublished) · 실패 → 긴급 1회(사람 조치 예외) + 끝 상태 기록(재시도하지 않음)."""
    label, st = _label(row), novel_store.state(row)
    if not _live():
        logger.info(f"[NovelPub][{settings.MODE}] {label} 중단 체크 후 공개됨 — 비공개 전환 대상(생략)")
        return
    if not safety.graph_write_allowed():
        ctx.log.degrade("unpublish:graph_write_blocked")
        return
    res = graph.unpublish_post(post_id)
    if res["status"] == "ok":
        props = {S.N_STOP: notion.v_checkbox(True), S.N_RESULT: notion.v_rich(UNPUBLISHED)}
        if st == S.NS_STOPPED:
            novel_store.update(row, props)
        else:
            novel_store.transition(row, S.NS_STOPPED, props)
        ctx.log.writes += 1
        ctx.log.degrade(UNPUBLISHED)
        logger.info(f"[NovelPub] {label} 중단 체크 후 공개된 게시물 — 비공개 전환 → 중단")
        _tg("T1d", f"⏸ {label} 중단 체크된 게시물이 {_hm(t)}에 공개되어 비공개로 전환했습니다.", row)
        return
    ctx.log.degrade(f"unpublish_failed:{res['status']}")
    logger.warning(f"[NovelPub] {label} 비공개 전환 실패 {res.get('reason', '')}")
    props = {S.N_RESULT: notion.v_rich("unpublish_failed")}
    if t is not None and not notion.date_value(row, S.N_PUBLISHED_AT):
        props[S.N_PUBLISHED_AT] = notion.v_date(t)  # R6-02 P3: 댓글 숨김 대상(게시일시 필터)에 들어가도록
    if st == S.NS_STOPPED:
        novel_store.update(row, props)
    else:  # 실제로 공개된 상태 — 기록도 게시완료로 맞춘다(같은 날 추가 등록 차단)
        novel_store.transition(row, S.NS_PUBLISHED, props)
    ctx.log.writes += 1
    notify_send(f"{label} 중단 체크된 게시물이 {_hm(t)}에 공개됨 — 비공개 전환 실패, 페이지에서 직접 삭제 필요",
                urgent=True)


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


HASH_TAG = "h:"  # 등록 시점 캡션 해시(앞 16자) — 결과코드에 실어 대조에 쓴다(R6 재리뷰: 재구성 해시 오판 방지)


def _stored_hash(row: dict) -> str:
    return next((part[len(HASH_TAG):] for part in notion.plain(row, S.N_RESULT).split("|")
                 if part.startswith(HASH_TAG)), "")


def _keep_hash(row: dict, code: str) -> str:
    h = _stored_hash(row)
    has = any(part.startswith(HASH_TAG) for part in code.split("|"))  # R6-02 P1: 파트 단위 판정
    return f"{code}|{HASH_TAG}{h}" if h and not has else code


def _caption(ctx: Ctx, row: dict) -> str:
    """등록 때와 같은 방식으로 캡션을 다시 만든다(알림·기록 없음). 품질 경고 대기면 빈 문자열."""
    if novel_store.kind(row) == S.NK_EPISODE:
        built = _episode_post(ctx, row, notify=False)
        return built[1] if built else ""
    return _notice_post(row)[1]


def _match_posted(ctx: Ctx, row: dict, t: datetime) -> None:
    """FB게시ID 없는 확인필요(T+30분 경과) → 공개 게시물(T±2시간) 캡션 해시 대조(리뷰 W4 b).
    캡션·게시물 본문은 메모리에서만 비교하고 로그·기록에 남기지 않는다(공개 레포)."""
    label = _label(row)
    if notion.plain(row, S.N_RESULT).split("|")[0] == "unconfirmed:closed":
        return  # R6-03: 48시간 대조 종료 — 더 조회하지 않는다
    caption = _caption(ctx, row)
    if not caption:
        ctx.log.degrade("match:no_caption")
        return
    ok, posts, _ = graph.page_posts(int((t - MATCH_WINDOW).timestamp()), int((t + MATCH_WINDOW).timestamp()),
                                    with_message=True)
    if not ok:
        ctx.log.degrade("match:read_failed")
        return
    used = {notion.plain(p, S.N_FB_ID) for p in _pub_rows(ctx)} - {""}
    want = _stored_hash(row)  # 등록 시점 해시 우선(법률 블록 확인일 변경 등으로 재구성 캡션이 달라질 수 있음)
    hits = [p for p in posts if str(p["id"]) not in used
            and _digest(str(p.get("message") or "")).startswith(want)] if want else None
    if hits is None:
        want = _digest(caption)
        hits = [p for p in posts if str(p["id"]) not in used and _digest(str(p.get("message") or "")) == want]
    if len(hits) == 1:
        at = _created(hits[0], t)
        if _live():
            novel_store.transition(row, S.NS_PUBLISHED, {S.N_FB_ID: notion.v_rich(str(hits[0]["id"])),
                                                         S.N_PUBLISHED_AT: notion.v_date(at),
                                                         S.N_RESULT: notion.v_rich("published:matched")})
            ctx.log.writes += 1
        ctx.log.degrade("matched")
        logger.info(f"[NovelPub] {label} 공개 게시물 대조 1건 → 게시완료")
        _tg("T3", f"📤 {label} 게시됨 — {_hm(at)} (예약 응답 미확정 후 대조로 확인).", row)
        return
    if not hits:
        # R6-02 N1(3관점 결정): 0건이어도 '승인'으로 되돌리지 않는다 — 실제 공개됐는데 created_time·메시지 형식이
        # 달라 못 찾은 경우 재등록이 중복 게시가 된다(STD-PUB-02 우선). 확인필요 유지 + T5 1회(사람 확인 예외).
        # 자동 되돌림은 E-1 실측(created_time 의미·message 보존) 후 별도 결정.
        head = notion.plain(row, S.N_RESULT).split("|")[0]
        if ctx.now > t + MATCH_GIVE_UP:  # R6-03: 48시간 뒤 대조 종료(매 실행 조회·degrade 반복 방지)
            if _live():
                novel_store.update(row, {S.N_RESULT: notion.v_rich(_keep_hash(row, "unconfirmed:closed"))})
                ctx.log.writes += 1
            logger.info(f"[NovelPub] {label} 대조 종료(48시간) — 확인필요 유지")
            return
        ctx.log.degrade("unconfirmed_not_found")
        if head == "unconfirmed:not_found":
            return
        if _live():
            novel_store.update(row, {S.N_RESULT: notion.v_rich(_keep_hash(row, "unconfirmed:not_found"))})
            ctx.log.writes += 1
        logger.info(f"[NovelPub] {label} 공개 게시물 대조 0건 → 확인필요 유지")
        _tg("T5", f"⚠️ {label} {_hm(t)} 예약 결과 미확정 — 공개 게시물에서 찾지 못했습니다. 중복 방지를 위해 재등록하지 "
                  f"않습니다. 페이지에서 확인해 주세요 — 게시됐으면 FB게시ID 를 적고, 게시되지 않았으면 상태를 '승인'으로 "
                  f"바꾸면 자동으로 다음 게시일에 다시 예약됩니다.", row)
        return
    ctx.log.degrade("match:ambiguous")
    if notion.plain(row, S.N_RESULT).split("|")[0] == "unconfirmed:ambiguous":
        return  # 정보 알림은 행당 1회
    if _live():
        novel_store.update(row, {S.N_RESULT: notion.v_rich(_keep_hash(row, "unconfirmed:ambiguous"))})
        ctx.log.writes += 1
    _tg("T5", f"ℹ️ {label} {_hm(t)} 전후 같은 캡션 게시물이 {len(hits)}건 — 확인필요로 둡니다.", row)


# ── 5 예약 등록 ───────────────────────────────────────────
def _taken_days(ctx: Ctx, blocked: set | frozenset = frozenset()) -> set:
    """하루 1건 판정(리뷰 C1): 그 날짜를 차지한 행의 (a) 등록예정시각 T 의 KST 날짜 (b) 게시예정일
    (c) 이번 실행 재예약 취소 실패 날짜(blocked) — 의 합집합. 생성 측이 게시예정일을 옮긴 뒤 취소가 실패해도
    옛 T 날짜가 빈 날로 보이지 않는다. FB 예약 객체가 남은 '중단' 행(취소 미완료)도 T 날짜를 차지한다."""
    days = set(blocked)
    for p in _pub_rows(ctx):
        st, fb_id = novel_store.state(p), notion.plain(p, S.N_FB_ID)
        live_obj = fb_id and (st != S.NS_STOPPED or notion.plain(p, S.N_RESULT) not in STOP_DONE)
        if st in HOLDS_SLOT or live_obj:
            for day in (notion.kst_date(p, S.N_DUE), notion.kst_date(p, S.N_REGISTER_AT)):
                if day is not None:
                    days.add(day)
        elif _claimed(p) and notion.kst_date(p, S.N_REGISTER_AT) is not None:
            days.add(notion.kst_date(p, S.N_REGISTER_AT))  # R6-02 N2: 선점 중 행의 T 도 그 날을 차지(예약이 걸렸을 수 있음)
    return days


def _candidates(ctx: Ctx) -> list[tuple[datetime, dict]]:
    out = []
    for p in _pub_rows(ctx):
        if novel_store.state(p) != S.NS_APPROVED or notion.plain(p, S.N_FB_ID):
            continue
        if notion.checkbox(p, S.N_STOP) and not _claimed(p):
            continue  # 선점 중 행은 중단 체크여도 먼저 대조한다(R6-03 — 예약이 걸렸다면 발행예고로 넘어가 취소 경로로)
        if novel_store.kind(p) == S.NK_EPISODE and not _bible_ok(ctx, p):
            continue
        t = slot_time(p)
        if _claimed(p) and notion.date_value(p, S.N_REGISTER_AT) is not None:
            t = notion.date_value(p, S.N_REGISTER_AT)  # R6-02 N2: 선점 시각 T 로 대조(N_DUE 가 바뀌었거나 비어도)
        if t is not None:
            out.append((t, p))
    out.sort(key=lambda x: (x[0], novel_store.season(x[1]), novel_store.episode(x[1])))
    return out


def _claimed(row: dict) -> bool:
    return any(part.startswith(POSTING) for part in notion.plain(row, S.N_RESULT).split("|"))


def register(ctx: Ctx, sp: _Sched, blocked: set | frozenset = frozenset()) -> None:
    taken = _taken_days(ctx, blocked)
    lo = timedelta(minutes=S.NOVEL_SCHEDULE_MIN_LEAD_MIN)
    hi = timedelta(hours=S.NOVEL_REGISTER_AHEAD_HOURS)
    for t, row in _candidates(ctx):
        label, lead = _label(row), t - ctx.now
        if _claimed(row):  # 직전 실행이 예약을 시도했는데 결과를 기록하지 못함 → 다른 판정보다 먼저 대조(W4 a)
            _unconfirmed(ctx, row, t, sp)
            taken.add(t.date())
            continue
        if lead > hi:
            continue
        if t.date() in blocked:  # 재예약 취소 실패 날짜 — 어떤 행도 등록하지 않는다(C1)
            ctx.log.degrade("day_blocked")
            logger.info(f"[NovelPub] {label} 같은 날 기존 예약 취소 실패 — 등록하지 않음")
            continue
        if novel_store.kind(row) == S.NK_NOTICE and lead < lo:
            _expire_notice(ctx, row)
            continue
        if t.weekday() == S.NOVEL_REST_WEEKDAY:
            ctx.log.degrade("rest_day_row")
            logger.warning(f"[NovelPub] {label} 게시예정일이 휴재 요일 — 등록하지 않음")
            continue
        if lead < lo:
            ctx.log.degrade("missed_slot")
            logger.warning(f"[NovelPub] {label} 예약 시각까지 {S.NOVEL_SCHEDULE_MIN_LEAD_MIN}분 미만 — 게시하지 않음")
            continue
        if t.date() in taken:
            ctx.log.degrade("day_taken")
            logger.info(f"[NovelPub] {label} 같은 게시예정일에 이미 예약·게시 — 등록하지 않음(하루 1건)")
            continue
        if _register_one(ctx, row, t, sp):
            taken.add(t.date())
        if safety.tripped():
            return


def _expire_notice(ctx: Ctx, row: dict) -> None:
    """휴재 안내(부록) 행의 게시 시각이 지났다 — 게시하지 않고 '중단'(notice_expired)."""
    ctx.log.degrade("notice_expired")
    logger.info(f"[NovelPub] {_label(row)} 휴재 안내 게시 시각 경과 — 중단")
    if _live():
        novel_store.transition(row, S.NS_STOPPED, {S.N_RESULT: notion.v_rich("notice_expired")})
        ctx.log.writes += 1


def _unconfirmed(ctx: Ctx, row: dict, t: datetime, sp: _Sched, code: str = "") -> None:
    code = code or f"posting_unconfirmed:{settings.RUN_ID}"
    if _live():
        novel_store.transition(row, S.NS_SCHEDULED, {S.N_REGISTER_AT: notion.v_date(t),
                                                     S.N_RESULT: notion.v_rich(_keep_hash(row, code))})
        ctx.log.writes += 1
    ctx.log.degrade(code.split(":", 1)[0])
    sp.invalidate()
    reconcile(ctx, sp)


def _hold(ctx: Ctx, row: dict, code: str) -> None:
    label = _label(row)
    ctx.log.fail(f"{label}:publish_check")
    if not _live():
        logger.info(f"[NovelPub][{settings.MODE}] {label} 게시 전 검사 실패 — {code} (보류 생략)")
        return
    novel_store.transition(row, S.NS_HELD, {S.N_RESULT: notion.v_rich(code[:1900])})
    ctx.log.writes += 1
    novel_tg.send("T5", f"⚠️ {label} 게시 전 검사 실패 — {code}. 보류로 옮겼습니다.", novel_tg.page_link(row["id"]))


def _episode_post(ctx: Ctx, row: dict, notify: bool = True) -> tuple[list[str], str, str, str, str] | None:
    """회차: 게시 직전 재검사 → (오류, 캡션, 카드 라벨, 헤드라인, 부제). 품질 경고 알림 대기면 None.
    notify=False: 대조용 캡션 재구성(품질 경고 알림을 보내지 않는다)."""
    n, m = novel_store.season(row), novel_store.episode(row)
    if _live() and notify:
        novel_policy.notify_quality(row, ctx.log)
    meta = novel_store.json_section(row, H_META)
    if meta.get("quality_warnings") and not novel_policy.release_ok(row):
        ctx.log.degrade("quality:notice_pending")
        return None
    p = _prompts(ctx)
    secs = novel_store.sections(row)
    body, question = secs.get(H_BODY, ""), secs.get(H_QUESTION, "")
    quoted = [int(x) for x in meta.get("quoted_journals") or []]
    journals = novel_prompts.journal_text(quoted) if quoted else {}
    errors = [e for e in novel_rules.check_episode(body, m, banned_terms=S.BANNED_TERMS, journals=journals)
              if not e.startswith(("R01:length", "R09"))]
    errors += novel_rules.question_ok(question)
    errors += novel_rules.check_public_fields(
        {"card_quote": secs.get(H_QUOTE, ""), "subtitle": secs.get(H_SUBTITLE, ""), "question": question},
        m, banned_terms=S.BANNED_TERMS)
    filled, law_errors = novel_rules.fill_laws(body, p.laws, ctx.today)
    errors += law_errors
    # DN2026_0126: 게시 직전에는 차단 규칙(식별·위해 어휘·자리표시자·법률 문안 없음)만 막는다. 나머지는 생성 때 경고로 알림.
    errors = novel_policy.blocking(errors)
    lo, hi = novel_policy.WARN_BODY_RANGE
    if not lo <= len(body.strip()) <= hi:  # R6-03 W3: 승인 뒤 잘리거나 폭주한 본문은 차단
        errors.append("R01:out_of_range")
    subtitle = secs.get(H_SUBTITLE, "").strip()
    caption = f"{filled.strip()}\n\n{novel_rules.footer(n, m, question)}"
    return errors, caption, f"사건 소설 시즌 {n} · 제{m}화", secs.get(H_QUOTE, "").strip() or subtitle, subtitle


def _notice_post(row: dict) -> tuple[list[str], str, str, str, str]:
    """부록(휴재 안내 카드, DN2026_0119 A2): 캡션 = 본문 섹션, 공개 문구·금칙어 검사만, 꼬리말 없음."""
    n = novel_store.season(row)
    secs = novel_store.sections(row)
    caption, subtitle = secs.get(H_BODY, "").strip(), secs.get(H_SUBTITLE, "").strip()
    errors = [] if caption else ["notice:empty_body"]
    errors += novel_rules.check_public_fields(
        {"card_quote": secs.get(H_QUOTE, ""), "subtitle": subtitle}, 0, banned_terms=S.BANNED_TERMS)
    errors += [f"{c}@body" for c in novel_rules.common_checks(caption, S.BANNED_TERMS)]
    label = f"사건 소설 시즌 {n} · 휴재 안내" if n else "사건 소설 · 휴재 안내"
    return sorted(set(errors)), caption, label, secs.get(H_QUOTE, "").strip() or subtitle, subtitle


def _claim(ctx: Ctx, row: dict, caption: str = "", t: datetime | None = None) -> bool:
    """예약 직전 선점: 결과코드에 posting:<run_id>(|h:<캡션 해시 16자>) 를 쓰고 다시 읽어 확인한다(STD-PUB-01·02)."""
    mark = f"{POSTING}{settings.RUN_ID}" + (f"|{HASH_TAG}{_digest(caption)[:16]}" if caption else "")
    props = {S.N_RESULT: notion.v_rich(mark)}
    if t is not None:  # R6-02 N2: 선점과 함께 예약 시각 T 를 남긴다 — 생성 측이 N_DUE 를 옮겨도 대조는 이 값으로
        props[S.N_REGISTER_AT] = notion.v_date(t)
    novel_store.update(row, props)
    ctx.log.writes += 1
    fresh = novel_store.refresh(row)
    seen = notion.plain(fresh, S.N_RESULT)
    if (seen != mark or novel_store.state(fresh) != S.NS_APPROVED or notion.checkbox(fresh, S.N_STOP)
            or notion.plain(fresh, S.N_FB_ID)):
        ctx.log.degrade("novel_publish:claim_mismatch")
        logger.info(f"[NovelPub] {_label(row)} 선점 확인 불일치 — 등록하지 않음")
        if seen == mark or not _claimed(fresh):
            _release(row, "claim_mismatch")
        return False
    return True


def _release(row: dict, code: str) -> None:
    """미등록이 확정된 선점 해제(posting: 제거, 선점 때 남긴 등록예정시각도 비움)."""
    novel_store.update(row, {S.N_RESULT: notion.v_rich(code), S.N_REGISTER_AT: notion.v_date(None)})


def _register_one(ctx: Ctx, row: dict, t: datetime, sp: _Sched) -> bool:
    """True = 이 게시예정일을 차지했다(예약됨·미확정)."""
    label = _label(row)
    row["properties"] = novel_store.refresh(row).get("properties", row.get("properties", {}))
    if novel_store.state(row) != S.NS_APPROVED or notion.checkbox(row, S.N_STOP):
        logger.info(f"[NovelPub] {label} 등록 직전 확인 — 중단·상태 변경으로 등록하지 않음")
        return False
    built = _episode_post(ctx, row) if novel_store.kind(row) == S.NK_EPISODE else _notice_post(row)
    if built is None:
        return False
    errors, caption, card_label, headline, subtitle = built
    if errors:
        _hold(ctx, row, "publish_check:" + ",".join(errors))
        return False
    work = Path(tempfile.mkdtemp(prefix="p76n_"))
    try:
        try:
            image = card_render.render(card_label, headline, subtitle, str(work))
        except card_render.RenderError as e:
            ctx.log.fail(str(e))
            _tg("T5", f"⚠️ {label} 카드 렌더 실패 — {e}. 다음 실행에서 다시 시도합니다.")
            return False
        if not _live():
            logger.info(f"[NovelPub][{settings.MODE}] {label} 검사·카드 통과 — 예약 {_hm(t)} 등록 생략")
            return True
        if not safety.graph_write_allowed():  # 회로 차단이면 선점도 하지 않는다
            ctx.log.degrade("novel_publish:graph_write_blocked")
            return False
        if not _claim(ctx, row, caption, t):
            return False
        try:
            res = graph.schedule_photo(image, caption, int(t.timestamp()), ctx.now.timestamp())
        except safety.Tripped:  # 요청 전 차단(미등록 확정) → 선점 해제 후 그대로 올린다
            _release(row, "tripped")
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    status, reason = res["status"], res.get("reason", "")
    if status == "ok":
        novel_store.transition(row, S.NS_SCHEDULED, {
            S.N_FB_ID: notion.v_rich(res["object_id"]), S.N_REGISTER_AT: notion.v_date(t),
            S.N_RESULT: notion.v_rich(f"scheduled:{settings.MODE}")})
        ctx.log.writes += 1
        logger.info(f"[NovelPub] {label} 예약 등록 — {_hm(t)} 게시")
        sp.invalidate()
        ok, posts = sp.get()
        if not ok or res["object_id"] not in {str(p["id"]) for p in posts}:
            ctx.log.degrade("schedule_unverified")
        _tg("T6", f"📅 {label} {_hm(t)} 게시 예약됨 — 멈추려면 노션 DB-12 이 행의 '중단'을 체크하세요.", row)
        return True
    if status == "unknown":  # 예약됐을 수 있다 — 재등록하지 않고 같은 시각 예약을 대조한다
        ctx.log.fail(f"unknown:{reason}")
        _unconfirmed(ctx, row, t, sp, f"unknown:{reason}")
        return True
    ctx.log.fail(f"failed:{reason}")
    _release(row, f"failed:{reason}")
    novel_tg.send("T5", f"⚠️ {label} 예약 등록 실패 — {reason}. 다음 실행에서 다시 시도합니다.")
    return False


# ── probe (E-1 외부 실측, 수동 실행 전용) ────────────────────
def probe_requested() -> bool:
    """workflow_dispatch inputs.mode=='probe' 일 때만 yml 이 PAGE76_PUBLISH_PROBE=true 를 넣는다.
    예약 실행(cron 값이 있음)에서는 env 와 무관하게 절대 probe 가 아니다."""
    return (os.getenv("PAGE76_PUBLISH_PROBE") or "").strip().lower() == "true" and not settings.SCHEDULE_CRON


def probe(log: RunLog, now: datetime) -> None:
    """DN2026_0119 E-1(DSN §12-1): 중립 카드 예약(T=now+29일) → 목록 확인 → 삭제 → 부재 확인.
    DB-12 읽기·쓰기 없음, 텔레그램은 삭제 실패 시만. Graph 쓰기는 MODE 와 무관(회로 차단은 지킨다)."""
    steps: list[str] = []
    failed = False
    t = int(now.timestamp()) + PROBE_LEAD_DAYS * 86400
    work = Path(tempfile.mkdtemp(prefix="p76p_"))
    graph.set_probe(True)
    try:
        try:
            image = card_render.render(PROBE_TEXT, PROBE_TEXT, "", str(work))
        except card_render.RenderError:
            log.fail("probe:render_failed")
            return
        res = graph.schedule_photo(image, PROBE_TEXT, t, now.timestamp())
        log.writes += 2 if res["status"] != "failed" else 1
        steps.append(f"register_{res['status']}")
        ok, posts, _ = graph.scheduled_posts()
        post_id = res["object_id"]
        if not post_id and res["status"] == "unknown" and ok:  # 미확정 → 같은 시각 예약을 찾아 지운다
            hits = [str(p["id"]) for p in posts if _at(p) == t]
            post_id = hits[0] if len(hits) == 1 else ""
        listed = ok and post_id in {str(p["id"]) for p in posts}
        steps.append("list_ok" if listed else ("list_missing" if ok else "list_failed"))
        if not post_id:
            failed = res["status"] != "failed"  # failed 는 예약된 것이 없다(남은 게시물 없음)
            if failed:
                notify_send("예약 점검 게시물 식별 실패 — 29일 뒤 게시 예정일 수 있음(페이지 예약 게시물 확인)", urgent=True)
            return
        dres = graph.delete_post(post_id)
        log.writes += 1
        steps.append("delete_ok" if dres["status"] == "ok" else f"delete_{dres['status']}")
        ok2, posts2, _ = graph.scheduled_posts()
        gone = ok2 and post_id not in {str(p["id"]) for p in posts2}
        steps.append("gone_ok" if gone else ("gone_still_listed" if ok2 else "gone_unread"))
        if dres["status"] != "ok" or not gone:
            failed = True
            notify_send("예약 점검 게시물 삭제 실패 — 29일 뒤 게시 예정", urgent=True)
    finally:
        graph.set_probe(False)
        shutil.rmtree(work, ignore_errors=True)
        if steps:
            code = "probe:" + ",".join(steps)
            (log.fail if failed else log.degrade)(code)
            logger.info(f"[NovelPub][probe] {code}")


# ── 진입점 ────────────────────────────────────────────────
def body(log: RunLog, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    if probe_requested():
        probe(log, now)  # NOVEL_* 스위치·DB-12 와 무관 (게시 전 실측용)
        return
    if not S.NOVEL_ENABLED or not S.NOVEL_PUBLISH_ENABLED:
        logger.info("[NovelPub] PAGE76_NOVEL_ENABLED / PAGE76_NOVEL_PUBLISH_ENABLED 꺼짐 — 종료")
        return
    if not S.DB12_ID:
        log.fail("novel:not_configured")
        return
    if S.NOVEL_PUBLISH_TIME_INVALID:
        log.degrade("publish_time_invalid")  # 형식 오류 → 기본 12:00
    # DN2026_0119: 예약 지연 가드(sched.too_late) 제거 — 게시 시각은 FB 예약이 지킨다. CONTROL 행은 제외
    rows = [p for p in novel_store.all_rows() if novel_store.kind(p) in (S.NK_BIBLE, *PUB_KINDS)]
    ctx = Ctx(log=log, now=now, rows=rows, ctrl_page={}, ctrl={})
    hh, mm = S.NOVEL_PUBLISH_TIME
    logger.info(f"[NovelPub] 행 {len(rows)}건 · mode={S.MODE} · 게시 {hh:02d}:{mm:02d} KST")
    sp = _Sched()
    try:
        reconcile(ctx, sp)
        cancel(ctx)
        blocked = reschedule(ctx)
        stops(ctx)
        confirm(ctx)
        if _live() and not S.BANNED_TERMS:
            # DN2026_0124 D4: 금칙어가 비면 원고 검사가 무력하다 — 대조·취소·확인만 하고 새 예약은 등록하지 않는다
            log.degrade("no_terms")
            logger.warning("[NovelPub] 금칙어(PAGE76_BANNED_TERMS)가 비어 있음 — 새 예약 등록 중단")
            _tg("T5", "⚠️ 금칙어 Secret(PAGE76_BANNED_TERMS)이 비어 있어 새 예약 등록을 멈췄습니다 — Secret 확인 필요")
        else:
            register(ctx, sp, blocked)
    except novel_prompts.PromptError as e:
        log.fail(str(e))
        _tg("T5", f"⚠️ 프롬프트 정본을 읽지 못함 — {e}")
    if safety.tripped():
        log.degrade("graph:tripped")


def main() -> int:
    cli.setup_logging()
    return cli.execute("novel_publish", VERSION, body)


if __name__ == "__main__":
    sys.exit(main())

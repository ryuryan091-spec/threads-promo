"""
p76/run_hiatus_publish.py
=========================
승인된 휴재 콘텐츠를 페이지에 FB 예약 게시로 등록한다 (FR-05, DSN §6-3 → DN2026_0119 DSN-P76-NOVEL-003 §6-5).
fb-page-publisher run_publish 의 상태 전이·결과 계약을 따른다. 게시 시각은 cron 이 아니라 FB 예약이 지킨다.

  1. 스위치: PAGE76_ENABLED · PAGE76_HIATUS_PUBLISH_ENABLED (모드는 아래 표)
  2. 게시 확인: '발행중'(= 예약 등록됨) + FB피드ID + 예약 시각 T 경과 → is_published=true → 발행완료 + 발행일시.
     T+30분까지 미게시 → 확인필요(정보 알림). 확인필요 + FB피드ID 는 매 실행 재조회 → 게시됐으면 발행완료.
     FB피드ID 없는 '발행중'(선점 후 중단된 실행) → 잔류 알림(기존 규칙)
  3. 예약 시각 T = 날짜 KST HIATUS_PUBLISH_TIME(20:05) + 결정적 지터 0~HIATUS_JITTER_MAX_MIN분
     (캘린더키 해시 — 재실행해도 같은 값. 기존 워크플로 무작위 대기를 대신한다)
  4. 등록 대상: 구분=휴재콘텐츠 · 게시상태=승인 · 검수완료 · FB피드ID 없음 ·
     NOVEL_SCHEDULE_MIN_LEAD_MIN ≤ T−now ≤ NOVEL_REGISTER_AHEAD_HOURS.
     같은 날짜에 이미 발행중·발행완료·부분완료·확인필요 행이 HIATUS_DAILY_CAP(1)건 이상이면 등록하지 않음
     (SEC C-22 — 날짜 범위 오늘−1~+2 질의로 판정, 리뷰 W6 c·d).
     T−now < 15분인 미등록 '승인' 행은 게시하지 않고 1회 '실패'(결과코드 missed_slot)로 정리 — 영구 잔류 방지.
     ※ v1 의 '늦게라도 게시' 동작을 바꾼 것이다(리뷰 W6 b). 다시 올리려면 날짜를 고쳐 '승인'으로 되돌린다
  ※ 조회는 날짜 하한(오늘−1일 KST)·날짜 오름차순·page_size 100 (리뷰 W6 a). 그보다 오래된 행은 보지 않는다
  ※ 중단 경로: DB-09 에 중단 속성이 없어 예약 취소는 이번 범위 밖이다(리뷰 W6, 등록 후 멈추려면 페이지에서 삭제)
  5. 승인 → 발행중 선점 후 재조회 확인 → 본문 hiatus_guard → 이미지(첨부 또는 카드 렌더, 4:5 · 10MB)
     → graph.schedule_photo(2단계) — 쓰기 재시도 없음
  6. ok → 발행중 + FB피드ID + 결과코드 scheduled:live · failed → 실패 · unknown → 확인필요. 임시 파일 삭제, 알림 1줄

  모드     | 선점·결과 기록 | 예약 등록
  dry_run | 없음          | 없음 (검사·렌더까지만)
  shadow  | 없음          | 없음 (검사·렌더까지 + 실행 기록)
  live    | 함            | 함

사용: python -m p76.run_hiatus_publish
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import sys
import tempfile
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import requests

from p76 import card_render, cli, graph, hiatus_guard, notion, safety, settings
from p76.notify import send
from p76.redact import redact
from p76.runlog import RunLog

VERSION = "2.1.0"  # 2.1.0: 리뷰 W6 조회 날짜 하한·정렬 · missed_slot 실패 정리 · 날짜 범위 하루 상한(HIATUS_DAILY_CAP)
# 2.0.0: DN2026_0119 FB 예약 게시 전환 · 결정적 지터 · 지연 보류(MAX_LAG) 제거 · 게시 확인

logger = logging.getLogger(__name__)
DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
CONFIRM_GRACE = timedelta(minutes=30)
SLOT_STATES = (settings.S_RUNNING, settings.S_DONE, settings.S_PARTIAL, settings.S_CHECK)
LOOKBACK_DAYS = 1    # 조회 날짜 하한 = 오늘(KST) − 1일 (리뷰 W6 a)
TAKEN_AHEAD_DAYS = 2  # 하루 상한 판정 범위 = 오늘−1 ~ 오늘+2 (등록 창 36시간을 덮는다)
PAGE_SIZE = 100


def _today(now: datetime) -> date:
    return now.astimezone(notion.KST).date()


def _query(conds: list[dict], since: date, until: date | None = None) -> list[dict]:
    conds = [{"property": settings.C_KIND, "select": {"equals": settings.KIND_HIATUS_CONTENT}}, *conds,
             {"property": settings.C_DATE, "date": {"on_or_after": since.isoformat()}}]
    if until is not None:
        conds.append({"property": settings.C_DATE, "date": {"on_or_before": until.isoformat()}})
    body = {"filter": {"and": conds}, "sorts": [{"property": settings.C_DATE, "direction": "ascending"}],
            "page_size": PAGE_SIZE}
    return list(notion.query(settings.DB09_ID, body, max_pages=1))


def _rows(state: str, now: datetime, until: date | None = None) -> list[dict]:
    """상태별 조회 — 날짜 ≥ 오늘−1일(KST), 날짜 오름차순."""
    since = _today(now) - timedelta(days=LOOKBACK_DAYS)
    return _query([{"property": settings.C_STATE, "select": {"equals": state}}], since, until)


def jitter_min(page: dict) -> int:
    """캘린더키(없으면 날짜) 해시 → 0~HIATUS_JITTER_MAX_MIN 분. 같은 행이면 언제 실행해도 같은 값."""
    seed = notion.plain(page, settings.C_KEY) or label_of(page)
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % (settings.HIATUS_JITTER_MAX_MIN + 1)


def slot_time(page: dict) -> datetime | None:
    day = notion.kst_date(page, settings.C_DATE)
    if day is None:
        return None
    hh, mm = settings.HIATUS_PUBLISH_TIME
    return datetime.combine(day, time(hh, mm), tzinfo=notion.KST) + timedelta(minutes=jitter_min(page))


def label_of(page: dict) -> str:
    day = notion.kst_date(page, settings.C_DATE)
    return day.isoformat() if day else "????-??-??"


def _hm(dt: datetime) -> str:
    return dt.astimezone(notion.KST).strftime("%m-%d %H:%M")


def taken_days(now: datetime) -> Counter:
    """날짜별 차지 건수 — 날짜 범위(오늘−1 ~ 오늘+2) 한 번의 질의(리뷰 W6 c).
    늦게 게시된 기존 행은 발행일시 날짜도 차지한다(같은 행이 두 날짜를 차지할 수 있다)."""
    today = _today(now)
    states = {"or": [{"property": settings.C_STATE, "select": {"equals": s}} for s in SLOT_STATES]}
    days: Counter = Counter()
    for p in _query([states], today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=TAKEN_AHEAD_DAYS)):
        for day in {notion.kst_date(p, n) for n in (settings.C_DATE, settings.C_PUBLISHED_AT)} - {None}:
            days[day] += 1
    return days


def _full(days: Counter, day: date) -> bool:
    return days[day] >= settings.HIATUS_DAILY_CAP  # SEC C-22 (리뷰 W6 d)


def claim(page: dict) -> bool:
    if notion.select(notion.retrieve_page(page["id"]), settings.C_STATE) != settings.S_APPROVED:
        return False
    notion.update_page(page["id"], {settings.C_STATE: notion.v_select(settings.S_RUNNING)})
    return notion.select(notion.retrieve_page(page["id"]), settings.C_STATE) == settings.S_RUNNING


def finalize(page: dict, state: str, code: str, published_at: datetime | None = None, feed_id: str = "") -> None:
    props = {
        settings.C_STATE: notion.v_select(state),
        settings.C_RESULT: notion.v_rich(redact(code, include_ids=False)[:1900]),
    }
    if published_at is not None:
        props[settings.C_PUBLISHED_AT] = notion.v_date(published_at)
    if feed_id:
        props[settings.C_FEED_ID] = notion.v_rich(feed_id)
    notion.update_page(page["id"], props)


def download(url: str, dest: Path) -> str:
    if not url:
        raise card_render.RenderError("image:empty_url")
    try:
        with requests.get(url, stream=True, timeout=settings.UPLOAD_TIMEOUT_SEC) as resp:
            if resp.status_code != 200:
                raise card_render.RenderError(f"image:http_{resp.status_code}")
            total = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(1024 * 1024):
                    total += len(chunk)
                    if total > DOWNLOAD_MAX_BYTES:
                        raise card_render.RenderError("image:too_large")
                    fh.write(chunk)
    except requests.exceptions.RequestException as e:
        raise card_render.RenderError(f"image:download_{type(e).__name__}") from None
    if total == 0 or total > settings.PHOTO_MAX_BYTES:
        raise card_render.RenderError("image:size")
    return str(dest)


def prepare_image(page: dict, text: str, work: Path) -> str:
    """첨부 우선(파일 URL 은 1시간 유효 → 조회 직후 다운로드). 없으면 카드 렌더."""
    attached = notion.files(page, settings.C_IMAGE)
    if attached:
        path = download(notion.file_url(attached[0]), work / "image.bin")
        card_render.check_attachment(path)
        return path
    content = notion.select(page, settings.C_CONTENT)
    day = notion.number(page, settings.C_HIATUS_DAY)
    season = notion.number(page, settings.C_SEASON)
    label = f"휴재 {day}일차 · {content}" if day else content
    meta = f"시즌 {season} 휴재 주간" if season else "휴재 주간"
    return card_render.render(label, hiatus_guard.headline(text), meta, str(work))


# ── 게시 확인 ─────────────────────────────────────────────
def _created(body: dict, fallback: datetime) -> datetime:
    try:
        dt = datetime.fromisoformat(str(body.get("created_time") or "").replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def confirm(log: RunLog, now: datetime) -> None:
    live = settings.MODE == "live"
    stuck = []
    for state in (settings.S_RUNNING, settings.S_CHECK):
        for page in _rows(state, now):
            feed_id, t, label = notion.plain(page, settings.C_FEED_ID), slot_time(page), label_of(page)
            if not feed_id:
                if state == settings.S_RUNNING:
                    stuck.append(label)
                continue
            if t is None or t > now:
                continue
            ok, body, reason = graph.get_post(feed_id)
            if ok and body.get("is_published") is True:
                if live:
                    finalize(page, settings.S_DONE, "published", _created(body, t))
                    log.writes += 1
                    send(f"휴재 콘텐츠 {label} 발행완료")
                logger.info(f"[Hiatus] {label} 게시 확인 → 발행완료")
                continue
            if not ok:
                log.degrade("confirm:read_failed")
                logger.warning(f"[Hiatus] {label} 게시 확인 조회 실패 {reason}")
            if state == settings.S_RUNNING and now > t + CONFIRM_GRACE:
                log.degrade("unconfirmed")
                if live:
                    finalize(page, settings.S_CHECK, "unconfirmed:not_published" if ok else "unconfirmed:read_failed")
                    log.writes += 1
                    send(f"휴재 콘텐츠 {label} {_hm(t)} 예약 게시 미확인 — 확인필요로 두고 매 실행 다시 조회합니다")
    if stuck:
        log.degrade("stuck_rows")
        send(f"휴재 게시 발행중 잔류 {len(stuck)}건 ({', '.join(stuck)}) — 페이지 확인 후 상태 정리")


# ── 예약 등록 ─────────────────────────────────────────────
def candidates(now: datetime) -> list[tuple[datetime, dict]]:
    horizon = (now + timedelta(hours=settings.NOVEL_REGISTER_AHEAD_HOURS)).astimezone(notion.KST).date()
    out = []
    for page in _rows(settings.S_APPROVED, now, until=horizon):
        t = slot_time(page)
        if t is not None and notion.checkbox(page, settings.C_REVIEWED):
            out.append((t, page))
    out.sort(key=lambda x: x[0])
    return out


def body(log: RunLog, now: datetime | None = None, banned_terms: tuple[str, ...] | None = None) -> None:
    if not settings.HIATUS_PUBLISH_ENABLED:
        logger.info("[Hiatus] PAGE76_HIATUS_PUBLISH_ENABLED=false — 종료")
        return
    now = now or datetime.now(UTC)
    # DN2026_0119: 금칙어 Secret 을 기본으로 쓴다(이전에는 호출자가 넘길 때만 검사)
    terms = tuple(settings.BANNED_TERMS) if banned_terms is None else banned_terms
    live = settings.MODE == "live"
    # DN2026_0119 §6-5: 지연 보류(sched.too_late · MAX_LAG_MIN) 제거 — 게시 시각은 FB 예약이 지킨다
    confirm(log, now)
    taken = taken_days(now)
    lo = timedelta(minutes=settings.NOVEL_SCHEDULE_MIN_LEAD_MIN)
    hi = timedelta(hours=settings.NOVEL_REGISTER_AHEAD_HOURS)
    for t, page in candidates(now):
        label, lead = label_of(page), t - now
        if lead > hi:
            continue
        if live and notion.plain(page, settings.C_FEED_ID):
            finalize(page, settings.S_DONE, "already_posted")
            log.writes += 1
            log.degrade("already_posted")
            taken[t.date()] += 1
            continue
        if lead < lo:
            # 리뷰 W6 b: 늦은 게시 없음 + 영구 잔류 방지 → 1회 '실패'(missed_slot). v1 은 늦게라도 게시했다
            log.degrade("missed_slot")
            logger.warning(f"[Hiatus] {label} 예약 시각까지 {settings.NOVEL_SCHEDULE_MIN_LEAD_MIN}분 미만 — 게시하지 않음")
            if live:
                finalize(page, settings.S_FAILED, "missed_slot")
                log.writes += 1
            continue
        if _full(taken, t.date()):
            logger.info(f"[Hiatus] {label} 같은 날 이미 휴재 콘텐츠 예약·발행 — 등록하지 않음(하루 {settings.HIATUS_DAILY_CAP}건)")
            continue
        if _register(log, page, label, t, now, terms, live):
            taken[t.date()] += 1
        if safety.tripped():
            return


def _register(log: RunLog, page: dict, label: str, t: datetime, now: datetime, terms: tuple[str, ...],
              live: bool) -> bool:
    """True = 그 날짜를 차지했다(예약됨·미확정·non-live 판정 통과)."""
    logger.info(f"[Hiatus] 대상 {label} 예약 {_hm(t)} mode={settings.MODE}")
    if live:
        if not safety.graph_write_allowed():
            log.degrade("graph_write_blocked")
            return False
        if not claim(page):
            log.degrade("claim_lost")
            logger.warning(f"[Hiatus] {label} 선점 실패 — 다른 실행이 처리 중")
            return False
        log.writes += 1
    work = Path(tempfile.mkdtemp(prefix="p76_"))
    try:
        text = notion.full_text(page, settings.C_BODY)
        errors = hiatus_guard.check(text, notion.select(page, settings.C_CONTENT), terms)
        if errors:
            _fail(log, page, label, live, "guard:" + ",".join(errors))
            return False
        try:
            image = prepare_image(page, text, work)
        except card_render.RenderError as e:
            _fail(log, page, label, live, str(e))
            return False
        if not live:
            logger.info(f"[Hiatus][{settings.MODE}] {label} 검사·이미지 통과 — 예약 생략 (본문 {len(text)}자)")
            return True
        try:
            res = graph.schedule_photo(image, text, int(t.timestamp()), now.timestamp())
        except safety.Tripped:  # 요청 전 차단(미등록 확정)
            finalize(page, settings.S_FAILED, "failed:tripped")
            log.writes += 1
            raise
        return _record(log, page, label, res, t)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fail(log: RunLog, page: dict, label: str, live: bool, code: str) -> None:
    log.fail(code)
    logger.error(f"[Hiatus] {label} 게시 전 실패: {code}")
    if live:
        finalize(page, settings.S_FAILED, code)
        log.writes += 1
    send(f"휴재 콘텐츠 {label} 게시 전 실패 — {code}")


def _record(log: RunLog, page: dict, label: str, res: dict, t: datetime) -> bool:
    status, reason = res["status"], res.get("reason", "")
    if status == "ok":
        log.writes += 1
        finalize(page, settings.S_RUNNING, f"scheduled:{settings.MODE}", feed_id=res["object_id"])
        log.writes += 1
        ok, posts, _ = graph.scheduled_posts()
        if not ok or res["object_id"] not in {str(p.get("id")) for p in posts}:
            log.degrade("schedule_unverified")
        send(f"휴재 콘텐츠 {label} {_hm(t)} 게시 예약됨")
        return True
    if status == "unknown":
        log.fail(f"unknown:{reason}")
        finalize(page, settings.S_CHECK, f"unknown:{reason}")
        log.writes += 1
        send(f"휴재 콘텐츠 {label} 확인필요 — 예약 응답 미확정({reason}). 페이지 예약 게시물 확인", urgent=True)
        return True
    log.fail(f"failed:{reason}")
    finalize(page, settings.S_FAILED, f"failed:{reason}")
    log.writes += 1
    send(f"휴재 콘텐츠 {label} 실패 — {reason}", urgent=safety.tripped() != "")
    return False


def main() -> int:
    cli.setup_logging()
    return cli.execute("hiatus_publish", VERSION, body)


if __name__ == "__main__":
    sys.exit(main())

"""
p76/run_hiatus_publish.py
=========================
승인된 휴재 콘텐츠 1건을 페이지에 사진 게시한다 (FR-05, DSN §6-3).
fb-page-publisher run_publish 의 상태 전이·결과 계약을 따른다.

  1. 스위치: PAGE76_ENABLED · PAGE76_HIATUS_PUBLISH_ENABLED (모드는 아래 표)
  2. DB-09: 구분=휴재콘텐츠 AND 게시상태=승인 AND 검수완료 AND 날짜 ≤ 오늘 → 가장 이른 1건
     오늘 이미 발행완료 행이 있으면 게시하지 않음 (하루 1건, SEC C-22) · 발행중 잔류 행은 알림
  3. 승인 → 발행중 선점 후 재조회 확인 (다른 실행이 먼저 바꿨으면 중단)
  4. 본문 hiatus_guard → 이미지(첨부 다운로드 또는 카드 렌더, 4:5 · 10MB)
  5. FB피드ID 가 비어 있을 때만 POST /{page-id}/photos — 쓰기 재시도 없음
  6. ok → 발행완료 · failed → 실패 · unknown → 확인필요. 임시 파일 삭제, 알림 1줄

  모드     | 선점·결과 기록 | 게시
  dry_run | 없음          | 없음 (검사·렌더까지만)
  shadow  | 없음          | 없음 (검사·렌더까지 + 실행 기록)
  live    | 함            | 함

사용: python -m p76.run_hiatus_publish
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import requests

from p76 import card_render, cli, graph, hiatus_guard, notion, safety, settings
from p76.notify import send
from p76.redact import redact
from p76.runlog import RunLog

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024


def _rows(state: str, until: date | None = None) -> list[dict]:
    conds = [
        {"property": settings.C_KIND, "select": {"equals": settings.KIND_HIATUS_CONTENT}},
        {"property": settings.C_STATE, "select": {"equals": state}},
    ]
    if until is not None:
        conds.append({"property": settings.C_DATE, "date": {"on_or_before": until.isoformat()}})
    body = {"filter": {"and": conds}, "sorts": [{"property": settings.C_DATE, "direction": "ascending"}],
            "page_size": 20}
    return list(notion.query(settings.DB09_ID, body, max_pages=1))


def find_ready(today: date) -> dict | None:
    for page in _rows(settings.S_APPROVED, until=today):
        if notion.checkbox(page, settings.C_REVIEWED):
            return page
    return None


def published_today(today: date) -> int:
    return sum(1 for p in _rows(settings.S_DONE) if notion.kst_date(p, settings.C_PUBLISHED_AT) == today)


def label_of(page: dict) -> str:
    day = notion.kst_date(page, settings.C_DATE)
    return day.isoformat() if day else "????-??-??"


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


def body(log: RunLog, now: datetime | None = None, banned_terms: tuple[str, ...] = ()) -> None:
    if not settings.HIATUS_PUBLISH_ENABLED:
        logger.info("[Hiatus] PAGE76_HIATUS_PUBLISH_ENABLED=false — 종료")
        return
    now = now or datetime.now(UTC)
    day0 = now.astimezone(notion.KST).date()
    live = settings.MODE == "live"

    stuck = _rows(settings.S_RUNNING)
    if stuck:
        log.degrade("stuck_rows")
        send(f"휴재 게시 발행중 잔류 {len(stuck)}건 ({', '.join(label_of(p) for p in stuck)}) — 페이지 확인 후 상태 정리")
    if published_today(day0) >= settings.HIATUS_DAILY_CAP:
        logger.info("[Hiatus] 오늘 이미 휴재 콘텐츠 발행 — 종료(하루 1건)")
        return
    page = find_ready(day0)
    if page is None:
        logger.info("[Hiatus] 게시 대상 없음")
        return
    label = label_of(page)
    logger.info(f"[Hiatus] 대상 {label} mode={settings.MODE}")

    if live:
        if not claim(page):
            log.degrade("claim_lost")
            logger.warning(f"[Hiatus] {label} 선점 실패 — 다른 실행이 처리 중")
            return
        log.writes += 1

    work = Path(tempfile.mkdtemp(prefix="p76_"))
    try:
        text = notion.full_text(page, settings.C_BODY)
        errors = hiatus_guard.check(text, notion.select(page, settings.C_CONTENT), banned_terms)
        if errors:
            _fail(log, page, label, live, "guard:" + ",".join(errors))
            return
        try:
            image = prepare_image(page, text, work)
        except card_render.RenderError as e:
            _fail(log, page, label, live, str(e))
            return
        if not live:
            logger.info(f"[Hiatus][{settings.MODE}] {label} 검사·이미지 통과 — 게시 생략 (본문 {len(text)}자)")
            return
        existing = notion.plain(page, settings.C_FEED_ID)
        if existing:
            finalize(page, settings.S_DONE, "already_posted")
            log.writes += 1
            log.degrade("already_posted")
            return
        res = graph.post_photo(image, text)
        _record(log, page, label, res, now)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fail(log: RunLog, page: dict, label: str, live: bool, code: str) -> None:
    log.fail(code)
    logger.error(f"[Hiatus] {label} 게시 전 실패: {code}")
    if live:
        finalize(page, settings.S_FAILED, code)
        log.writes += 1
    send(f"휴재 콘텐츠 {label} 게시 전 실패 — {code}")


def _record(log: RunLog, page: dict, label: str, res: dict, now: datetime) -> None:
    status, reason = res["status"], res.get("reason", "")
    if status == "ok":
        log.writes += 1
        finalize(page, settings.S_DONE, "ok", now, res["object_id"])
        log.writes += 1
        send(f"휴재 콘텐츠 {label} 발행완료")
        return
    if status == "unknown":
        log.fail(f"unknown:{reason}")
        finalize(page, settings.S_CHECK, f"unknown:{reason}")
        log.writes += 1
        send(f"휴재 콘텐츠 {label} 확인필요 — 응답 미확정({reason}). 페이지에서 게시 여부 확인", urgent=True)
        return
    log.fail(f"failed:{reason}")
    finalize(page, settings.S_FAILED, f"failed:{reason}")
    log.writes += 1
    send(f"휴재 콘텐츠 {label} 실패 — {reason}", urgent=safety.tripped() != "")


def main() -> int:
    cli.setup_logging()
    return cli.execute("hiatus_publish", VERSION, body)


if __name__ == "__main__":
    sys.exit(main())

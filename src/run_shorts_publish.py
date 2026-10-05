"""숏폼 publish job (v1.7.0) — 마스터 승인(environment) 뒤에만 실행된다.

흐름
  1) build artifact 의 manifest.json 읽기 → 오늘(KST) 콘텐츠만 남김(신선도)
  2) 게시 시각 계산(shorts_plan.publish_schedule): 첫 편 지연 · 편간 간격 (DN2026_0004: 둘 다 1~10분, 수동 즉시는 첫 편 0)
     DN2026_0002 : 채널별 시간대 — Facebook 06:06~22시, Threads 10~22시. 채널별로 따로 계산해 시각순 병합.
  3) 편마다 대기 → Facebook 릴스(중복 설명이면 건너뜀) → (첫 편만) Threads 동영상
     Threads 는 게시 직전에 킬 스위치·워밍업·하루 총량(DAILY_POST_BUDGET)을 다시 본다.
  4) 결과 알림 + 'AI 정보 표시는 앱에서' 안내

v1.8.0 (FACE_STORY_ENABLED) — Facebook 회차 원장(Notion, 상세설계 v1.0)
  0) 시작 시 재조정: 원장 '확인필요' 행을 Facebook 상태로 다시 확인 → 게시완료/실패
  3-1) Facebook 결과마다 원장 upsert(회차ID 1행): 게시완료 / 확인필요(처리 확인 시간 초과) / 실패
  3-2) 루프 끝에 이번 실행의 확인필요를 한 번 더 확인
  원장 실패는 게시 결과·종료코드를 바꾸지 않는다(재게시 없음). 알림에 수동 입력용 값을 싣는다.

DRY_RUN 이면 대기·쓰기 없이 계획만 로그로 남긴다.
종료코드: 0 정상 · 4 일부 실패 · 7 계정·토큰 사용 불가(회로 차단)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, face_story, media_host, notifier, safety, shorts_plan
from .face_client import FaceApiError, FaceClient
from .threads_client import ContainerNotReadyError, ThreadsApiError, ThreadsClient, fetch_user_id

VERSION = "1.2.0"   # v1.8.6: 원장 기반 재게시 방지·세션 전 치명 오류 실패 기록 · v1.8.0: Facebook 회차 원장 기록 · 재조정

KST = ZoneInfo("Asia/Seoul")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("shorts-publish")
for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

FAIL_EXIT_CODE = 4
RECHECK_SLACK_MIN = 20     # 루프 끝 재확인을 하는 마지노선: job 예산(300분) + 20분 (timeout 350분)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _dry_run() -> bool:
    return _env("DRY_RUN").lower() not in ("false", "0", "no")


def _notify(message: str) -> None:
    notifier.send(_env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_ALERT_CHAT_ID"), message)


# ── DN2026_0003 : 발행 거래 분리 ─────────────────────────────────────────────
#   Facebook 게시는 별도 워크플로(face_publish.yml), Threads 동영상은 shorts.yml publish job 이 맡는다.
#   같은 러너 코드를 쓰되 아래 3개 환경변수로 범위를 정한다.
#     SHORTS_PUBLISH_CHANNELS : 게시할 채널 (face / threads / face,threads — 미설정 시 둘 다, 하위 호환)
#     SHORTS_TARGET_DATE      : 게시할 회차 날짜 YYYY-MM-DD(KST). 미설정 시 오늘. 미게시분 수동 재게시용.
#     SHORTS_IGNORE_WINDOW    : true 면 채널 시간대를 무시하고 지금부터 게시(마스터 수동 실행 전용).
_ANY_TIME_WINDOW = ("00:00", "23:59")


def _publish_channels() -> set[str]:
    raw = _env("SHORTS_PUBLISH_CHANNELS")
    allowed = {shorts_plan.CHANNEL_FACE, shorts_plan.CHANNEL_THREADS}
    if not raw:
        return allowed
    chosen = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = chosen - allowed
    if unknown:
        raise ValueError(f"SHORTS_PUBLISH_CHANNELS 알 수 없는 채널: {sorted(unknown)}")
    return chosen


def _target_date(now: dt.datetime) -> dt.date:
    raw = _env("SHORTS_TARGET_DATE")
    if not raw:
        return now.date()
    try:
        target = dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"SHORTS_TARGET_DATE 형식 오류(YYYY-MM-DD): {raw}") from exc
    if target > now.date():
        raise ValueError(f"SHORTS_TARGET_DATE 가 미래 날짜입니다: {raw}")
    if target != now.date():
        log.info("지정 회차 날짜로 게시: %s (오늘 %s)", target, now.date())
    return target


def _ignore_window() -> bool:
    return _env("SHORTS_IGNORE_WINDOW").lower() in ("true", "1", "yes")


def load_manifest(base: Path) -> dict:
    path = base / "manifest.json"
    if not path.exists():
        return {"items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def fresh_items(manifest: dict, today: dt.date) -> list[dict]:
    """오늘(KST) content_id 만. 늦은 승인으로 지난 날 영상이 나가는 것을 막는다."""
    out = []
    for item in manifest.get("items") or []:
        if shorts_plan.content_date(str(item.get("content_id"))) == today:
            out.append(item)
        else:
            log.warning("신선도 불일치로 제외 %s (오늘 %s)", item.get("content_id"), today)
    return out


def _threads_client() -> ThreadsClient:
    token = _env("THREADS_LONG_LIVED_TOKEN")
    if not token:
        raise RuntimeError("THREADS_LONG_LIVED_TOKEN 없음")
    user_id = _env("THREADS_USER_ID")
    if not user_id or user_id == "me":
        user_id, _ = fetch_user_id(token)
    return ThreadsClient(user_id, token)


def _sleep_until(target: dt.datetime) -> None:
    wait = (target - dt.datetime.now(KST)).total_seconds()
    if wait > 0:
        log.info("다음 게시까지 %d초 대기 (%s KST)", int(wait), target.strftime("%H:%M"))
        time.sleep(wait)


@dataclass(frozen=True)
class FaceOutcome:
    """Facebook 1편 결과. status 는 원장 상태값(face_story.STATUS_*)."""

    status: str
    video_id: str
    message: str


def _status_of(face: FaceClient, video_id: str) -> str | None:
    """Facebook 처리 상태 → 원장 상태. 아직 진행 중이면 None. 계정 치명 오류는 그대로 올린다."""
    st = face.status(video_id)
    if st.failed:
        return face_story.STATUS_FAILED
    if st.published:
        return face_story.STATUS_PUBLISHED
    return None


def face_outcome(client: FaceClient, item: dict, base: Path, *, with_ids: bool = False,
                 on_session=None) -> FaceOutcome:
    """Facebook 릴스 1편. 처리 실패는 FaceApiError(definitive=True).

    with_ids: v1.8.0 원장 기록용. 같은 설명의 릴스가 이미 있으면 그 id 와 처리 상태를 함께 돌려준다
              (리뷰 #3-F1: 처리 확인 없이 게시완료로 기록하지 않는다). False 면 v1.7.0 과 같은 요청.
    """
    caption = str(item["caption"])
    cid = item["content_id"]
    if with_ids:
        same = [rid for rid, desc in client.recent_reels() if desc.strip() == caption.strip()]
        for rid in same:
            note = ""
            try:
                state = (_status_of(client, rid) if rid else None) or face_story.STATUS_PENDING
            except FaceApiError as exc:
                # 리뷰 2차 QC-N1·CR-D: 건너뛴 편의 상태 확인 실패는 게시 실패가 아니다(v1.7.0 과 같이 '건너뜀').
                if safety.is_account_fatal(exc):
                    raise
                state, note = face_story.STATUS_PENDING, " · 상태 확인 실패"
            if state == face_story.STATUS_FAILED:
                # 리뷰 v1.8.6 U1: Facebook 이 처리 실패를 확정한 영상은 공개되지 않았다 → 중복으로 보지 않는다.
                log.info("같은 설명의 릴스 %s 는 처리 실패 확정 — 중복으로 보지 않음", rid)
                continue
            return FaceOutcome(state, rid,
                               f"FB {cid}: 같은 설명의 릴스가 이미 있어 건너뜀 (video_id={rid or '-'} {state}{note})")
    elif caption.strip() in {d.strip() for d in client.recent_descriptions()}:
        return FaceOutcome(face_story.STATUS_PUBLISHED, "", f"FB {cid}: 같은 설명의 릴스가 이미 있어 건너뜀")
    if on_session is None:
        video_id, status = client.publish_reel(base / item["video"], caption)
    else:
        video_id, status = client.publish_reel(base / item["video"], caption, on_session=on_session)
    if status is None:
        return FaceOutcome(face_story.STATUS_PENDING, video_id,
                           f"FB {cid}: video_id={video_id} 게시 요청 완료 · 처리 상태 확인 필요")
    if status.failed:
        exc = FaceApiError(200, f"릴스 처리 실패 video_id={video_id}: {status.error or status}")
        exc.video_id, exc.definitive = video_id, True
        raise exc
    return FaceOutcome(face_story.STATUS_PUBLISHED, video_id, f"FB {cid}: video_id={video_id} 게시 완료")


def post_face(client: FaceClient, item: dict, base: Path) -> str:
    """Facebook 릴스 1편. 반환은 결과 요약 문자열(v1.7.0 호환)."""
    return face_outcome(client, item, base).message


def post_threads(client: ThreadsClient, item: dict, base: Path) -> str:
    """Threads 동영상 1편. 게시 직전 안전 판정을 다시 한다."""
    today = dt.datetime.now(KST).date()
    reason = safety.block_reason(safety.KIND_SHORTS, today)
    if reason or not config.SHORTS_THREADS_ENABLED:
        return f"Threads {item['content_id']}: 건너뜀 — {reason or 'SHORTS_THREADS_ENABLED=false'}"
    posts = client.get_my_posts(safety.BUDGET_SCAN_POSTS, since=today - dt.timedelta(days=1))
    over = safety.budget_block(safety.KIND_SHORTS, posts, dt.datetime.now(dt.UTC))
    if over:
        return f"Threads {item['content_id']}: 건너뜀 — {over}"
    caption = str(item["caption"])
    if caption.strip() in {str(p.get("text") or "").strip() for p in posts}:
        return f"Threads {item['content_id']}: 같은 글이 이미 있어 건너뜀"
    url = media_host.publish_file(base / item["video"], f"{item['content_id']}.mp4")
    media_host.verify(url)
    post_id = client.publish_video_post(url, caption)
    return f"Threads {item['content_id']}: post_id={post_id} 게시 완료"


def failure_status(exc: FaceApiError) -> str:
    """게시 오류 → 원장 상태. 리뷰 #2-3: 결과를 모르는 오류를 '실패'로 확정하지 않는다.

    - Facebook 이 처리 실패를 확정(definitive) → 실패
    - 업로드 세션을 연 뒤의 오류(video_id 있음 — finish 이후 오류 포함) → 확인필요
    - 세션을 열기 전 오류(중복 확인 조회·start 실패 — 4xx·네트워크 모두) → 실패. finish 를 부르지 않았으므로
      게시물이 생길 수 없다(리뷰 3차 OPS-R3-1).
    """
    if exc.definitive:
        return face_story.STATUS_FAILED
    if exc.video_id:
        return face_story.STATUS_PENDING
    return face_story.STATUS_FAILED


# ---------------------------------------------------------------------------
# v1.8.0 Facebook 회차 원장
# ---------------------------------------------------------------------------


def _manual_values(item: dict, status: str, video_id: str) -> str:
    c = item.get("continuity") or {}
    return (f"{item.get('content_id')} 상태={status} FB영상ID={video_id or '-'} "
            f"캡션해시={face_story.caption_hash(str(item.get('caption') or ''))} "
            f"시리즈회차={c.get('series_no')} 떡밥={c.get('thread_state', '-')}:{c.get('thread_text', '')} "
            f"요약={c.get('summary', '')}")


class LedgerRun:
    """이번 실행의 원장 기록. 어떤 예외도 밖으로 내지 않는다(게시 결과·종료코드 불변 — 리뷰 #1-D1·#3-F2).

    회차별 (page_id, 현재 상태)를 기억해 두 번째 기록부터는 재조회 없이 갱신한다(리뷰 #3-F6).
    """

    def __init__(self, ledger: face_story.Ledger, notes: list[str]):
        self.ledger = ledger
        self.notes = notes
        self._pages: dict[str, tuple[str, str]] = {}

    def record(self, item: dict, status: str, *, video_id: str = "", error: str = "",
               quiet: bool = False) -> None:
        """quiet: 선기록·세션 기록처럼 뒤에 결과 기록이 이어지는 경우 — 실패해도 알림 대신 로그만(리뷰 2차 OPS-N5)."""
        cid = str(item.get("content_id") or "")
        page_id, current = self._pages.get(cid, ("", ""))
        try:
            page_id, after = self.ledger.upsert(
                face_story.record_from_item(item, status, video_id=video_id, error=error),
                page_id=page_id, current_status=current)
            self._pages[cid] = (page_id, after)
            log.info("원장 기록 %s → %s", cid, after)
        except Exception as exc:  # noqa: BLE001 — 원장 장애 격리
            log.warning("원장 기록 실패 %s: %s", cid, exc)
            if quiet:
                return
            self.notes.append(f"원장 기록 실패(수동 입력 필요) {_manual_values(item, status, video_id)} — {exc}")

    def prior(self, item: dict) -> tuple[str, str] | None:
        """v1.8.6 게시 직전 원장 조회 → (상태, FB영상ID). 행이 없거나 조회 실패면 None(원장 장애가 게시를 막지 않는다).

        조회한 page_id·상태는 캐시해 이후 기록에서 다시 조회하지 않는다.
        """
        cid = str(item.get("content_id") or "")
        try:
            entry = self.ledger.find_entry(cid)
            if entry is None:
                return None
            page_id, status, video_id = (str(x or "") for x in entry)
        except Exception as exc:  # noqa: BLE001 — 원장 장애·응답 형식 오류 격리
            log.warning("원장 사전 조회 실패 %s: %s", cid, exc)
            self.notes.append(f"원장 사전 조회 실패 {cid} — 원장 재게시 방지 없이 진행(Facebook 중복 확인은 동작) — {exc}")
            return None
        self._pages[cid] = (page_id, status)
        return status, video_id

    def reconcile(self, face: FaceClient, today: dt.date) -> None:
        """원장 '확인필요' 행을 Facebook 상태로 다시 확인한다. 계정 치명 오류만 그대로 올린다.

        - FB영상ID 가 없는 행(업로드 세션 전 끊긴 실행)은 최근 릴스의 캡션해시로 찾는다.
        - 확인하지 못한 행은 회차 날짜로부터 RECONCILE_GIVEUP_DAYS 가 지나면 '실패(확인 불가)'로 닫는다
          (리뷰 2차 OPS-N1: 한 번 못 찾았다고 바로 닫지 않는다 · CR-C: 못 닫는 행이 재조정 자리를 막지 않게).
        - 확인필요 → 실패로 닫히면 시리즈회차 결번이 생길 수 있어 알림에 남긴다(리뷰 2차 CR-A).
        """
        try:
            rows = self.ledger.pending()
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"원장 재조정 조회 실패 — {exc}")
            return
        reels: list[tuple[str, str]] | None = None
        for page_id, cid, video_id, chash in rows:
            day = face_story.content_date(cid)
            if day is None:
                # 리뷰 3차 OPS-R3-2: 회차ID 형식이 틀린 행(사람 입력)은 기한 계산이 안 되므로 바로 닫는다.
                try:
                    self.ledger.set_status(page_id, cid, face_story.STATUS_FAILED, "회차ID 형식 오류(sv-YYYYMMDD-N)")
                    self.notes.append(f"원장 재조정 {cid or '-'} → 실패(회차ID 형식 오류)")
                except Exception as exc:  # noqa: BLE001
                    self.notes.append(f"원장 재조정 기록 실패 {cid} — {exc}")
                continue
            expired = day is not None and (today - day).days >= face_story.RECONCILE_GIVEUP_DAYS
            reason = ""
            try:
                if not video_id:
                    if reels is None:
                        reels = face.recent_reels()
                    video_id = next((rid for rid, desc in reels
                                     if chash and face_story.caption_hash(desc) == chash), "")
                    if not video_id:
                        reason = "게시 흔적 없음(최근 릴스에 같은 설명 없음)"
                if video_id:
                    new = _status_of(face, video_id)
                    if new is not None:
                        self.ledger.set_status(page_id, cid, new, video_id=video_id)
                        self.notes.append(f"원장 재조정 {cid} → {new}"
                                          + (" — 시리즈회차 결번 가능(이 회차는 게시되지 않음)"
                                             if new == face_story.STATUS_FAILED else ""))
                        continue
                    reason = "아직 처리 중"
            except FaceApiError as exc:
                if safety.is_account_fatal(exc):
                    raise
                reason = f"상태 확인 실패 {exc}"
            except Exception as exc:  # noqa: BLE001
                reason = f"재조정 오류 {exc}"
            if not expired:
                log.info("원장 재조정 보류 %s: %s", cid, reason)
                continue
            try:
                self.ledger.set_status(page_id, cid, face_story.STATUS_FAILED,
                                       f"확인 불가({face_story.RECONCILE_GIVEUP_DAYS}일 경과): {reason}",
                                       video_id=video_id)
                self.notes.append(f"원장 재조정 {cid} → 실패(확인 불가: {reason[:80]}) — 시리즈회차 결번 가능, "
                                  "실제로 게시됐다면 Notion 에서 게시완료로 고쳐 주십시오")
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"원장 재조정 기록 실패 {cid} — {exc}")
        overflow = getattr(self.ledger, "pending_overflow", 0)
        if overflow:
            self.notes.append(f"원장 확인필요 {overflow}건 이상 남음(다음 실행에서 이어서 재조정)")


def ledger_guard(face: FaceClient, ledger: LedgerRun, item: dict,
                 pending: list[tuple[dict, str]]) -> str | None:
    """v1.8.6 원장 기반 재게시 방지(DESIGN_V18 §10.3). 메시지를 돌려주면 이번 편은 Facebook 에 쓰지 않는다.

    None 이면 기존 흐름으로 게시한다(행 없음·실패·영상ID 없는 확인필요·조회 실패·이전 영상 처리 실패 확정).
    계정 치명 오류만 그대로 올린다.
    """
    prior = ledger.prior(item)
    if prior is None:
        return None
    status, video_id = prior
    cid = item["content_id"]
    if status == face_story.STATUS_PUBLISHED:
        return f"FB {cid}: 원장에 게시완료 — 재게시하지 않음 (video_id={video_id or '-'})"
    if status != face_story.STATUS_PENDING or not video_id:
        return None
    try:
        new = _status_of(face, video_id)
    except FaceApiError as exc:
        if safety.is_account_fatal(exc):
            raise
        return f"FB {cid}: 원장 확인필요(video_id={video_id}) 상태 확인 실패 — 재게시하지 않음 {exc}"
    if new == face_story.STATUS_PUBLISHED:
        ledger.record(item, new, video_id=video_id)
        return f"FB {cid}: 이전 게시 확인(video_id={video_id} 게시완료) — 재게시하지 않음"
    if new == face_story.STATUS_FAILED:
        # 이전 영상은 Facebook 이 처리 실패를 확정 → 공개되지 않았다. 원장에 남기고 새로 게시한다.
        ledger.record(item, new, video_id=video_id, error="이전 업로드 처리 실패 확정 — 다시 게시")
        return None
    pending.append((item, video_id))
    return f"FB {cid}: 이전 업로드(video_id={video_id}) 아직 처리 중 — 재게시하지 않음(확인필요 유지)"


def _open_ledger(notes: list[str]) -> LedgerRun | None:
    try:
        return LedgerRun(face_story.Ledger(_env("NOTION_TOKEN"), config.FACE_NOTION_DB_ID), notes)
    except face_story.LedgerError as exc:
        notes.append(f"원장 미사용 — {exc}")
        return None


def run() -> int:
    log.info("[ShortsPublish] v%s 시작 (config v%s) DRY_RUN=%s", VERSION, config.VERSION, _dry_run())
    log.info(safety.describe())
    base = Path(_env("SHORTS_OUT_DIR") or "out/shorts")
    now = dt.datetime.now(KST)
    manifest = load_manifest(base)
    channels = _publish_channels()   # DN2026_0003
    target = _target_date(now)       # DN2026_0003
    log.info("게시 범위: 채널=%s · 회차 날짜=%s · 시간대 무시=%s",
             ",".join(sorted(channels)), target, _ignore_window())
    items = fresh_items(manifest, target)
    if not items:
        log.info("게시할 오늘 영상 없음")
        if manifest.get("items"):
            # 늦은 승인으로 지난 날짜 영상만 남은 경우. 조용히 끝나면 원인을 알 수 없다.
            _notify("[Shorts] 승인 시점이 지나 게시하지 않았습니다(지난 날짜 영상) — "
                    + ", ".join(str(i.get("content_id")) for i in manifest["items"]))
        return 0

    face_reason = safety.face_block_reason()
    # DN2026_0003 : 이 실행이 맡은 채널만 게시한다
    face_items = ([i for i in items if shorts_plan.CHANNEL_FACE in i.get("channels", [])]
                  if shorts_plan.CHANNEL_FACE in channels else [])
    threads_items = ([i for i in items if shorts_plan.CHANNEL_THREADS in i.get("channels", [])][:1]
                     if shorts_plan.CHANNEL_THREADS in channels else [])
    if face_reason and face_items:
        log.info("Facebook 게시 안 함 — %s", face_reason)
        face_items = []
    # DN2026_0002 : 채널별 시간대로 각각 계산 → (영상, 시각, 채널) 항목을 시각순 병합.
    #   첫 편은 Facebook·Threads 공용 영상일 수 있으므로 채널 단위로 따로 게시한다.
    # DN2026_0003 : 수동 실행(SHORTS_IGNORE_WINDOW)은 시간대 무시 — 지금 + 첫 편 지연부터 게시
    face_window = _ANY_TIME_WINDOW if _ignore_window() else config.SHORTS_FACE_PUBLISH_WINDOW
    threads_window = _ANY_TIME_WINDOW if _ignore_window() else None
    # DN2026_0004 : 수동 즉시 게시(SHORTS_IGNORE_WINDOW=true)는 첫 편 지연 없이 바로 게시
    face_schedule = shorts_plan.publish_schedule(
        now, len(face_items), window=face_window, immediate=_ignore_window())
    threads_schedule = shorts_plan.publish_schedule(
        now, len(threads_items), window=threads_window, immediate=_ignore_window())
    planned = sorted(
        [(i, t, shorts_plan.CHANNEL_FACE) for i, t in zip(face_items, face_schedule, strict=False)]
        + [(i, t, shorts_plan.CHANNEL_THREADS)
           for i, t in zip(threads_items, threads_schedule, strict=False)],
        key=lambda entry: entry[1],
    )
    log.info("게시 계획: %s", ", ".join(
        f"{i['content_id']}({ch})@{t.strftime('%H:%M')}" for i, t, ch in planned))
    dropped = ([(i, shorts_plan.CHANNEL_FACE) for i in face_items[len(face_schedule):]]
               + [(i, shorts_plan.CHANNEL_THREADS) for i in threads_items[len(threads_schedule):]])
    for item, ch in dropped:
        log.warning("게시 시간대·job 예산을 넘어 제외 %s(%s)", item["content_id"], ch)

    if _dry_run():
        log.info("DRY_RUN — 대기·게시 없이 종료")
        return 0

    face = FaceClient(_env("FACE_PAGE_ID"), _env("FACE_PAGE_TOKEN")) if face_items else None
    threads = _threads_client() if threads_items and safety.shorts_threads_allowed() else None
    results: list[str] = [f"제외(시간 초과) {d['content_id']}({ch})" for d, ch in dropped]
    notes: list[str] = []
    ledger = _open_ledger(notes) if face and config.FACE_STORY_ENABLED else None
    if ledger is not None and face is not None:
        ledger.reconcile(face, now.date())
    pending: list[tuple[dict, str]] = []
    failed = False
    for item, at, channel in planned:  # DN2026_0002 : 채널 단위 항목
        _sleep_until(at)
        on_session = None
        skip: str | None = None
        do_face = channel == shorts_plan.CHANNEL_FACE and face is not None
        do_threads = channel == shorts_plan.CHANNEL_THREADS and threads is not None
        if do_face:
            skip = ledger_guard(face, ledger, item, pending) if ledger is not None else None
            if skip is not None:
                results.append(skip)
                log.info(skip)
            elif ledger is not None:
                # 게시 직전 선기록 + 세션 video_id 즉시 기록: 업로드 중 실행이 끊겨도 다음 실행이 재조정한다
                #   (리뷰 #3-F3 · 2차 OPS-N1).
                ledger.record(item, face_story.STATUS_PENDING, quiet=True)

                def on_session(vid: str, _item: dict = item) -> None:
                    ledger.record(_item, face_story.STATUS_PENDING, video_id=vid, quiet=True)
        if do_face and skip is None:
            try:
                outcome = face_outcome(face, item, base, with_ids=ledger is not None, on_session=on_session)
                results.append(outcome.message)
                if ledger is not None:
                    ledger.record(item, outcome.status, video_id=outcome.video_id)
                    if outcome.status == face_story.STATUS_PENDING and outcome.video_id:
                        pending.append((item, outcome.video_id))
            except FaceApiError as exc:
                if safety.is_account_fatal(exc):
                    if ledger is not None and (exc.before_session or exc.video_id):
                        # v1.8.6 F3: 업로드 세션 시작 요청에서 막혔으면 게시물이 생길 수 없다 → 실패.
                        #   세션 뒤면 확인필요(영상ID 보존). 중복 확인 조회 중 오류처럼 게시 여부를 모르는 경우는
                        #   선기록 '확인필요'를 그대로 둔다(재조정이 캡션해시로 확인 — 리뷰 v1.8.6 D1).
                        state = face_story.STATUS_FAILED if exc.before_session else face_story.STATUS_PENDING
                        ledger.record(item, state, video_id=exc.video_id, error=str(exc), quiet=True)
                    raise
                failed = True
                state = failure_status(exc) if ledger is not None else face_story.STATUS_FAILED
                if state == face_story.STATUS_PENDING:
                    # 리뷰 2차 CR-E: 결과를 모르는 오류를 '실패'로 알리면 수동 재게시 → 중복 게시 위험
                    results.append(f"FB {item['content_id']}: 결과 미확인(확인필요 — 재게시 금지, 다음 실행이 재조정) {exc}")
                else:
                    results.append(f"FB {item['content_id']}: 실패 {exc}")
                if ledger is not None:
                    ledger.record(item, state, video_id=exc.video_id, error=str(exc))
                    if state == face_story.STATUS_PENDING and exc.video_id:
                        pending.append((item, exc.video_id))
        if do_threads:
            try:
                results.append(post_threads(threads, item, base))
            except (ThreadsApiError, ContainerNotReadyError, media_host.MediaHostError) as exc:
                if safety.is_account_fatal(exc):
                    raise
                failed = True
                results.append(f"Threads {item['content_id']}: 실패 {exc}")
        log.info(results[-1] if results else "-")

    fatal: FaceApiError | None = None
    elapsed_min = (dt.datetime.now(KST) - now).total_seconds() / 60
    if pending and elapsed_min > config.SHORTS_JOB_BUDGET_MIN + RECHECK_SLACK_MIN:
        # 리뷰 2차 OPS-N4: job 시간 상한이 가까우면 재확인을 건너뛰고 다음 실행의 재조정에 맡긴다.
        notes.append(f"원장 확인필요 재확인 생략(경과 {elapsed_min:.0f}분) — 다음 실행에서 재조정")
        pending = []
    if ledger is not None and face is not None:
        for item, video_id in pending:
            try:
                new = _status_of(face, video_id)
            except FaceApiError as exc:
                if safety.is_account_fatal(exc):
                    # 리뷰 #2-5: 게시는 끝났다. 결과 알림을 먼저 보내고 회로 차단 종료(7)는 그 뒤에.
                    fatal = exc
                    notes.append(f"원장 확인필요 재확인 중단 — 계정 오류 {exc}")
                    break
                notes.append(f"원장 확인필요 재확인 실패 {item['content_id']} — {exc}")
                continue
            if new is not None:
                ledger.record(item, new, video_id=video_id)
                notes.append(f"원장 확인필요 재확인 {item['content_id']} → {new}")

    _notify("[Shorts] 게시 결과\n" + "\n".join(f"- {r}" for r in results)
            + ("\n\n[원장]\n" + "\n".join(f"- {n}" for n in notes) if notes else "")
            + "\n\n앱에서 각 게시물의 'AI 정보' 표시를 켜 주십시오(Meta AI 표시 의무).")
    if fatal is not None:
        raise fatal
    return FAIL_EXIT_CODE if failed else 0


def face_fatal_message(exc: FaceApiError) -> str:
    """Facebook 계정·토큰 오류 알림. Threads 재인가 안내가 섞이지 않게 따로 만든다."""
    kind = ("페이지 토큰 무효·만료 (code=190 / HTTP 401) — 장기 페이지 토큰 재발급 후 FACE_PAGE_TOKEN 교체"
            if exc.is_auth_error else
            "권한·접근 문제 (code=200) — 페이지 역할(CREATE_CONTENT)·앱 권한(pages_manage_posts 등) 확인")
    return (
        f"[Facebook][최우선] 숏폼 게시 — {kind}\n"
        "이번 실행의 쓰기를 즉시 멈췄습니다(재시도 없음).\n"
        "해소 전까지 Variables FACE_ENABLED=false 로 두십시오.\n"
        f"{exc}"
    )


def main() -> int:
    try:
        return run()
    except (FaceApiError, ThreadsApiError) as exc:
        if safety.is_account_fatal(exc) and isinstance(exc, FaceApiError):
            safety.trip(exc)
            msg = face_fatal_message(exc)
            log.error(msg)
            _notify(msg)
            return safety.FATAL_EXIT_CODE
        if safety.is_account_fatal(exc):
            return safety.handle_fatal(exc, "숏폼 게시", _notify)
        log.exception("게시 실패")
        _notify(f"[Shorts] 게시 실패\n{exc}")
        return FAIL_EXIT_CODE
    except Exception as exc:  # noqa: BLE001 — 최상위 방어
        log.exception("예기치 못한 오류")
        _notify(f"[Shorts] 게시 예기치 못한 오류\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

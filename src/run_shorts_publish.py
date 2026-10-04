"""숏폼 publish job (v1.7.0) — 마스터 승인(environment) 뒤에만 실행된다.

흐름
  1) build artifact 의 manifest.json 읽기 → 오늘(KST) 콘텐츠만 남김(신선도)
  2) 게시 시각 계산(shorts_plan.publish_schedule): 시간대 10~22시 · 첫 편 지연 · 편간 2~3시간대 간격
  3) 편마다 대기 → Facebook 릴스(중복 설명이면 건너뜀) → (첫 편만) Threads 동영상
     Threads 는 게시 직전에 킬 스위치·워밍업·하루 총량(DAILY_POST_BUDGET)을 다시 본다.
  4) 결과 알림 + 'AI 정보 표시는 앱에서' 안내

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
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, media_host, notifier, safety, shorts_plan
from .face_client import FaceApiError, FaceClient
from .threads_client import ContainerNotReadyError, ThreadsApiError, ThreadsClient, fetch_user_id

VERSION = "1.0.0"   # v1.7.0 신규

KST = ZoneInfo("Asia/Seoul")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("shorts-publish")
for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

FAIL_EXIT_CODE = 4


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _dry_run() -> bool:
    return _env("DRY_RUN").lower() not in ("false", "0", "no")


def _notify(message: str) -> None:
    notifier.send(_env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_ALERT_CHAT_ID"), message)


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


def post_face(client: FaceClient, item: dict, base: Path) -> str:
    """Facebook 릴스 1편. 반환은 결과 요약 문자열."""
    caption = str(item["caption"])
    if caption.strip() in {d.strip() for d in client.recent_descriptions()}:
        return f"FB {item['content_id']}: 같은 설명의 릴스가 이미 있어 건너뜀"
    video_id, status = client.publish_reel(base / item["video"], caption)
    if status is None:
        return f"FB {item['content_id']}: video_id={video_id} 게시 요청 완료 · 처리 상태 확인 필요"
    if status.failed:
        raise FaceApiError(200, f"릴스 처리 실패 video_id={video_id}: {status.error or status}")
    return f"FB {item['content_id']}: video_id={video_id} 게시 완료"


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


def run() -> int:
    log.info("[ShortsPublish] v%s 시작 (config v%s) DRY_RUN=%s", VERSION, config.VERSION, _dry_run())
    log.info(safety.describe())
    base = Path(_env("SHORTS_OUT_DIR") or "out/shorts")
    now = dt.datetime.now(KST)
    manifest = load_manifest(base)
    items = fresh_items(manifest, now.date())
    if not items:
        log.info("게시할 오늘 영상 없음")
        if manifest.get("items"):
            # 늦은 승인으로 지난 날짜 영상만 남은 경우. 조용히 끝나면 원인을 알 수 없다.
            _notify("[Shorts] 승인 시점이 지나 게시하지 않았습니다(지난 날짜 영상) — "
                    + ", ".join(str(i.get("content_id")) for i in manifest["items"]))
        return 0

    face_reason = safety.face_block_reason()
    face_items = [i for i in items if shorts_plan.CHANNEL_FACE in i.get("channels", [])]
    threads_items = [i for i in items if shorts_plan.CHANNEL_THREADS in i.get("channels", [])][:1]
    if face_reason:
        log.info("Facebook 게시 안 함 — %s", face_reason)
        face_items = []
    ordered = [i for i in items if i in face_items or i in threads_items]
    schedule = shorts_plan.publish_schedule(now, len(ordered))
    log.info("게시 계획: %s", ", ".join(
        f"{i['content_id']}@{t.strftime('%H:%M')}" for i, t in zip(ordered, schedule, strict=False)))
    dropped = ordered[len(schedule):]
    for item in dropped:
        log.warning("게시 시간대·job 예산을 넘어 제외 %s", item["content_id"])

    if _dry_run():
        log.info("DRY_RUN — 대기·게시 없이 종료")
        return 0

    face = FaceClient(_env("FACE_PAGE_ID"), _env("FACE_PAGE_TOKEN")) if face_items else None
    threads = _threads_client() if threads_items and safety.shorts_threads_allowed() else None
    results: list[str] = [f"제외(시간 초과) {d['content_id']}" for d in dropped]
    failed = False
    for item, at in zip(ordered, schedule, strict=False):
        _sleep_until(at)
        if face and item in face_items:
            try:
                results.append(post_face(face, item, base))
            except FaceApiError as exc:
                if safety.is_account_fatal(exc):
                    raise
                failed = True
                results.append(f"FB {item['content_id']}: 실패 {exc}")
        if threads and item in threads_items:
            try:
                results.append(post_threads(threads, item, base))
            except (ThreadsApiError, ContainerNotReadyError, media_host.MediaHostError) as exc:
                if safety.is_account_fatal(exc):
                    raise
                failed = True
                results.append(f"Threads {item['content_id']}: 실패 {exc}")
        log.info(results[-1] if results else "-")

    _notify("[Shorts] 게시 결과\n" + "\n".join(f"- {r}" for r in results)
            + "\n\n앱에서 각 게시물의 'AI 정보' 표시를 켜 주십시오(Meta AI 표시 의무).")
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

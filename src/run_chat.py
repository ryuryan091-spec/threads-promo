"""오전 시장 잡담(CHAT) 발행 + 답글 스윕 엔트리포인트.

실행: python -m src.run_chat

흐름
  1. CHAT_ENABLED · 휴식일 확인
  2. 토큰 확보 → 오늘 게시물 조회 → CHAT 게이트(chat_plan.gate)
  3. 근거 수집(mood_source) → 생성 → lint_chat (재시도 AI_MAX_RETRY)
  4. 랜덤 지연 → 게이트 재검증 → 텍스트 발행 (이미지·링크 셀프리플 없음)
  5. 답글 스윕(run_reply.sweep, 실행당 REPLY_PER_RUN_CAP)

정책
  - 생성에 실패하면 발행하지 않는다. 정적 폴백을 두지 않는다.
    하루 여러 번 같은 정형문이 나가면 그 자체가 봇 신호다.
  - 링크를 붙이지 않는다. 링크는 정기 발행 1건에만 둔다.
  - DRY_RUN 은 게이트 결과와 무관하게 생성문을 미리보기로 출력한다(검증용).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from zoneinfo import ZoneInfo

from . import (
    ai_writer,
    antibot,
    chat_plan,
    config,
    content,
    mood_source,
    run_reply,
    watchdog,
)
from .env import MissingEnvError, Settings, load_settings
from .main import _acquire_token, _notify_safe
from .threads_client import ThreadsApiError, ThreadsClient, fetch_user_id

VERSION = "1.0.1"
KST = ZoneInfo("Asia/Seoul")
PILLAR_KEY = "CHAT"
POSTS_TO_SCAN = 25   # 오늘 게시물(정기 1 + 셀프리플 제외 + CHAT ≤9 + 이벤트) 을 덮는 크기

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-chat")

for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _is_manual() -> bool:
    event = os.environ.get("EVENT_NAME", "").strip()
    return bool(event) and event != "schedule"


def _today_posts(client: ThreadsClient, today: dt.date) -> list[dict]:
    # 서버 since 는 경계 해석이 문서에 없으므로 하루 앞당겨 받고, 판정은 timestamp 로 한다.
    return client.get_my_posts(POSTS_TO_SCAN, since=today - dt.timedelta(days=1))


def _check_gate(
    client: ThreadsClient,
    today: dt.date,
    trigger: int | None,
    manual: bool,
    *,
    enforce_gap: bool = True,
) -> tuple[str | None, int]:
    """(보류 사유, 간격 대기 초). 사유가 None 이면 발행 가능."""
    now = dt.datetime.now(dt.UTC)
    counts = chat_plan.count_posts(
        _today_posts(client, today), now, watchdog.parse_threads_timestamp
    )
    log.info(
        "오늘 CHAT %d건 / 목표 %d건 / 선택 트리거 %s",
        counts.chat_today, chat_plan.daily_target(today),
        chat_plan.selected_triggers(today),
    )
    blocked = chat_plan.gate(
        today, now, trigger, counts, manual=manual, enforce_gap=enforce_gap
    )
    wait = chat_plan.gap_wait_seconds(counts, now)
    if blocked is None and not enforce_gap and wait > config.CHAT_MAX_GAP_WAIT_SEC:
        blocked = (
            f"직전 게시물과 간격 확보에 {wait}초 대기 필요 — "
            f"한도 {config.CHAT_MAX_GAP_WAIT_SEC}초 초과"
        )
    return blocked, wait


def generate_chat(
    api_key: str, seed: str, recent_texts: list[str], mood: mood_source.Mood
) -> str:
    """생성 + lint_chat. 재시도 소진 시 빈 문자열."""
    for attempt in range(1, config.AI_MAX_RETRY + 1):
        try:
            text = ai_writer.generate(
                api_key, PILLAR_KEY, seed, recent_texts,
                facts_block=mood.to_prompt_block(),
            )
            content.lint_chat(text)
            return text
        except content.ContentPolicyError as exc:
            log.warning("CHAT 린트 실패 (%d/%d): %s", attempt, config.AI_MAX_RETRY, exc)
        except ai_writer.AiWriterError as exc:
            log.warning("CHAT 생성 실패 (%d/%d): %s", attempt, config.AI_MAX_RETRY, exc)
    return ""


def _safe_sweep(client: ThreadsClient, settings: Settings) -> None:
    """답글 스윕. 실패가 CHAT 결과를 뒤집지 않게 격리한다(차단 신호만 전파)."""
    if not config.REPLY_ENABLED:
        log.info("REPLY_ENABLED=false — 답글 스윕 생략")
        return
    try:
        run_reply.sweep(
            client, settings,
            per_run_cap=config.REPLY_PER_RUN_CAP, dry_run=settings.dry_run,
        )
    except ThreadsApiError as exc:
        if exc.is_blocked:
            raise
        log.warning("답글 스윕 실패 — CHAT 결과에는 영향 없음: %s", exc)


def run() -> int:
    log.info("[ChatRun] v%s 시작", VERSION)

    settings = load_settings()
    manual = _is_manual()

    if not config.CHAT_ENABLED and not (manual and settings.dry_run):
      log.info("CHAT_ENABLED=false — 종료 (수동 dry_run 만 허용)")

    today = dt.datetime.now(KST).date()
    if antibot.is_rest_day(today, config.PUBLISH_WEEKLY_REST_DAYS):
        return 0

    trigger_raw = os.environ.get("TRIGGER", "").strip()
    trigger = chat_plan.trigger_number(trigger_raw)
    log.info("트리거=%s 수동=%s DRY_RUN=%s", trigger_raw or "-", manual, settings.dry_run)

    token = _acquire_token(settings)
    user_id = settings.threads_user_id
    if user_id == "me":
        user_id, username = fetch_user_id(token)
        log.info("사용자 ID 조회 완료 — @%s", username)
    client = ThreadsClient(user_id, token)

    quota = client.get_post_quota()
    log.info("발행 쿼터 %d/%d (잔여 %d)", quota.used, quota.total, quota.remaining)

    # 사전 판정은 간격을 강제하지 않는다. 간격이 모자라면 기다렸다 낸다.
    blocked, gap_wait = _check_gate(client, today, trigger, manual, enforce_gap=False)
    if quota.remaining < 1:
        blocked = blocked or f"발행 쿼터 잔여 {quota.remaining}건"

    if blocked and not settings.dry_run:
        log.info("CHAT 보류 — %s", blocked)
        _safe_sweep(client, settings)
        return 0
    if blocked:
        log.info("CHAT 게이트 보류 사유(DRY_RUN 이라 미리보기는 계속): %s", blocked)

    if not settings.can_generate or not config.AI_ENABLED:
        log.warning("AI 생성 불가(CLAUDE_AI_KEY/AI_ENABLED) — CHAT 은 정적 폴백이 없어 생략")
        _safe_sweep(client, settings)
        return 0

    now = dt.datetime.now(dt.UTC)
    first = chat_plan.source_for(today, trigger, config.CHAT_SOURCE_MODE)
    mood = mood_source.collect(
        first, api_key=settings.claude_api_key, today=today, now=now
    )

    recent_texts = client.get_recent_texts(config.CHAT_RECENT_FOR_DEDUP)
    seed_index = today.timetuple().tm_yday * 10 + (trigger or 0)
    seed = ai_writer.pick_seed(PILLAR_KEY, seed_index)

    text = generate_chat(settings.claude_api_key, seed, recent_texts, mood)
    log.info(
        "기둥=CHAT 소스=%s(우선=%s) 테마=%s 소재=%s 생성=%s",
        mood.source, first, ",".join(mood.themes) or "-", seed,
        "성공" if text else "실패",
    )

    if not text:
        log.warning("CHAT 생성 재시도 소진 — 이번 트리거는 발행하지 않습니다.")
        _safe_sweep(client, settings)
        return 0

    if settings.dry_run:
        log.info("DRY_RUN — 실제 발행하지 않습니다.\n--- CHAT 본문 ---\n%s", text)
        _safe_sweep(client, settings)
        return 0

    if gap_wait:
        log.info("직전 게시물과 간격 확보를 위해 최소 %d초 대기", gap_wait)
    antibot.jitter_sleep(*chat_plan.jitter_range(gap_wait), label="CHAT 발행 전")

    # 지터 도중 다른 발행이 나갔거나 창을 벗어났을 수 있다. 직전에 간격까지 포함해 다시 확인한다.
    blocked_again, _ = _check_gate(client, today, trigger, manual)
    if blocked_again:
        log.info("지연 후 재검증에서 보류 — %s", blocked_again)
        _safe_sweep(client, settings)
        return 0

    post_id = client.publish_text_post(text)
    log.info("CHAT 발행 완료 post_id=%s", post_id)

    _safe_sweep(client, settings)
    return 0


def main() -> int:
    try:
        return run()
    except MissingEnvError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except ThreadsApiError as exc:
        if exc.is_blocked:
            log.error("API 접근 차단 (code=200) — 워크플로우를 비활성화하십시오.\n%s", exc)
            _notify_safe(
                "[Threads][최우선] API 접근 차단 (code=200)\n"
                "developers.facebook.com 에서 계정·앱 상태를 확인하십시오.\n"
                "해소 전까지 모든 워크플로우를 Disable 하십시오.\n"
                f"{exc}"
            )
            return 7
        hint = " (재인가 필요)" if exc.is_auth_error else ""
        log.error("Threads API 오류%s: %s", hint, exc)
        _notify_safe(f"[Threads Chat] API 오류{hint}\n{exc}")
        return 4
    except Exception as exc:  # noqa: BLE001 — 최상위 방어
        log.exception("예기치 못한 오류")
        _notify_safe(f"[Threads Chat] 예기치 못한 오류\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

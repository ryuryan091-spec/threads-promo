"""이벤트 기반 STORY 발행.

실행: python -m src.run_story

EDT 회차가 트래커에 올라오면 그 회차를 근거로 Threads 에 STORY 를 발행한다.
정기 발행과 별개 경로지만, 발행 로직 자체는 main 의 것을 재사용한다.
중복 구현하면 한쪽만 고치는 사고가 난다.

무상태 신규 판정
  마지막 처리 회차를 저장하지 않는다. created_time 시간창으로 판정한다.
  cron 주기 6시간에 창 7.2시간(20% 여유)을 두어 경계 누락을 막는다.
  그 대가인 경계 중복은 아래 안전장치가 흡수한다.

안전장치
  1. 오늘 정기 발행이 이미 STORY 였는가 -> 스킵
  2. 직전 발행과 최소 간격이 확보되었는가
  3. 오늘 이벤트 발행 상한을 넘지 않았는가
  4. 발행 쿼터 잔여가 있는가
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from zoneinfo import ZoneInfo

from . import (
    ai_writer,
    antibot,
    config,
    content,
    notion_source,
    watchdog,
)
from .env import MissingEnvError, Settings, load_settings
from .main import (
    REPO_ROOT,
    _acquire_token,
    _notify_safe,
    _preflight,
    _resolve_raw_base_url,
    _select_usable_image,
)
from .threads_client import (
    ContainerNotReadyError,
    ThreadsApiError,
    ThreadsClient,
    fetch_user_id,
)

VERSION = "1.0.0"
KST = ZoneInfo("Asia/Seoul")
ASSETS_DIR = REPO_ROOT / "assets"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-story")

for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _hours_since_last_post(posts: list[dict], now: dt.datetime) -> float | None:
    """마지막 발행 이후 경과 시간. 판정 불가면 None."""
    stamps = [
        parsed
        for post in posts
        if (parsed := watchdog.parse_threads_timestamp(str(post.get("timestamp", ""))))
    ]
    if not stamps:
        return None
    return watchdog.hours_since(max(stamps), now)


def _events_published_today(posts: list[dict], now: dt.datetime) -> int:
    """오늘 발행된 글 수. 이벤트 상한 판정에 쓴다.

    정기와 이벤트를 구분할 표식이 본문에 없으므로, 오늘 발행 총량으로 본다.
    보수적으로 잡는 편이 안전하다.
    """
    today = now.astimezone(KST).date()
    count = 0
    for post in posts:
        parsed = watchdog.parse_threads_timestamp(str(post.get("timestamp", "")))
        if parsed and parsed.astimezone(KST).date() == today:
            count += 1
    return count


def _regular_pillar_today(today: dt.date) -> str:
    """오늘 정기 발행이 어느 기둥이었는지.

    슬롯 A~C 중 어느 것이 당첨됐는지 모르므로, 세 슬롯 중 하나라도
    STORY 면 STORY 로 본다. 보수적 판정이다.
    """
    for slot_disc in config.DISCRIMINATOR_BY_SLOT.values():
        idx = content.run_index(today, slot_disc)
        if ai_writer.pick_pillar(idx) == "STORY":
            return "STORY"
    return "OTHER"


def _gate(
    posts: list[dict], now: dt.datetime, today: dt.date, quota_remaining: int
) -> str | None:
    """발행을 막아야 하는 사유. 없으면 None."""
    if _regular_pillar_today(today) == "STORY":
        return "오늘 정기 발행이 STORY 입니다. 중복을 피해 건너뜁니다."

    elapsed = _hours_since_last_post(posts, now)
    if elapsed is not None and elapsed < config.EVENT_MIN_GAP_HOURS:
        return (
            f"직전 발행 후 {elapsed:.1f}시간 — 최소 간격 "
            f"{config.EVENT_MIN_GAP_HOURS}시간 미만입니다."
        )

    today_count = _events_published_today(posts, now)
    if today_count >= config.EVENT_DAILY_CAP:
        return f"오늘 발행 {today_count}건 — 상한 {config.EVENT_DAILY_CAP}건 도달."

    if quota_remaining < 2:
        return f"발행 쿼터 잔여 {quota_remaining}건 — 부족합니다."

    return None


def run() -> int:
    log.info("[StoryEvent] v%s 시작", VERSION)

    if not config.EVENT_STORY_ENABLED:
        log.info("EVENT_STORY_ENABLED=false — 종료")
        return 0

    _preflight()

    settings = load_settings()
    now = dt.datetime.now(dt.UTC)
    today = dt.datetime.now(KST).date()

    if not settings.can_fetch_episodes:
        log.info("Notion 설정 없음 — 이벤트 발행은 근거 없이 하지 않습니다. 종료")
        return 0

    since = now - dt.timedelta(hours=config.EVENT_WINDOW_HOURS)
    episodes = notion_source.fetch_new_episodes(
        settings.notion_token,
        config.NOTION_DB_ID,
        since,
        config.NOTION_EPISODE_LIMIT,
        status_property=config.NOTION_STATUS_PROPERTY,
        status_value=config.NOTION_STATUS_VALUE,
    )

    if not episodes:
        log.info("신규 회차 없음 — 종료")
        return 0

    log.info("신규 회차 %d건 감지", len(episodes))

    token = _acquire_token(settings)
    user_id = settings.threads_user_id
    if user_id == "me":
        user_id, username = fetch_user_id(token)
        log.info("사용자 ID 조회 완료 — @%s", username)

    client = ThreadsClient(user_id, token)
    quota = client.get_post_quota()
    posts = client.get_my_posts(5)

    blocked = _gate(posts, now, today, quota.remaining)
    if blocked:
        log.info("이벤트 발행 보류 — %s", blocked)
        return 0

    # 근거는 신규 회차만 쓴다. 과거 회차를 섞으면 "새 회차 이야기"가 아니게 된다.
    episode_block = "\n".join(["# 방금 발행한 회차", *[f"- {e}" for e in episodes]])

    recent_texts = client.get_recent_texts(config.RECENT_POSTS_FOR_DEDUP)

    plan = content.build_plan(
        today,
        ASSETS_DIR,
        _resolve_raw_base_url(settings),
        claude_api_key=settings.claude_api_key,
        recent_texts=recent_texts,
        episode_block=episode_block,
        discriminator=config.DISCRIMINATOR_EVENT,
    )
    log.info(
        "기둥=%s 소재=%s 생성=%s 근거=회차%d건",
        plan.pillar, plan.seed, plan.source, len(episodes),
    )

    if plan.pillar != "STORY":
        # 이벤트 발행은 STORY 여야 의미가 있다. 로테이션이 다른 기둥을 뽑으면 보류.
        log.info("이벤트 슬롯이 STORY 가 아님(%s) — 보류", plan.pillar)
        return 0

    image_url, degrade_reasons = _select_usable_image(plan, settings)

    if settings.dry_run:
        log.info("DRY_RUN — 실제 발행하지 않습니다.")
        log.info("--- 본문 ---\n%s", plan.text)
        log.info("--- 리플 ---\n%s", plan.reply_text)
        log.info("--- 이미지 ---\n%s", image_url or "(텍스트 전용)")
        return 0

    # 안티봇 — EDT 발행 시각에 종속되지 않도록 넓게 지연한다.
    antibot.jitter_sleep(*config.ANTIBOT_EVENT_JITTER, label="이벤트 발행 전")

    # 지터 도중 정기 발행이 나갔을 수 있다. 게이트를 다시 통과시킨다.
    # 게이트는 '아직 발행되지 않은' 글을 볼 수 없으므로 사전 판정만으로는
    # 부족하다. 실제 발행 직전에 최신 상태로 재확인한다.
    recheck_now = dt.datetime.now(dt.UTC)
    fresh_posts = client.get_my_posts(5)
    blocked_again = _gate(
        fresh_posts, recheck_now, today, client.get_post_quota().remaining
    )
    if blocked_again:
        log.info("지연 후 재검증에서 보류 — %s", blocked_again)
        return 0

    post_id = None
    if image_url:
        try:
            post_id = client.publish_image_post(image_url, plan.text, dry_run=False)
            log.info("본문 발행 완료 (이미지) post_id=%s", post_id)
        except (ContainerNotReadyError, ThreadsApiError) as exc:
            log.error("이미지 발행 실패 — 텍스트 폴백: %s", exc)
            degrade_reasons.append(f"이미지 발행 실패: {str(exc)[:200]}")
            if not config.IMAGE_FALLBACK_TO_TEXT:
                raise

    if post_id is None:
        post_id = client.publish_text_post(plan.text, dry_run=False)
        log.warning("본문 발행 완료 (텍스트 전용 폴백) post_id=%s", post_id)
        _notify_safe(
            "[Threads] 이벤트 발행 — 이미지 없이 텍스트만 발행했습니다.\n"
            + "\n".join(degrade_reasons[:3])
        )

    reply_id = client.publish_self_reply(post_id, plan.reply_text, dry_run=False)
    log.info("셀프 리플라이 발행 완료 reply_id=%s", reply_id)
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
                f"{exc}"
            )
            return 7
        log.error("Threads API 오류: %s", exc)
        _notify_safe(f"[Threads] 이벤트 발행 API 오류\n{exc}")
        return 4
    except Exception as exc:  # noqa: BLE001
        log.exception("예기치 못한 오류")
        _notify_safe(f"[Threads] 이벤트 발행 오류\n{exc}")
        return 1


__all__ = ["Settings", "main", "run"]


if __name__ == "__main__":
    sys.exit(main())

"""이벤트 기반 STORY 발행.

실행: python -m src.run_story

EDT 회차가 트래커에 올라오면 그 회차를 근거로 Threads 에 STORY 를 발행한다.
정기 발행과 별개 경로지만, 발행 로직 자체는 main 의 것을 재사용한다.
중복 구현하면 한쪽만 고치는 사고가 난다.

무상태 신규 판정
  마지막 처리 회차를 저장하지 않는다. created_time 시간창으로 판정한다.
  v1.2.0: 창 = 직전 cron 과의 간격 × 1.2 (최소 EVENT_WINDOW_HOURS).
  cron 간격이 3.9~10.3시간으로 불균등해 고정 7.2시간 창은 03:11→13:29 사이
  회차를 놓쳤다. 그 대가인 경계 중복은 아래 안전장치가 흡수한다.

안전장치
  1. 오늘 정기 발행 기둥이 STORY 인가(당첨 슬롯 예측) / 오늘 STORY 가 이미 나갔는가 -> 스킵
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
    chat_plan,
    config,
    content,
    insights,
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

VERSION = "1.2.0"   # v1.2.0: 이벤트 기둥 STORY 강제, 당첨 슬롯 기반 차단, 가변 감지 창
KST = ZoneInfo("Asia/Seoul")
ASSETS_DIR = REPO_ROOT / "assets"
# CHAT 도입 후 하루 게시물이 약 10건이다. 5건이면 정기 글이 보이지 않는다.
POSTS_TO_SCAN = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-story")

for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _non_chat_stamps(posts: list[dict]) -> list[dt.datetime]:
    """CHAT 창 게시물을 뺀 발행 시각.

    CHAT 은 오전 잡담이라 정기·이벤트 발행과 간격·상한을 공유하지 않는다.
    섞어 세면 CHAT 만으로 이벤트 상한·최소 간격이 매일 막힌다.
    """
    stamps: list[dt.datetime] = []
    for post in posts:
        parsed = watchdog.parse_threads_timestamp(str(post.get("timestamp", "")))
        if parsed and not chat_plan.is_chat_post(parsed, str(post.get("media_type") or "")):
            stamps.append(parsed)
    return stamps


def _hours_since_last_post(posts: list[dict], now: dt.datetime) -> float | None:
    """마지막 발행(CHAT 제외) 이후 경과 시간. 판정 불가면 None."""
    stamps = _non_chat_stamps(posts)
    if not stamps:
        return None
    return watchdog.hours_since(max(stamps), now)


def _events_published_today(posts: list[dict], now: dt.datetime) -> int:
    """오늘 발행된 글 수. 이벤트 상한 판정에 쓴다.

    정기와 이벤트를 구분할 표식이 본문에 없으므로, 오늘 발행 총량(CHAT 제외)으로 본다.
    보수적으로 잡는 편이 안전하다.
    """
    today = now.astimezone(KST).date()
    return sum(1 for s in _non_chat_stamps(posts) if s.astimezone(KST).date() == today)


def _predicted_regular_pillar(today: dt.date) -> str | None:
    """오늘 정기 발행(당첨 슬롯)의 기둥. 휴식일이면 None.

    v1.2.0: 이전에는 슬롯 A~C 중 하나라도 STORY 면 막았다. 로테이션(길이 8, STORY 3칸)과
    run_index = 날짜 + 구분자 구조상 연속 3칸에는 항상 STORY 가 있어, 이벤트가
    STORY 인 날이 한 번도 없었다(120일 시뮬레이션 0일). 정기 발행과 같은 함수·솔트로
    당첨 슬롯을 계산해 그 슬롯의 기둥만 본다(main._slot_gate 와 동일 결정론).
    """
    if antibot.is_rest_day(today, config.PUBLISH_WEEKLY_REST_DAYS):
        return None
    slots = list(config.PUBLISH_SLOTS)
    if not slots:
        return None
    slot = antibot.choose_slot(today, slots, config.ANTIBOT_SLOT_SALT_PUBLISH)
    disc = config.DISCRIMINATOR_BY_SLOT.get(slot)
    if disc is None:
        return None
    return ai_writer.pick_pillar(content.run_index(today, disc))


def _story_published_today(posts: list[dict], now: dt.datetime) -> bool:
    """오늘(KST) 이미 STORY 로 복원되는 글이 나갔는지(수동·이벤트 포함, CHAT 제외)."""
    today = now.astimezone(KST).date()
    for post in posts:
        parsed = watchdog.parse_threads_timestamp(str(post.get("timestamp", "")))
        if parsed is None or parsed.astimezone(KST).date() != today:
            continue
        pillar = insights.restore_pillar(parsed, str(post.get("media_type") or ""))
        if pillar == "STORY":
            return True
    return False


def event_window_hours(now: dt.datetime) -> float:
    """이번 실행의 신규 회차 감지 창(시간).

    직전 이벤트 cron 과 현재 cron 사이 간격에 20% 여유를 더한다(cron 지연분 포함).
    EVENT_WINDOW_HOURS 보다 작아지지 않는다. 수동 실행도 같은 규칙을 쓴다.
    """
    local = now.astimezone(KST)
    minute_now = local.hour * 60 + local.minute
    marks = sorted(int(t[:2]) * 60 + int(t[3:]) for t in insights.EVENT_SLOTS)
    if not marks:
        return config.EVENT_WINDOW_HOURS
    # 현재 시각 이하 가장 늦은 cron(자정 넘김 포함)과 그 직전 cron
    past = [m for m in marks if m <= minute_now]
    current = past[-1] if past else marks[-1] - 24 * 60
    idx = marks.index(current % (24 * 60)) if current >= 0 else len(marks) - 1
    previous = marks[idx - 1] if idx > 0 else marks[-1] - 24 * 60
    if current < 0:
        previous -= 24 * 60
    gap_min = current - previous
    since_current = minute_now - current
    hours = (gap_min * 1.2 + since_current) / 60
    return max(config.EVENT_WINDOW_HOURS, round(hours, 2))


def _gate(
    posts: list[dict], now: dt.datetime, today: dt.date, quota_remaining: int
) -> str | None:
    """발행을 막아야 하는 사유. 없으면 None."""
    predicted = _predicted_regular_pillar(today)
    if predicted == "STORY":
        return "오늘 정기 발행(당첨 슬롯) 기둥이 STORY 입니다. 중복을 피해 건너뜁니다."
    if _story_published_today(posts, now):
        return "오늘 STORY 글이 이미 발행되었습니다."

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

    # v1.2.0: 휴식일에는 정기·CHAT 과 같이 이벤트도 쉰다(휴식일 취지 유지).
    if antibot.is_rest_day(today, config.PUBLISH_WEEKLY_REST_DAYS):
        log.info("휴식일 — 이벤트 발행도 쉽니다. 종료")
        return 0

    if not settings.can_fetch_episodes:
        log.info("Notion 설정 없음 — 이벤트 발행은 근거 없이 하지 않습니다. 종료")
        return 0

    window_hours = event_window_hours(now)
    log.info("신규 회차 감지 창 %.2f시간", window_hours)
    since = now - dt.timedelta(hours=window_hours)
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
    posts = client.get_my_posts(POSTS_TO_SCAN, since=today - dt.timedelta(days=1))

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
        pillar="STORY",
    )
    log.info(
        "기둥=%s 소재=%s 생성=%s 근거=회차%d건",
        plan.pillar, plan.seed, plan.source, len(episodes),
    )

    if plan.source != "ai":
        # v1.2.0: 기둥은 STORY 로 강제된다. 정적 폴백 문구는 회차 근거가 없으므로
        # '새 회차 이야기'가 아니다. 이벤트로 낼 의미가 없어 보류한다.
        log.info("회차 근거 생성 실패(생성=%s) — 이벤트 발행 보류", plan.source)
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
    # v1.2.0: 지터(최대 50분) 동안 KST 자정을 넘길 수 있다. 날짜를 다시 계산한다.
    recheck_today = recheck_now.astimezone(KST).date()
    fresh_posts = client.get_my_posts(
        POSTS_TO_SCAN, since=recheck_today - dt.timedelta(days=1)
    )
    blocked_again = _gate(
        fresh_posts, recheck_now, recheck_today, client.get_post_quota().remaining
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

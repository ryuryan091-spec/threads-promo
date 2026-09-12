"""답글 엔진 엔트리포인트.

실행: python -m src.run_reply

흐름
  1. 슬롯 판정 (오늘 이 슬롯이 당첨이 아니면 즉시 종료 — Actions 분 절약)
  2. 토큰 확보
  3. 답글 쿼터 확인
  4. 최근 내 글 N개 조회
  5. 각 글의 대화를 훑어 처리 대상 댓글 추출
  6. 순서 섞기 → 랜덤 지연 → 답글 발행
  7. 일일 캡·저자 캡 준수, 실패 시 텔레그램 알림
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

from . import antibot, config, content, notifier, reply_engine
from .env import MissingEnvError, load_settings
from .main import _acquire_token  # 토큰 확보 로직 재사용
from .reply_engine import Comment, ReplyStrategy
from .threads_client import ThreadsApiError, ThreadsClient, fetch_user_id

VERSION = "1.0.0"
KST = ZoneInfo("Asia/Seoul")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-reply")

for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _parse_comment(raw: dict) -> Comment:
    replied_to = raw.get("replied_to") or {}
    return Comment(
        id=str(raw.get("id", "")),
        text=(raw.get("text") or "").strip(),
        username=str(raw.get("username", "")),
        timestamp=str(raw.get("timestamp", "")),
        replied_to_id=str(replied_to.get("id", "")),
        owned_by_me=bool(raw.get("is_reply_owned_by_me", False)),
        hide_status=str(raw.get("hide_status", "")),
    )


def _already_replied_ids(comments: list[Comment]) -> set[str]:
    """내가 이미 답글을 단 댓글 ID 집합.

    내 답글의 replied_to.id 가 곧 '내가 응답한 대상'이다.
    DB 없이 중복을 막는 유일한 방법.
    """
    return {
        c.replied_to_id
        for c in comments
        if c.owned_by_me and c.replied_to_id
    }


def run() -> int:
    log.info("[ReplyEngine] v%s 시작", VERSION)

    if not config.REPLY_ENABLED:
        log.info("REPLY_ENABLED=false — 종료")
        return 0

    today = dt.datetime.now(KST).date()

    # 1) 슬롯 판정 — 시각 분산. 당첨 아니면 즉시 종료해 Actions 분을 아낀다.
    slots = [s for s in os.environ.get("REPLY_SLOTS", "").split(",") if s.strip()]
    current_slot = os.environ.get("SLOT", "").strip()
    if slots and current_slot:
        if not antibot.should_run_this_slot(
            today, current_slot, slots, config.ANTIBOT_SLOT_SALT_REPLY
        ):
            log.info("오늘 슬롯이 아님 — 종료")
            return 0

    settings = load_settings()
    dry_run = settings.dry_run

    token = _acquire_token(settings)
    user_id = settings.threads_user_id
    if user_id == "me":
        user_id, username = fetch_user_id(token)
        log.info("사용자 ID 조회 완료 — @%s", username)

    client = ThreadsClient(user_id, token)

    # 2) 쿼터
    quota = client.get_reply_quota()
    log.info("답글 쿼터 %d/%d (잔여 %d)", quota.used, quota.total, quota.remaining)
    if quota.remaining < 1:
        log.warning("API 답글 쿼터 소진 — 종료")
        return 0

    # 3) 최근 내 글
    posts = client.get_my_posts(config.REPLY_SCAN_POSTS)
    log.info("대상 원글 %d건", len(posts))
    if not posts:
        return 0

    # 4) 대상 추출
    plans: list[tuple[str, reply_engine.ReplyDecision]] = []
    author_used: dict[str, int] = defaultdict(int)

    for post in posts:
        post_id = str(post.get("id", ""))
        post_text = (post.get("text") or "").strip()
        if not post_id:
            continue

        try:
            raw_items = client.get_conversation(post_id, config.REPLY_SCAN_LIMIT)
        except ThreadsApiError as exc:
            log.warning("대화 조회 실패 post=%s: %s", post_id, exc)
            continue

        comments = [_parse_comment(r) for r in raw_items]
        replied = _already_replied_ids(comments)

        for comment in comments:
            decision = reply_engine.decide(
                comment,
                already_replied=comment.id in replied,
                author_used=author_used[comment.username],
            )
            if decision.strategy is ReplyStrategy.SKIP:
                log.debug("스킵 %s — %s", comment.id, decision.reason)
                continue
            author_used[comment.username] += 1
            plans.append((post_text, decision))

    log.info("응답 후보 %d건", len(plans))
    if not plans:
        return 0

    # 5) 순서 섞기 + 캡 적용
    ordered = antibot.shuffled(plans)
    cap = min(config.REPLY_DAILY_CAP, quota.remaining)

    sent = 0
    for post_text, decision in ordered:
        if not antibot.within_daily_cap(sent, cap, label="답글"):
            break

        text = reply_engine.compose(
            decision, post_text, settings.claude_api_key, content.lint
        )
        if not text:
            log.info("응답 생략 %s (%s)", decision.comment.id, decision.reason)
            continue

        if dry_run:
            log.info(
                "DRY_RUN 대상=%s(@%s) 방침=%s\n  댓글: %s\n  답글: %s",
                decision.comment.id, decision.comment.username,
                decision.strategy.value, decision.comment.text[:60], text,
            )
            sent += 1
            continue

        # 안티봇 — 답글 사이 랜덤 지연
        if sent > 0:
            antibot.jitter_sleep(*config.ANTIBOT_REPLY_JITTER, label="답글 간격")

        try:
            reply_id = client.publish_self_reply(decision.comment.id, text)
            log.info("답글 발행 완료 %s -> %s", decision.comment.id, reply_id)
            sent += 1
        except ThreadsApiError as exc:
            log.error("답글 발행 실패 %s: %s", decision.comment.id, exc)
            notifier.send(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                f"[Threads Reply] 발행 실패 id={decision.comment.id}\n{exc}",
            )

    log.info("완료 — 발행 %d건", sent)
    return 0


def main() -> int:
    try:
        return run()
    except MissingEnvError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except ThreadsApiError as exc:
        log.error("Threads API 오류: %s", exc)
        _notify_safe(f"[Threads Reply] API 오류\n{exc}")
        return 4
    except Exception as exc:  # noqa: BLE001
        log.exception("예기치 못한 오류")
        _notify_safe(f"[Threads Reply] 예기치 못한 오류\n{exc}")
        return 1


def _notify_safe(message: str) -> None:
    try:
        s = load_settings()
        notifier.send(s.telegram_bot_token, s.telegram_chat_id, message)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())

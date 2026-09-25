"""답글 엔진 엔트리포인트.

실행: python -m src.run_reply

흐름
  1. 토큰 확보
  2. sweep() — 최근 24시간 내 글의 대화를 훑어 답글
       - 답글 쿼터 확인
       - 오늘(KST) 내가 이미 단 답글 수를 Threads 에서 산출 → 일일 캡 잔여 계산
       - 저자별·스레드별 누적을 Threads 에서 산출 → 저자 캡·스레드 캡
       - 순서 섞기 → 랜덤 지연 → 답글 발행

v1.1.0 변경 (CHAT 도입에 따른 빈도 확대)
  - 슬롯 1-of-3 추첨 제거. reply.yml 은 3슬롯 모두 실행한다.
    run_chat 도 실행마다 sweep() 을 호출한다(하루 최대 9회).
  - 일일 캡·저자 캡을 실행 내 메모리가 아니라 Threads 조회 결과로 산정한다.
    실행이 여러 번이어도 하루 캡이 하루 캡으로 유지된다.
  - 대댓글(내 답글에 달린 댓글)은 내 직전 답글을 맥락으로 함께 넘긴다.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from . import antibot, config, content, notifier, reply_engine, watchdog
from .env import MissingEnvError, Settings, load_settings
from .main import _acquire_token  # 토큰 확보 로직 재사용
from .reply_engine import Comment, ReplyStrategy
from .threads_client import (
    ContainerNotReadyError,
    ThreadsApiError,
    ThreadsClient,
    fetch_user_id,
)

VERSION = "1.2.0"   # v1.2.0: 제3자 간 대화 제외, 컨테이너 대기 실패 건별 처리, 예약 실행 캡
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


# ---------------------------------------------------------------------------
# 무상태 캡 산정
# ---------------------------------------------------------------------------


@dataclass
class ReplyLedger:
    """Threads 조회 결과로 재구성한 '오늘 내 답글' 장부."""

    used_today: int = 0
    author_today: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # (원글 ID, 저자) -> 기간 무관 누적 내 답글 수
    thread_author: dict[tuple[str, str], int] = field(
        default_factory=lambda: defaultdict(int)
    )


def build_ledger(
    conversations: dict[str, list[Comment]],
    my_post_ids: set[str],
    today: dt.date,
) -> ReplyLedger:
    """내 답글을 집계한다.

    셀프 리플라이(내 원글에 단 링크 답글)는 댓글 응답이 아니므로 제외한다.
    대상 댓글 작성자는 같은 대화 목록 안에서 replied_to.id 로 찾는다.
    """
    ledger = ReplyLedger()
    for post_id, comments in conversations.items():
        by_id = {c.id: c for c in comments}
        for c in comments:
            if not c.owned_by_me or not c.replied_to_id:
                continue
            if c.replied_to_id in my_post_ids:
                continue
            target = by_id.get(c.replied_to_id)
            author = target.username if target else ""

            if author:
                ledger.thread_author[(post_id, author)] += 1

            parsed = watchdog.parse_threads_timestamp(c.timestamp)
            if parsed and parsed.astimezone(KST).date() == today:
                ledger.used_today += 1
                if author:
                    ledger.author_today[author] += 1
    return ledger


def _within_scan_window(post: dict, now: dt.datetime) -> bool:
    """시각을 모르는 글은 제외하지 않는다(누락보다 과스캔이 안전)."""
    parsed = watchdog.parse_threads_timestamp(str(post.get("timestamp", "")))
    if parsed is None:
        return True
    return watchdog.hours_since(parsed, now) <= config.REPLY_SCAN_HOURS


# ---------------------------------------------------------------------------
# 스윕
# ---------------------------------------------------------------------------


def sweep(
    client: ThreadsClient,
    settings: Settings,
    *,
    per_run_cap: int,
    dry_run: bool,
    now: dt.datetime | None = None,
    budget_sec: float | None = None,
) -> int:
    """최근 글의 댓글에 답글한다. 발행(또는 DRY_RUN 계획) 건수를 돌려준다.

    budget_sec: 이 스윕에 쓸 수 있는 시간(초). 다음 답글의 최악 소요가 남은 시간을
    넘으면 새 답글을 시작하지 않는다(v1.2.0, job timeout 방지). None 이면 제한 없음.
    """
    started = time.monotonic()
    now = now or dt.datetime.now(dt.UTC)
    today = now.astimezone(KST).date()

    quota = client.get_reply_quota()
    log.info("답글 쿼터 %d/%d (잔여 %d)", quota.used, quota.total, quota.remaining)
    if quota.remaining < 1:
        log.warning("API 답글 쿼터 소진 — 종료")
        return 0

    # 서버 since 의 날짜 경계 해석(UTC/KST)이 문서에 없으므로 하루 더 앞당겨 받고,
    # 실제 범위 판정은 _within_scan_window 가 timestamp 로 한다.
    since = (
        now.astimezone(KST) - dt.timedelta(hours=config.REPLY_SCAN_HOURS, days=1)
    ).date()
    posts = [
        p for p in client.get_my_posts(config.REPLY_SCAN_POSTS, since=since)
        if _within_scan_window(p, now)
    ]
    log.info("대상 원글 %d건 (최근 %d시간)", len(posts), config.REPLY_SCAN_HOURS)
    if not posts:
        return 0

    my_post_ids = {str(p.get("id", "")) for p in posts if p.get("id")}
    post_texts = {str(p.get("id", "")): (p.get("text") or "").strip() for p in posts}

    conversations: dict[str, list[Comment]] = {}
    for post_id in my_post_ids:
        try:
            raw_items = client.get_conversation(post_id, config.REPLY_SCAN_LIMIT)
        except ThreadsApiError as exc:
            if exc.is_blocked:
                raise
            log.warning("대화 조회 실패 post=%s: %s", post_id, exc)
            continue
        conversations[post_id] = [_parse_comment(r) for r in raw_items]

    ledger = build_ledger(conversations, my_post_ids, today)
    log.info("오늘 내 답글 %d건 (일일 캡 %d)", ledger.used_today, config.REPLY_DAILY_CAP)

    cap = min(config.REPLY_DAILY_CAP - ledger.used_today, per_run_cap, quota.remaining)
    if cap <= 0:
        log.info("일일 캡 도달 — 이번 실행은 답글하지 않습니다.")
        return 0

    # 대상 추출. 이번 실행에서 계획한 건도 캡 계산에 포함한다.
    plans: list[tuple[str, reply_engine.ReplyDecision, str]] = []
    planned_author: dict[str, int] = defaultdict(int)
    planned_thread: dict[tuple[str, str], int] = defaultdict(int)

    for post_id, comments in conversations.items():
        replied = _already_replied_ids(comments)
        by_id = {c.id: c for c in comments}
        # v1.2.0: 응답 대상은 내 원글에 단 댓글, 또는 내 답글에 단 댓글뿐이다.
        # 제3자끼리 주고받는 대화에 끼어들지 않는다.
        reply_targets = my_post_ids | {c.id for c in comments if c.owned_by_me}

        for comment in comments:
            key = (post_id, comment.username)
            decision = reply_engine.decide(
                comment,
                already_replied=comment.id in replied,
                author_used=ledger.author_today[comment.username]
                + planned_author[comment.username],
                thread_author_count=ledger.thread_author[key] + planned_thread[key],
                reply_target_ids=reply_targets,
            )
            if decision.strategy is ReplyStrategy.SKIP:
                log.debug("스킵 %s — %s", comment.id, decision.reason)
                continue

            parent = by_id.get(comment.replied_to_id)
            parent_text = parent.text if (parent and parent.owned_by_me) else ""

            planned_author[comment.username] += 1
            planned_thread[key] += 1
            plans.append((post_texts.get(post_id, ""), decision, parent_text))

    log.info("응답 후보 %d건 / 이번 실행 상한 %d건", len(plans), cap)
    if not plans:
        return 0

    sent = 0
    attempted = 0
    for post_text, decision, parent_text in antibot.shuffled(plans):
        if not antibot.within_daily_cap(sent, cap, label="답글"):
            break
        if budget_sec is not None and not dry_run:
            worst = (
                (config.ANTIBOT_REPLY_JITTER[1] if attempted else 0)
                + config.CONTAINER_POLL_MAX_SEC + config.CONTAINER_WAIT_TEXT_SEC
                + config.REPLY_ITEM_MARGIN_SEC
            )
            elapsed = time.monotonic() - started
            if elapsed + worst > budget_sec:
                log.warning(
                    "스윕 시간 예산 소진 — 경과 %.0f초 + 최악 %d초 > 예산 %.0f초. 나머지는 다음 실행",
                    elapsed, worst, budget_sec,
                )
                break
        attempted += 1

        text = reply_engine.compose(
            decision, post_text, settings.claude_api_key, content.lint_reply,
            parent_reply_text=parent_text,
        )
        if not text:
            log.info("응답 생략 %s (%s)", decision.comment.id, decision.reason)
            continue

        if dry_run:
            log.info(
                "DRY_RUN 대상=%s(@%s) 방침=%s 대댓글=%s\n  댓글: %s\n  답글: %s",
                decision.comment.id, decision.comment.username,
                decision.strategy.value, "예" if parent_text else "아니오",
                decision.comment.text[:60], text,
            )
            sent += 1
            continue

        # 안티봇 — 답글 사이 랜덤 지연 (v1.2.0: 실패 건 다음에도 지연)
        if attempted > 1:
            antibot.jitter_sleep(*config.ANTIBOT_REPLY_JITTER, label="답글 간격")

        try:
            reply_id = client.publish_self_reply(decision.comment.id, text)
            log.info("답글 발행 완료 %s -> %s", decision.comment.id, reply_id)
            sent += 1
        except (ThreadsApiError, ContainerNotReadyError) as exc:
            # v1.2.0: 컨테이너 대기 실패(ContainerNotReadyError)는 ThreadsApiError 계열이
            # 아니라 스윕 전체가 중단됐다. 건별 실패로 처리하고 다음 건을 계속한다.
            if isinstance(exc, ThreadsApiError) and exc.is_blocked:
                raise
            log.error("답글 발행 실패 %s: %s", decision.comment.id, exc)
            notifier.send(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                f"[Threads Reply] 발행 실패 id={decision.comment.id}\n{exc}",
            )

    log.info("스윕 완료 — %s %d건", "계획" if dry_run else "발행", sent)
    return sent


def run() -> int:
    log.info("[ReplyEngine] v%s 시작", VERSION)

    if not config.REPLY_ENABLED:
        log.info("REPLY_ENABLED=false — 종료")
        return 0

    event = os.environ.get("EVENT_NAME", "").strip()
    slots = [s for s in os.environ.get("REPLY_SLOTS", "").split(",") if s.strip()]
    current_slot = os.environ.get("SLOT", "").strip()

    if event and event != "schedule":
        log.info("수동 실행(%s)", event)
    elif slots and current_slot and current_slot not in slots:
        # cron 문자열과 case 분기가 어긋난 상태. 조용히 돌면 안 된다.
        log.error(
            "슬롯 '%s' 이 등록 목록 %s 에 없습니다. "
            "cron 문자열과 Resolve slot case 분기를 확인하십시오.",
            current_slot, slots,
        )
        return 1   # v1.2.0: 설정 오류를 Actions 실패로 드러낸다(이전 0 은 녹색으로 가려짐)
    else:
        log.info("슬롯 %s 실행 (v1.1.0 부터 전 슬롯 실행)", current_slot or "-")

    settings = load_settings()

    token = _acquire_token(settings)
    user_id = settings.threads_user_id
    if user_id == "me":
        user_id, username = fetch_user_id(token)
        log.info("사용자 ID 조회 완료 — @%s", username)

    client = ThreadsClient(user_id, token)
    # v1.2.0: 일일 캡(20)을 실행당 상한으로 쓰면 답글 사이 지연(최대 150초 × 19)이
    # reply.yml timeout(20분)을 넘는다. 예약 실행 전용 상한을 둔다.
    sweep(
        client, settings,
        per_run_cap=config.REPLY_SCHEDULED_RUN_CAP, dry_run=settings.dry_run,
        budget_sec=config.REPLY_SWEEP_BUDGET_SEC,
    )
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

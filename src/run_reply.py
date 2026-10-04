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

v1.4.0 변경 (답글 고도화, DESIGN_V14_REPLY.md)
  - 대댓글 맥락: replied_to 사슬을 최대 REPLY_CONTEXT_TURNS 턴까지 대화로 넘긴다.
  - 반복 린트 대상: 이번 실행에서 만든 답글(최신순) + 같은 글의 내 답글(최신순, 링크 리플 제외).
    같은 목록 앞 5건을 프롬프트 '# 이미 쓴 답글'로도 넘긴다.
  - 외국어 정형 문구: 같은 글 대화에서 내가 이미 쓴 문구(이번 실행 계획분 포함)는 다시 쓰지 않는다.
  - 발행 실패한 답글은 '이번 실행에서 만든 답글'에 넣지 않는다(실제로 보이지 않으므로).

v1.6.0 변경 (계정 보호 모드, DESIGN_V16_SAFETY.md)
  - 킬 스위치(AUTOMATION_ENABLED=false)·워밍업이면 sweep 이 조회·생성·발행 없이 0 을 돌려준다.
  - 이어쓰기는 FOLLOWUP_ENABLED 에 더해 킬 스위치·워밍업에도 막힌다(safety.followups_allowed).
  - 캡 기본값 축소(일 10 · 저자 1 · 스레드 2 · 실행당 2/3)는 config 에서 한다.
  - 계정·토큰 사용 불가 오류(code 200 · 190 · HTTP 401)는 건별 실패로 넘기지 않고 즉시 전파한다.
    같은 실행에서 다음 답글을 시도하지 않는다(S7).
  - 링크 셀프 리플라이가 없는 글(LINK_REPLY_PCT=0)에서도 집계가 같다: 원글 직속 내 글은
    링크 유무와 무관하게 댓글 응답 집계에서 빠지고, 이어쓰기는 '링크 없는 원글 직속 내 글'로 판정한다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from . import antibot, config, content, notifier, reply_engine, safety, watchdog
from .env import MissingEnvError, Settings, load_settings
from .main import _acquire_token  # 토큰 확보 로직 재사용
from .reply_engine import Comment, DialogueTurn, ReplyStrategy
from .threads_client import (
    ContainerNotReadyError,
    ThreadsApiError,
    ThreadsClient,
    fetch_user_id,
)

VERSION = "1.5.0"   # v1.5.0: 계정 보호 모드(킬 스위치·워밍업·이어쓰기 차단·회로 차단기)
# v1.4.0: 대화 맥락 사슬, 답글 반복 린트, 외국어 문구 중복 회피
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


def my_reply_texts(comments: list[Comment]) -> list[str]:
    """대화 안의 내 글(답글·이어쓰기) 본문, 최신순. 링크 셀프 리플라이는 뺀다(v1.4.0).

    timestamp 문자열(ISO, 같은 형식)로 정렬한다. 없으면 대화 순서의 역순.
    """
    mine = [(i, c) for i, c in enumerate(comments) if c.owned_by_me and c.text.strip()]
    mine.sort(key=lambda pair: (pair[1].timestamp, pair[0]), reverse=True)
    return reply_engine.prior_reply_texts(c.text for _, c in mine)


@dataclass(frozen=True)
class ReplyPlan:
    """응답 후보 한 건(v1.4.0: 튜플 → 데이터클래스, 맥락 필드 추가)."""

    post_id: str
    post_text: str
    decision: reply_engine.ReplyDecision
    parent_text: str                       # 내 직전 답글(대댓글일 때). 하위 호환용
    dialogue: tuple[DialogueTurn, ...] = ()
    used_texts: tuple[str, ...] = ()       # 같은 글 대화에서 내가 이미 쓴 글 전부(링크 포함)


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
    allow_followup: bool = False,
) -> int:
    """최근 글의 댓글에 답글한다. 발행(또는 DRY_RUN 계획) 건수를 돌려준다.

    budget_sec: 이 스윕에 쓸 수 있는 시간(초). 다음 답글의 최악 소요가 남은 시간을
    넘으면 새 답글을 시작하지 않는다(v1.2.0, job timeout 방지). None 이면 제한 없음.
    allow_followup: v1.3.0 셀프 이어쓰기 수행 여부. 반환 건수에는 포함하지 않는다.
    """
    started = time.monotonic()
    now = now or dt.datetime.now(dt.UTC)
    today = now.astimezone(KST).date()

    # v1.6.0(S1·S5): 킬 스위치·워밍업이면 조회도 하지 않는다.
    blocked = safety.block_reason(safety.KIND_REPLY, today)
    if blocked:
        log.info("답글 스윕 생략 — %s", blocked)
        return 0

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
            if safety.is_account_fatal(exc):
                raise
            log.warning("대화 조회 실패 post=%s: %s", post_id, exc)
            continue
        conversations[post_id] = [_parse_comment(r) for r in raw_items]

    ledger = build_ledger(conversations, my_post_ids, today)
    log.info("오늘 내 답글 %d건 (일일 캡 %d)", ledger.used_today, config.REPLY_DAILY_CAP)

    sent = _reply_to_comments(
        client, settings, conversations, my_post_ids, post_texts, ledger, quota,
        per_run_cap=per_run_cap, dry_run=dry_run, started=started, budget_sec=budget_sec,
    )

    # v1.6.0(S6): FOLLOWUP_ENABLED 에 더해 킬 스위치·워밍업도 본다.
    followup_on = allow_followup and safety.followups_allowed(today)
    if allow_followup and config.FOLLOWUP_ENABLED and not followup_on:
        log.info("이어쓰기 생략 — %s", safety.block_reason(safety.KIND_FOLLOWUP, today))
    if followup_on and len(conversations) < len(my_post_ids):
        # 조회 실패한 대화에 오늘 이어쓰기가 있으면 일일 상한 집계가 틀어진다. 보수적으로 쉰다.
        log.warning("대화 조회 실패 %d건 — 이번 실행은 이어쓰기를 하지 않습니다.",
                    len(my_post_ids) - len(conversations))
    elif followup_on:
        _followups(
            client, settings, posts, conversations, now,
            dry_run=dry_run, started=started, budget_sec=budget_sec,
            reply_remaining=quota.remaining - sent,
        )

    log.info("스윕 완료 — %s %d건", "계획" if dry_run else "발행", sent)
    return sent


def _reply_to_comments(
    client: ThreadsClient,
    settings: Settings,
    conversations: dict[str, list[Comment]],
    my_post_ids: set[str],
    post_texts: dict[str, str],
    ledger: ReplyLedger,
    quota: Any,
    *,
    per_run_cap: int,
    dry_run: bool,
    started: float,
    budget_sec: float | None,
) -> int:
    """댓글 답글 본체(v1.2.0 sweep 후반부를 분리). 발행(또는 계획) 건수."""
    cap = min(config.REPLY_DAILY_CAP - ledger.used_today, per_run_cap, quota.remaining)
    if cap <= 0:
        log.info("일일 캡 도달 — 이번 실행은 답글하지 않습니다.")
        return 0

    # 대상 추출. 이번 실행에서 계획한 건도 캡 계산에 포함한다.
    plans: list[ReplyPlan] = []
    planned_author: dict[str, int] = defaultdict(int)
    planned_thread: dict[tuple[str, str], int] = defaultdict(int)
    # v1.4.0: 같은 글의 내 답글(최신순, 링크 리플 제외) — 반복 린트·'이미 쓴 답글'
    mine_by_post: dict[str, list[str]] = {}

    for post_id, comments in conversations.items():
        replied = _already_replied_ids(comments)
        by_id = {c.id: c for c in comments}
        mine_by_post[post_id] = my_reply_texts(comments)
        used_texts = tuple(c.text for c in comments if c.owned_by_me and c.text)
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
            plans.append(ReplyPlan(
                post_id=post_id,
                post_text=post_texts.get(post_id, ""),
                decision=decision,
                parent_text=parent_text,
                dialogue=reply_engine.build_dialogue(comment, by_id),
                used_texts=used_texts,
            ))

    log.info("응답 후보 %d건 / 이번 실행 상한 %d건", len(plans), cap)
    if not plans:
        return 0

    sent = 0
    attempted = 0
    # v1.4.0: 이번 실행에서 실제로 발행(또는 DRY_RUN 계획)한 답글. 최신이 앞.
    run_texts: list[str] = []
    run_texts_by_post: dict[str, list[str]] = defaultdict(list)
    for plan in antibot.shuffled(plans):
        decision = plan.decision
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
            decision, plan.post_text, settings.claude_api_key, content.lint_reply,
            parent_reply_text=plan.parent_text,
            dialogue=plan.dialogue,
            recent_replies=run_texts + mine_by_post.get(plan.post_id, []),
            used_texts=plan.used_texts + tuple(run_texts_by_post[plan.post_id]),
        )
        if not text:
            log.info("응답 생략 %s (%s)", decision.comment.id, decision.reason)
            continue

        if dry_run:
            log.info(
                "DRY_RUN 대상=%s(@%s) 방침=%s(%s) 맥락=%d턴\n  댓글: %s\n  답글: %s",
                decision.comment.id, decision.comment.username,
                decision.strategy.value, decision.reason, len(plan.dialogue),
                decision.comment.text[:60], text,
            )
            sent += 1
            run_texts.insert(0, text)
            run_texts_by_post[plan.post_id].append(text)
            continue

        # 안티봇 — 답글 사이 랜덤 지연 (v1.2.0: 실패 건 다음에도 지연)
        if attempted > 1:
            antibot.jitter_sleep(*config.ANTIBOT_REPLY_JITTER, label="답글 간격")

        try:
            reply_id = client.publish_self_reply(decision.comment.id, text)
            log.info("답글 발행 완료 %s -> %s", decision.comment.id, reply_id)
            sent += 1
            run_texts.insert(0, text)
            run_texts_by_post[plan.post_id].append(text)
        except (ThreadsApiError, ContainerNotReadyError) as exc:
            # v1.2.0: 컨테이너 대기 실패(ContainerNotReadyError)는 ThreadsApiError 계열이
            # 아니라 스윕 전체가 중단됐다. 건별 실패로 처리하고 다음 건을 계속한다.
            # v1.6.0(S7): 계정·토큰 사용 불가 오류는 건별 실패가 아니다. 즉시 전파(다음 건 시도 없음).
            if safety.is_account_fatal(exc):
                raise
            log.error("답글 발행 실패 %s: %s", decision.comment.id, exc)
            notifier.send(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                f"[Threads Reply] 발행 실패 id={decision.comment.id}\n{exc}",
            )

    log.info("댓글 답글 — %s %d건", "계획" if dry_run else "발행", sent)
    return sent
# ---------------------------------------------------------------------------
# 셀프 이어쓰기 (v1.3.0)
# ---------------------------------------------------------------------------


def _has_link(text: str) -> bool:
    return "http://" in text or "https://" in text


def is_followup_target(post_id: str) -> bool:
    """게시물 ID 해시로 대상 여부를 정한다(무상태·멱등)."""
    digest = hashlib.sha256(f"{post_id}::followup".encode()).hexdigest()[:8]
    return int(digest, 16) % 100 < config.FOLLOWUP_PCT


def _my_followups(post_id: str, comments: list[Comment]) -> list[Comment]:
    """원글 직속 내 답글 중 링크 셀프 리플라이가 아닌 것 = 이어쓰기."""
    return [
        c for c in comments
        if c.owned_by_me and c.replied_to_id == post_id and not _has_link(c.text)
    ]


def followup_count_today(conversations: dict[str, list[Comment]], today: dt.date) -> int:
    count = 0
    for post_id, comments in conversations.items():
        for c in _my_followups(post_id, comments):
            parsed = watchdog.parse_threads_timestamp(c.timestamp)
            if parsed and parsed.astimezone(KST).date() == today:
                count += 1
    return count


def followup_candidates(
    posts: list[dict],
    conversations: dict[str, list[Comment]],
    now: dt.datetime,
) -> list[tuple[str, str]]:
    """(원글 ID, 원글 본문) 목록. 대상 비율·경과 시간·기존 이어쓰기로 거른다.

    대화 조회에 실패한 글은 '이미 덧붙였는지' 판정 근거가 없으므로 제외한다.
    """
    result: list[tuple[str, str]] = []
    for post in posts:
        post_id = str(post.get("id", ""))
        text = (post.get("text") or "").strip()
        if not post_id or not text or post_id not in conversations:
            continue
        parsed = watchdog.parse_threads_timestamp(str(post.get("timestamp", "")))
        if parsed is None:
            continue
        age = watchdog.hours_since(parsed, now)
        if not config.FOLLOWUP_MIN_AGE_HOURS <= age <= config.FOLLOWUP_MAX_AGE_HOURS:
            continue
        if not is_followup_target(post_id):
            continue
        if _my_followups(post_id, conversations[post_id]):
            continue
        result.append((post_id, text))
    return result


def _followups(
    client: ThreadsClient,
    settings: Settings,
    posts: list[dict],
    conversations: dict[str, list[Comment]],
    now: dt.datetime,
    *,
    dry_run: bool,
    started: float,
    budget_sec: float | None,
    reply_remaining: int,
) -> int:
    """셀프 이어쓰기. 발행(또는 계획) 건수. 실패는 건별 처리."""
    today = now.astimezone(KST).date()
    used = followup_count_today(conversations, today)
    room = min(config.FOLLOWUP_DAILY_CAP - used, config.FOLLOWUP_PER_RUN, reply_remaining)
    if room <= 0:
        log.info("이어쓰기 상한 — 오늘 %d건 (상한 %d)", used, config.FOLLOWUP_DAILY_CAP)
        return 0

    candidates = antibot.shuffled(followup_candidates(posts, conversations, now))
    log.info("이어쓰기 후보 %d건 / 이번 실행 %d건", len(candidates), room)
    done = 0
    for post_id, post_text in candidates:
        if done >= room:
            break
        if budget_sec is not None and not dry_run:
            worst = (
                config.ANTIBOT_REPLY_JITTER[1] + config.CONTAINER_POLL_MAX_SEC
                + config.CONTAINER_WAIT_TEXT_SEC + config.REPLY_ITEM_MARGIN_SEC
            )
            if time.monotonic() - started + worst > budget_sec:
                log.warning("스윕 시간 예산 부족 — 이어쓰기는 다음 실행")
                break
        text = reply_engine.compose_followup(
            post_text, settings.claude_api_key, content.lint_chat
        )
        if not text:
            log.info("이어쓰기 생략 post=%s (생성 실패)", post_id)
            continue
        if dry_run:
            log.info("DRY_RUN 이어쓰기 post=%s\n  원글: %s\n  덧붙임: %s",
                     post_id, post_text[:60], text)
            done += 1
            continue
        antibot.jitter_sleep(*config.ANTIBOT_REPLY_JITTER, label="이어쓰기 전")
        try:
            reply_id = client.publish_self_reply(post_id, text)
            log.info("이어쓰기 발행 완료 %s -> %s", post_id, reply_id)
            done += 1
        except (ThreadsApiError, ContainerNotReadyError) as exc:
            if safety.is_account_fatal(exc):
                raise
            log.error("이어쓰기 발행 실패 %s: %s", post_id, exc)
            notifier.send(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                f"[Threads Reply] 이어쓰기 실패 post={post_id}\n{exc}",
            )
    return done


def run() -> int:
    log.info("[ReplyEngine] v%s 시작", VERSION)

    # v1.6.0(S1·S5): 킬 스위치·워밍업이면 토큰·Claude·Threads 호출 없이 종료.
    blocked = safety.block_reason(safety.KIND_REPLY)
    if blocked:
        log.info("답글 실행 생략 — %s", blocked)
        return 0

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

    # v1.3.0: 예약 실행은 시작 전 랜덤 지연. 답글이 매일 같은 슬롯 시각에 찍히지 않게 한다.
    #   예산(REPLY_SWEEP_BUDGET_SEC)은 sweep 시작부터 잰다 — timeout 40분에 지연 10분을 더 잡았다.
    if not event or event == "schedule":
        antibot.jitter_sleep(
            *config.REPLY_START_JITTER, label="답글 실행 시작", dry_run=settings.dry_run
        )

    # v1.2.0: 일일 캡을 실행당 상한으로 쓰면 답글 사이 지연이 timeout 을 넘는다.
    # 예약 실행 전용 상한을 둔다.
    sweep(
        client, settings,
        per_run_cap=config.REPLY_SCHEDULED_RUN_CAP, dry_run=settings.dry_run,
        budget_sec=config.REPLY_SWEEP_BUDGET_SEC,
        allow_followup=True,
    )
    return 0


def main() -> int:
    try:
        return run()
    except MissingEnvError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except ThreadsApiError as exc:
        if safety.is_account_fatal(exc):
            return safety.handle_fatal(exc, "답글", _notify_safe)
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

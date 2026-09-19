"""CHAT 발행 계획 — 무상태·멱등 판정.

상태를 저장하지 않는다. 날짜 시드로 "오늘 몇 건, 어느 트리거에서"를 정하고,
실제 발행 여부는 Threads 에서 조회한 '오늘 CHAT 창 게시물 수'와 비교해 판정한다.

  N       = CHAT_DAILY_MIN ~ CHAT_DAILY_MAX 중 날짜 시드로 1개
  선택    = 트리거 T1~T9 중 N 개를 날짜 시드로 선택
  허용(k) = 오늘 CHAT 수 < (Tk 이하 선택 트리거 수)

같은 트리거를 재실행하면 이미 허용치에 도달해 있으므로 스킵된다(멱등).
앞 트리거의 cron 이 누락되면 다음 트리거(선택 여부 무관)에서 1건씩 보충된다.
  v1.0.1: 보충을 '다음 선택 트리거'에서 '다음 트리거'로 넓혔다. 선택 트리거만
  보충하면 누락분이 끝까지 회복되지 않는다(전수 테스트 시뮬레이션에서 확인).

게시물 분류
  본문에 표식을 넣지 않는다. KST CHAT_WINDOW_START ~ CHAT_WINDOW_END 창에
  발행된 글을 CHAT 으로 본다. 정기·이벤트 슬롯과 창이 겹치지 않는다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import config

VERSION = "1.0.1"

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

MANUAL = "MANUAL"


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _seed(today: dt.date, salt: str) -> int:
    raw = f"{today.isoformat()}::{config.CHAT_SALT}::{salt}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def is_chat_time(when: dt.datetime) -> bool:
    """KST 기준 CHAT 창 안인지. 창 = [START, END)."""
    local = when.astimezone(KST)
    minute = local.hour * 60 + local.minute
    return _minutes(config.CHAT_WINDOW_START) <= minute < _minutes(config.CHAT_WINDOW_END)


def daily_bounds() -> tuple[int, int]:
    """설정값을 트리거 수 범위로 보정한다. 잘못된 설정이 폭주로 이어지지 않게 한다."""
    total = len(config.CHAT_TRIGGERS)
    low = max(0, min(config.CHAT_DAILY_MIN, total))
    high = max(low, min(config.CHAT_DAILY_MAX, total))
    return low, high


def daily_target(today: dt.date) -> int:
    low, high = daily_bounds()
    return random.Random(_seed(today, "n")).randint(low, high)


def selected_triggers(today: dt.date) -> tuple[int, ...]:
    """오늘 발행하는 트리거 번호(1부터). 오름차순."""
    n = daily_target(today)
    indices = range(1, len(config.CHAT_TRIGGERS) + 1)
    picked = random.Random(_seed(today, "pick")).sample(list(indices), n)
    return tuple(sorted(picked))


def trigger_number(raw: str) -> int | None:
    """'T3' -> 3. 수동 실행이나 알 수 없는 값은 None."""
    value = (raw or "").strip().upper()
    if not value.startswith("T") or not value[1:].isdigit():
        return None
    number = int(value[1:])
    if not 1 <= number <= len(config.CHAT_TRIGGERS):
        return None
    return number


@dataclass(frozen=True)
class PostCounts:
    chat_today: int
    last_post_at: dt.datetime | None


def count_posts(posts: list[dict], now: dt.datetime, parse) -> PostCounts:
    """오늘(KST) CHAT 창 게시물 수와 마지막 게시물 시각.

    parse 는 Threads 타임스탬프 파서(watchdog.parse_threads_timestamp).
    순환 import 를 피하려 주입받는다.
    """
    today = now.astimezone(KST).date()
    chat_today = 0
    stamps: list[dt.datetime] = []
    for post in posts:
        parsed = parse(str(post.get("timestamp", "")))
        if parsed is None:
            continue
        stamps.append(parsed)
        if parsed.astimezone(KST).date() == today and is_chat_time(parsed):
            chat_today += 1
    return PostCounts(chat_today, max(stamps) if stamps else None)


def gate(
    today: dt.date,
    now: dt.datetime,
    trigger: int | None,
    counts: PostCounts,
    *,
    manual: bool,
    enforce_gap: bool = True,
) -> str | None:
    """발행을 막아야 하는 사유. 없으면 None.

    enforce_gap=False 는 사전 판정용이다. 간격 부족은 gap_wait_seconds() 로
    대기 시간을 계산해 지연 후 발행하고, 발행 직전 재판정(enforce_gap=True)에서
    최종 확인한다.
    """
    if not is_chat_time(now):
        return f"CHAT 창({config.CHAT_WINDOW_START}~{config.CHAT_WINDOW_END}) 밖입니다."

    target = daily_target(today)
    if counts.chat_today >= target:
        return f"오늘 CHAT {counts.chat_today}건 — 목표 {target}건 도달."

    if not manual:
        if trigger is None:
            return "트리거 번호를 알 수 없습니다. chat.yml case 분기를 확인하십시오."
        # 선택 여부와 무관하게 '이 시점까지 나갔어야 할 건수'로 판정한다.
        #   선택 트리거: 정상이면 허용-1 건 상태 → 발행
        #   미선택 트리거: 정상이면 허용 건수와 같음 → 보류. 앞선 cron 누락으로
        #   밀려 있으면 1건 보충한다(실행당 1건이라 몰아 내지 않는다).
        selected = selected_triggers(today)
        allowed = sum(1 for t in selected if t <= trigger)
        if counts.chat_today >= allowed:
            tag = "선택" if trigger in selected else "미선택"
            return (
                f"T{trigger}({tag}) 까지 허용 {allowed}건 — 이미 {counts.chat_today}건 발행."
            )

    if enforce_gap and counts.last_post_at is not None:
        gap_min = (now - counts.last_post_at).total_seconds() / 60
        if gap_min < config.CHAT_MIN_GAP_MIN:
            return f"직전 게시물 후 {gap_min:.0f}분 — 최소 간격 {config.CHAT_MIN_GAP_MIN}분 미만."

    return None


def gap_wait_seconds(counts: PostCounts, now: dt.datetime) -> int:
    """최소 간격을 채우려면 몇 초 더 기다려야 하는지. 충분하면 0."""
    if counts.last_post_at is None:
        return 0
    elapsed = (now - counts.last_post_at).total_seconds()
    return max(0, int(config.CHAT_MIN_GAP_MIN * 60 - elapsed))


def jitter_range(wait_sec: int) -> tuple[int, int]:
    """발행 전 지연 범위. 간격 대기가 필요하면 그만큼 하한을 올린다."""
    low, high = config.CHAT_JITTER
    if wait_sec <= 0:
        return low, high
    low = max(low, wait_sec + 30)   # 30초 여유: 재조회·컨테이너 생성 시간
    return low, max(high, low + 60)


def source_for(today: dt.date, trigger: int | None, mode: str) -> str:
    """이 실행이 먼저 쓸 근거 소스. mix 면 날짜·트리거 시드로 rss/web 중 하나."""
    if mode in ("rss", "web", "none"):
        return mode
    key = f"{trigger or 0}"
    return "rss" if _seed(today, f"src-{key}") % 2 == 0 else "web"

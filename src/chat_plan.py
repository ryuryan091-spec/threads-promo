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

VERSION = "1.1.0"   # v1.1.0: 주말 목표, 트리거별 소재 비복원 배정, 마무리 비율

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


# 공식 문서(Threads Media)에서 확인된 텍스트 게시물 media_type 값.
CHAT_MEDIA_TYPE = "TEXT_POST"


def is_chat_post(posted_at: dt.datetime, media_type: str = "") -> bool:
    """게시물이 CHAT 인지. 시간창 + 형식(텍스트)으로 판정한다.

    v1.0.2: 정기 발행(이미지)이 cron 지연으로 창 안에 들어와도 CHAT 으로
    오분류하지 않도록 media_type 을 함께 본다. media_type 이 비어 있으면
    (조회 필드 누락 등) 기존처럼 시간창만으로 판정한다.
    남는 한계: 이미지 실패로 텍스트 폴백된 정기 글이 창 안이면 여전히 CHAT 으로 본다.
    """
    if not is_chat_time(posted_at):
        return False
    return not media_type or media_type == CHAT_MEDIA_TYPE


def is_chat_post_dict(post: dict, parse) -> bool:
    """API 게시물 dict 판정. parse 는 watchdog.parse_threads_timestamp."""
    parsed = parse(str(post.get("timestamp", "")))
    return bool(parsed) and is_chat_post(parsed, str(post.get("media_type") or ""))


def is_weekend(today: dt.date) -> bool:
    """토·일(KST 날짜 기준)."""
    return today.weekday() >= 5


def daily_bounds(today: dt.date | None = None) -> tuple[int, int]:
    """설정값을 트리거 수 범위로 보정한다. 잘못된 설정이 폭주로 이어지지 않게 한다.

    v1.1.0: today 가 주말이면 CHAT_WEEKEND_MIN/MAX 를 쓴다. None 이면 평일 값.
    """
    total = len(config.CHAT_TRIGGERS)
    if today is not None and is_weekend(today):
        raw_low, raw_high = config.CHAT_WEEKEND_MIN, config.CHAT_WEEKEND_MAX
    else:
        raw_low, raw_high = config.CHAT_DAILY_MIN, config.CHAT_DAILY_MAX
    low = max(0, min(raw_low, total))
    high = max(low, min(raw_high, total))
    return low, high


def daily_target(today: dt.date) -> int:
    low, high = daily_bounds(today)
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
        media_type = str(post.get("media_type") or "")
        if parsed.astimezone(KST).date() == today and is_chat_post(parsed, media_type):
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


def seed_for(today: dt.date, trigger: int | None, seeds: tuple[str, ...]) -> str:
    """이 트리거의 소재. 날짜 시드로 소재 순서를 섞고 트리거 번호로 꺼낸다.

    v1.1.0: 트리거마다 독립 추첨하면 같은 날 소재가 겹친다(10개 중 6건 추첨 시
    중복 확률 84.9%). 비복원 배정으로 소재 수 이하의 발행에서는 중복이 없다.
    수동 실행(trigger=None)은 트리거 다음 칸을 쓴다.
    """
    if not seeds:
        raise ValueError("소재 목록이 비어 있습니다.")
    order = random.Random(_seed(today, "seed")).sample(range(len(seeds)), len(seeds))
    pos = (trigger - 1) if trigger else len(config.CHAT_TRIGGERS)
    return seeds[order[pos % len(seeds)]]


def closing_for(today: dt.date, trigger: int | None) -> str:
    """이 트리거 글의 마무리 방식. 'question' | 'statement' (날짜·트리거 결정론)."""
    key = f"close-{trigger or 0}"
    return "question" if _seed(today, key) % 100 < config.CHAT_QUESTION_PCT else "statement"

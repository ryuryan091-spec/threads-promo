"""CHAT 발행 계획 — 무상태·멱등 판정.

상태를 저장하지 않는다. 날짜 시드로 "오늘 몇 건, 어느 트리거에서"를 정하고,
실제 발행 여부는 Threads 에서 조회한 '오늘 CHAT 구역 게시물 수'와 비교해 판정한다.

  N       = CHAT_DAILY_MIN ~ CHAT_DAILY_MAX 중 날짜 시드로 1개
  선택    = 트리거 T1~T15 중 N 개를 날짜 시드로 선택 (v1.4.0: 9개 → 15개)
  허용(k) = 오늘 CHAT 수 < (Tk 이하 선택 트리거 수)

같은 트리거를 재실행하면 이미 허용치에 도달해 있으므로 스킵된다(멱등).
앞 트리거의 cron 이 누락되면 다음 트리거(선택 여부 무관)에서 1건씩 보충된다.
  v1.0.1: 보충을 '다음 선택 트리거'에서 '다음 트리거'로 넓혔다. 선택 트리거만
  보충하면 누락분이 끝까지 회복되지 않는다(전수 테스트 시뮬레이션에서 확인).

게시물 분류
  본문에 표식을 넣지 않는다. KST CHAT_WINDOW_START ~ CHAT_WINDOW_END 창 안의
  'CHAT 구역'(chat_zones)에 발행된 텍스트 글을 CHAT 으로 본다.
  v1.5.0: 창이 09:00~24:00 으로 넓어져 정기·이벤트 판정 창이 창 안에 들어온다.
    예약 구간 = 정기 판정 창 [t, t+PUBLISH_CLASSIFY_WINDOW_MIN] · 이벤트 판정 창
    [t, t+EVENT_CLASSIFY_WINDOW_MIN] 에 앞 여유 CHAT_RESERVED_MARGIN_MIN 분을 더한 구간.
    CHAT 구역 = 창 − 예약 구간 중 CHAT_MIN_ZONE_MIN 분 이상인 구간.
    예약 구간이 항상 CHAT 보다 우선한다(텍스트 폴백된 정기 글은 CHAT 이 아니다).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import config

VERSION = "1.5.0"   # v1.5.0: 창 09:00~24:00 · 예약 구간 제외 CHAT 구역 · 시간대(band) 소재
# v1.4.0: 트리거 15개·평일 5~15건·간격 10분·지연 30~300초 (상수는 config)

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

MANUAL = "MANUAL"
DAY_MINUTES = 24 * 60
# v1.5.0: 발행 직전 재판정에서 구역 끝까지 남아 있어야 하는 초(컨테이너 생성·5초 대기·발행).
PUBLISH_MIN_ROOM_SEC = 30
# v1.5.0: 트리거 시각 뒤 구역 잔여 하한(분). 준비 ~1.5 + 생성 ~1 + 지연 하한 1 + 종료 여유 1.5
#   + 정상 cron 지연 0~5분 ≈ 10분에 지연 분산 몫 5분을 더했다. verify_repo 10번이 검사한다.
TRIGGER_MIN_ROOM_MIN = 15


def _minutes(hhmm: str) -> int:
    """'HH:MM' → 자정 기준 분. '24:00' 은 1440(창 끝 전용)."""
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _seed(today: dt.date, salt: str) -> int:
    raw = f"{today.isoformat()}::{config.CHAT_SALT}::{salt}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def _minute_of_day(when: dt.datetime) -> int:
    local = when.astimezone(KST)
    return local.hour * 60 + local.minute


# ---------------------------------------------------------------------------
# v1.5.0: 예약 구간 · CHAT 구역
#   구간은 모두 [시작, 끝) 분 단위(자정 기준). 판정 창은 분 단위 양끝 포함
#   (insights.discriminator_from_timestamp: 경과 분 ≤ 창)이므로 끝 = t + 창 + 1.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reserved:
    label: str
    start: int   # 여유 포함 시작(분). 자정 전으로 넘어가면 음수가 아니라 % 1440 로 접는다.
    end: int     # 끝(분, 미포함). 자정을 넘으면 1440 보다 클 수 있다.


def reserved_windows() -> tuple[Reserved, ...]:
    """정기·이벤트 판정 창 + 앞 여유. 단일 출처: config.PUBLISH_SLOT_TIMES·EVENT_SLOT_TIMES."""
    margin = config.CHAT_RESERVED_MARGIN_MIN
    out: list[Reserved] = []
    for slot, hhmm in config.PUBLISH_SLOT_TIMES.items():
        t = _minutes(hhmm)
        out.append(Reserved(f"정기 {slot} {hhmm}", t - margin,
                            t + config.PUBLISH_CLASSIFY_WINDOW_MIN + 1))
    for hhmm in config.EVENT_SLOT_TIMES:
        t = _minutes(hhmm)
        out.append(Reserved(f"이벤트 {hhmm}", t - margin,
                            t + config.EVENT_CLASSIFY_WINDOW_MIN + 1))
    return tuple(out)


def _reserved_spans() -> list[tuple[int, int]]:
    """예약 구간을 [0, 1440) 안의 조각으로 접어 시작 순으로 돌려준다(자정 넘김 분할)."""
    spans: list[tuple[int, int]] = []
    for r in reserved_windows():
        start, end = r.start, r.end
        while start < 0:
            start, end = start + DAY_MINUTES, end + DAY_MINUTES
        if end <= DAY_MINUTES:
            spans.append((start, end))
        else:
            spans.append((start, DAY_MINUTES))
            spans.append((0, end - DAY_MINUTES))
    return sorted(spans)


def free_zones() -> tuple[tuple[int, int], ...]:
    """CHAT 창 안에서 예약 구간을 뺀 빈 구간 전부 [시작, 끝) 분. 길이 무관."""
    cursor, end = _minutes(config.CHAT_WINDOW_START), _minutes(config.CHAT_WINDOW_END)
    zones: list[tuple[int, int]] = []
    for r_start, r_end in _reserved_spans():
        if r_end <= cursor:
            continue
        if r_start >= end:
            break
        if r_start > cursor:
            zones.append((cursor, r_start))
        cursor = max(cursor, r_end)
    if cursor < end:
        zones.append((cursor, end))
    return tuple(zones)


def chat_zones() -> tuple[tuple[int, int], ...]:
    """CHAT 구역: 빈 구간 중 CHAT_MIN_ZONE_MIN 분 이상. 발행·판정 모두 이 구역만 쓴다."""
    return tuple(z for z in free_zones() if z[1] - z[0] >= config.CHAT_MIN_ZONE_MIN)


def zone_at(when: dt.datetime) -> tuple[int, int] | None:
    """when(KST 날짜 기준)이 속한 CHAT 구역. 없으면 None."""
    minute = _minute_of_day(when)
    for zone in chat_zones():
        if zone[0] <= minute < zone[1]:
            return zone
    return None


def seconds_left_in_zone(when: dt.datetime) -> int | None:
    """when 이 속한 CHAT 구역 끝까지 남은 초. 구역 밖이면 None."""
    zone = zone_at(when)
    if zone is None:
        return None
    local = when.astimezone(KST)
    end = dt.datetime.combine(local.date(), dt.time(0), tzinfo=KST) + dt.timedelta(minutes=zone[1])
    return int((end - local).total_seconds())


def is_chat_time(when: dt.datetime) -> bool:
    """KST 기준 CHAT 구역 안인지.

    v1.5.0: 창 [START, END) 안이면서 정기·이벤트 예약 구간 밖이고, 그 빈 구간이
    CHAT_MIN_ZONE_MIN 분 이상일 때만 True.
    """
    return zone_at(when) is not None


def fmt_minutes(minute: int) -> str:
    """분 → 'HH:MM'. 1440 은 '24:00'."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def zones_label() -> str:
    return ", ".join(f"{fmt_minutes(a)}~{fmt_minutes(b)}" for a, b in chat_zones())


# ---------------------------------------------------------------------------
# v1.5.0: 시간대(band) — 프롬프트 맥락과 소재 풀
# ---------------------------------------------------------------------------


def band_of_minute(minute: int) -> str:
    """분 → 시간대 이름(config.CHAT_TIME_BANDS). 첫 시작 이전이면 첫 시간대."""
    name = config.CHAT_TIME_BANDS[0][0]
    for band, start in config.CHAT_TIME_BANDS:
        if minute >= _minutes(start):
            name = band
    return name


def band_for(trigger: int | None, now: dt.datetime) -> str:
    """이 실행의 시간대. 예약 실행은 트리거 예정 시각, 수동 실행은 현재 시각 기준.

    트리거 예정 시각을 쓰는 이유: 같은 트리거 재실행·cron 지연에도 소재·맥락이 같아야 한다(멱등).
    """
    if trigger:
        return band_of_minute(_minutes(config.CHAT_TRIGGERS[trigger - 1]))
    return band_of_minute(_minute_of_day(now))


def band_triggers(band: str) -> tuple[int, ...]:
    """이 시간대에 속한 트리거 번호(오름차순)."""
    return tuple(
        i for i, hhmm in enumerate(config.CHAT_TRIGGERS, start=1)
        if band_of_minute(_minutes(hhmm)) == band
    )


# 공식 문서(Threads Media)에서 확인된 텍스트 게시물 media_type 값.
CHAT_MEDIA_TYPE = "TEXT_POST"


def is_chat_post(posted_at: dt.datetime, media_type: str = "") -> bool:
    """게시물이 CHAT 인지. 시간창 + 형식(텍스트)으로 판정한다.

    v1.0.2: 정기 발행(이미지)이 cron 지연으로 창 안에 들어와도 CHAT 으로
    오분류하지 않도록 media_type 을 함께 본다. media_type 이 비어 있으면
    (조회 필드 누락 등) 기존처럼 시간창만으로 판정한다.
    v1.5.0: 정기·이벤트 판정 창(+앞 여유)은 CHAT 구역에서 빠지므로, 판정 창 안에서
    텍스트로 폴백된 정기·이벤트 글은 CHAT 으로 보지 않는다(예약 구간 우선).
    남는 한계
      - 정기·이벤트 글이 cron 지연으로 판정 창(정기 +47분·이벤트 +90분)을 넘겨 CHAT 구역에
        텍스트로 발행되면 CHAT 으로 센다(이미지면 media_type 으로 걸러진다).
      - 정기 발행을 수동 실행(workflow_dispatch)해 CHAT 구역에서 텍스트로 나가면 CHAT 으로 센다.
      - media_type 이 비면 시간만으로 판정한다.
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


def due_opportunities(today: dt.date, now: dt.datetime, grace_min: int) -> int:
    """now 까지 'CHAT 이 나갈 수 있었던' 트리거 수(워치독용, v1.5.0).

    첫 선택 트리거부터 그 뒤 모든 트리거(미선택 포함 — 밀려 있으면 1건 보충)가 발행 기회다.
    트리거 예정 시각 + grace_min(cron 지연·실행 소요 여유)이 now 이하인 것만 센다.
    오늘 선택 트리거가 없으면(목표 0) 0.
    """
    selected = selected_triggers(today)
    if not selected:
        return 0
    local_date = now.astimezone(KST).date()
    if local_date < today:
        return 0
    minute = _minute_of_day(now) if local_date == today else DAY_MINUTES
    return sum(
        1 for i, hhmm in enumerate(config.CHAT_TRIGGERS, start=1)
        if i >= selected[0] and _minutes(hhmm) + grace_min <= minute
    )


@dataclass(frozen=True)
class PostCounts:
    chat_today: int
    last_post_at: dt.datetime | None


def count_posts(posts: list[dict], now: dt.datetime, parse) -> PostCounts:
    """오늘(KST) CHAT 구역 게시물 수와 마지막 게시물 시각(종류 무관).

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
    v1.5.0: 발행 직전 재판정은 CHAT 구역 끝까지 PUBLISH_MIN_ROOM_SEC 초 이상 남았을 때만
    통과시킨다. 구역 끝이 하루 5번(이전 1번) 생겨, 재판정 통과 후 컨테이너 생성·발행
    몇 초 사이에 구역을 넘는 경계 발행(CHAT 으로 세지 않음 → 목표 초과)이 늘기 때문이다.
    """
    if not is_chat_time(now):
        return f"CHAT 구역({zones_label()}) 밖입니다."
    if enforce_gap:
        left = seconds_left_in_zone(now)
        if left is not None and left < PUBLISH_MIN_ROOM_SEC:
            return f"CHAT 구역 종료까지 {left}초 — 발행 여유 {PUBLISH_MIN_ROOM_SEC}초 미만."

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


# 구역 종료 전 남겨 둘 여유(초): 재검증 조회 + 컨테이너 생성. (v1.5.0: 창 끝 → 구역 끝)
WINDOW_END_MARGIN_SEC = 90


def seconds_until_next_trigger(now: dt.datetime) -> int | None:
    """now(KST) 이후 가장 가까운 CHAT 트리거까지 남은 초. 오늘 남은 트리거가 없으면 None.

    v1.4.0: CHAT 실행의 답글 스윕 예산을 다음 트리거 전으로 자르는 데 쓴다.
    """
    local = now.astimezone(KST)
    for hhmm in config.CHAT_TRIGGERS:
        at = dt.datetime.combine(local.date(), dt.time.fromisoformat(hhmm), tzinfo=KST)
        if at > local:
            return int((at - local).total_seconds())
    return None


def jitter_range(wait_sec: int, now: dt.datetime | None = None) -> tuple[int, int]:
    """발행 전 지연 범위. 간격 대기가 필요하면 그만큼 하한을 올린다.

    v1.2.0: now 를 주면 상한을 'CHAT 창 종료 - 여유'로 자른다. 지연 상한(당시 540초)이
    마지막 트리거(당시 11:52)에서 창 종료(12:05)를 넘겨 재검증에서 버려지는 것을 막는다.
    v1.4.0: 지연 상한 300초·마지막 트리거 11:50 — 정시 실행이면 자르지 않지만,
    cron 지연으로 늦게 시작한 실행은 여전히 이 상한이 적용된다.
    v1.5.0: 창 종료 대신 '지금 속한 CHAT 구역의 끝 - 여유'로 자른다. 구역 끝 = 다음 예약
    구간(정기·이벤트 판정 창 - 앞 여유) 시작 또는 창 끝(24:00). now 가 구역 밖이면 상한 = 하한
    (사전 게이트가 이미 막고, 재검증이 다시 막는다).
    하한(간격 대기)은 자르지 않는다 — 간격이 모자라면 재검증이 보류한다.
    """
    low, high = config.CHAT_JITTER
    if wait_sec > 0:
        low = max(low, wait_sec + 30)   # 30초 여유: 재조회·컨테이너 생성 시간
        high = max(high, low + 60)
    if now is not None:
        left = seconds_left_in_zone(now)
        room = 0 if left is None else left - WINDOW_END_MARGIN_SEC
        high = max(low, min(high, room))
    return low, high


def source_for(today: dt.date, trigger: int | None, mode: str) -> str:
    """이 실행이 먼저 쓸 근거 소스. mix 면 날짜·트리거 시드로 rss/web 중 하나."""
    if mode in ("rss", "web", "none"):
        return mode
    key = f"{trigger or 0}"
    return "rss" if _seed(today, f"src-{key}") % 2 == 0 else "web"


def seed_for(
    today: dt.date, trigger: int | None, seeds: tuple[str, ...], band: str | None = None
) -> str:
    """이 트리거의 소재. 날짜 시드로 소재 순서를 섞고 트리거 번호로 꺼낸다.

    v1.1.0: 트리거마다 독립 추첨하면 같은 날 소재가 겹친다(10개 중 6건 추첨 시
    중복 확률 84.9%). 비복원 배정으로 소재 수 이하의 발행에서는 중복이 없다.
    v1.4.0: 트리거 15개 + 수동 1칸 = 16칸 ≤ 소재 18개라 하루 최대 15건에서도 겹치지 않는다.
    수동 실행(trigger=None)은 트리거 다음 칸을 쓴다.
    v1.5.0: band 를 주면 그 시간대 소재 풀 안에서 배정한다. 순서는 날짜·시간대 시드로 섞고,
    칸 = 그 시간대 트리거 중 몇 번째인지(수동은 시간대 트리거 수 = 다음 칸).
    시간대 풀 크기 ≥ 시간대 트리거 수 + 1 이면 하루 안에서 시간대별로 겹치지 않는다
    (tests/test_chat_v15.py). band=None 은 이전 동작(트리거 번호 칸) 그대로다.
    """
    if not seeds:
        raise ValueError("소재 목록이 비어 있습니다.")
    if band is None:
        order = random.Random(_seed(today, "seed")).sample(range(len(seeds)), len(seeds))
        pos = (trigger - 1) if trigger else len(config.CHAT_TRIGGERS)
        return seeds[order[pos % len(seeds)]]
    order = random.Random(_seed(today, f"seed-{band}")).sample(range(len(seeds)), len(seeds))
    members = band_triggers(band)
    pos = members.index(trigger) if trigger in members else len(members)
    return seeds[order[pos % len(seeds)]]


# v1.2.0: 질문이 아닌 나머지 몫을 단정·여운·혼잣말로 나누는 비율(합 100).
_NON_QUESTION_SPLIT = (("statement", 45), ("trail", 30), ("aside", 25))


def closing_for(today: dt.date, trigger: int | None) -> str:
    """이 트리거 글의 마무리 방식 (날짜·트리거 결정론).

    'question' 이 CHAT_QUESTION_PCT%, 나머지는 statement / trail / aside.
    """
    key = f"close-{trigger or 0}"
    if _seed(today, key) % 100 < config.CHAT_QUESTION_PCT:
        return "question"
    point = _seed(today, f"{key}-kind") % 100
    for name, weight in _NON_QUESTION_SPLIT:
        if point < weight:
            return name
        point -= weight
    return "statement"


def style_key(today: dt.date, trigger: int | None) -> str:
    """CHAT 문체 축 키. 같은 트리거 재실행은 같은 형식(멱등)."""
    return f"{today.isoformat()}::chat::{trigger or 0}"

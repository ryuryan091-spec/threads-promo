"""v1.7.0 숏폼 발행 계획 — 무상태·멱등 순수 함수 모음 (DESIGN_V17_SHORTS.md).

같은 날짜로 다시 계산하면 같은 결과가 나온다(날짜 시드). DB 를 두지 않는다.

  face_daily_target  : 오늘 Facebook 릴스 편수(램프 FACE_RAMP_START · FACE_DAILY_MAX)
  daily_plan         : 오늘 만들 영상 목록(포맷·채널)
  publish_schedule   : 승인 시각부터 각 편의 게시 시각(게시 시간대 · 편간 간격 · job 예산)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import antibot, config, safety

VERSION = "1.0.0"   # v1.7.0 신규

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 포맷 (DESIGN_V17_SHORTS.md §4). 하루 여러 편이면 서로 다른 포맷을 쓴다.
FORMAT_SAGA = "F1"       # EDT 시장 서사
FORMAT_CONCEPT = "F2"    # 개념 해설
FORMAT_WEEK = "F3"       # 이번 주 볼 것(일정)
FORMATS: tuple[str, ...] = (FORMAT_SAGA, FORMAT_CONCEPT, FORMAT_WEEK)

CHANNEL_FACE = "face"
CHANNEL_THREADS = "threads"


def ramp_start() -> dt.date | None:
    """FACE_RAMP_START 를 날짜로. 비었거나 형식 오류면 None(=0편, 안전 측)."""
    raw = (config.FACE_RAMP_START or "").strip()
    if not raw or not _DATE_RE.fullmatch(raw):
        if raw:
            log.warning("FACE_RAMP_START=%r 를 날짜(YYYY-MM-DD)로 읽을 수 없습니다 — Facebook 0편", raw)
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        log.warning("FACE_RAMP_START=%r 는 존재하지 않는 날짜입니다 — Facebook 0편", raw)
        return None


def face_daily_target(today: dt.date) -> int:
    """오늘 Facebook 릴스 편수. 시작 전·미설정은 0. 상한 FACE_DAILY_MAX(0~3 으로 제한)."""
    start = ramp_start()
    if start is None or today < start:
        return 0
    weeks = (today - start).days // 7
    per_day = config.FACE_RAMP_STEPS[-1][1]
    for limit_weeks, count in config.FACE_RAMP_STEPS:
        if weeks < limit_weeks:
            per_day = count
            break
    cap = max(0, min(int(config.FACE_DAILY_MAX), len(FORMATS)))
    return min(per_day, cap)


def is_rest_day(today: dt.date) -> bool:
    """숏폼 주간 휴식일. 정기 발행과 다른 솔트라 쉬는 요일이 겹치지 않을 수 있다."""
    return antibot.is_rest_day(today, config.SHORTS_WEEKLY_REST_DAYS, config.SHORTS_REST_SALT)


@dataclass(frozen=True)
class PlannedVideo:
    content_id: str          # sv-YYYYMMDD-N (N 은 1부터)
    index: int               # 0부터
    fmt: str                 # F1 | F2 | F3
    channels: tuple[str, ...]
    character: str = config.CHARACTER_EDT   # Facebook 에 가는 편은 GOC(config.FACE_CHARACTER)


def _seed(today: dt.date, salt: str) -> int:
    return int(hashlib.sha256(f"{today.isoformat()}::shorts::{salt}".encode()).hexdigest()[:8], 16)


def formats_for(today: dt.date, n: int) -> list[str]:
    """오늘 n 편의 포맷. 첫 편은 항상 F1, 나머지는 F2·F3 를 날짜 시드로 섞는다."""
    if n <= 0:
        return []
    rest = list(FORMATS[1:])
    random.Random(_seed(today, "fmt")).shuffle(rest)
    return [FORMAT_SAGA, *rest][:n]


def daily_plan(today: dt.date) -> list[PlannedVideo]:
    """오늘 만들 영상 목록. 생성 비용이 드는 판단이라 '게시할 채널이 있는 편'만 만든다.

    - Facebook: face_daily_target 편 (FACE_ENABLED · 킬 스위치 통과 시)
    - Threads : 첫 편만(하루 최대 1편), SHORTS_THREADS_ENABLED · 킬 스위치 · 워밍업 통과 시.
                하루 총량(DAILY_POST_BUDGET)은 게시 직전에 다시 본다.
    휴식일·SHORTS_BUILD_ENABLED=false 면 빈 목록.
    """
    if not config.SHORTS_BUILD_ENABLED:
        return []
    if is_rest_day(today):
        return []
    face_n = face_daily_target(today) if safety.face_block_reason() is None else 0
    threads_ok = safety.shorts_threads_allowed(today)
    total = max(face_n, 1 if threads_ok else 0)
    plan: list[PlannedVideo] = []
    for idx, fmt in enumerate(formats_for(today, total)):
        channels: list[str] = []
        if idx < face_n:
            channels.append(CHANNEL_FACE)
        if idx == 0 and threads_ok:
            channels.append(CHANNEL_THREADS)
        # 마스터 결정 2026-10-04: Facebook 영상에는 GOC 만 등장한다. Facebook 과 Threads 에 함께 가는
        #   첫 편도 Facebook 규칙을 따른다(같은 영상). Threads 단독 편만 EDT.
        character = config.FACE_CHARACTER if CHANNEL_FACE in channels else config.CHARACTER_EDT
        plan.append(
            PlannedVideo(
                content_id=f"sv-{today.strftime('%Y%m%d')}-{idx + 1}",
                index=idx,
                fmt=fmt,
                channels=tuple(channels),
                character=character,
            )
        )
    return plan


def content_date(content_id: str) -> dt.date | None:
    """content_id 의 날짜. 형식이 다르면 None."""
    m = re.fullmatch(r"sv-(\d{8})-\d+", content_id or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _at(today: dt.date, hhmm: str) -> dt.datetime:
    return dt.datetime.combine(today, dt.time.fromisoformat(hhmm), tzinfo=KST)


def publish_schedule(
    now: dt.datetime,
    count: int,
    *,
    rng: random.Random | None = None,
    window: tuple[str, str] | None = None,  # DN2026_0002 : 채널별 시간대 (None=Threads 기본값)
    immediate: bool = False,  # DN2026_0004 : True 면 첫 편 지연 없이 바로(수동 즉시 게시)
) -> list[dt.datetime]:
    """승인(=publish job 시작) 시각 now 부터 count 편의 게시 시각(KST).

    - 첫 편: max(now, 시간대 시작) + SHORTS_FIRST_JITTER_MIN (DN2026_0004: immediate 면 지연 0)
    - 남은 시간 = min(시간대 끝, job 예산 끝) - 첫 편. 편간 간격은 SHORTS_GAP_MIN 범위에서 무작위로 뽑되
      남은 시간에 다 들어가도록 상한을 줄인다. 그래도 간격이 SHORTS_GAP_FLOOR_MIN 보다 짧아지면
      뒤 편부터 뺀다(몰아서 올리지 않는다).
    지연은 매 실행 무작위(시드 없음)다. 고정 간격을 쓰지 않는다.
    """
    rng = rng or random.Random()
    if count <= 0:
        return []
    local = now.astimezone(KST)
    # DN2026_0002 : 시간대를 인자로 받는다 (Facebook=SHORTS_FACE_PUBLISH_WINDOW)
    win = window or config.SHORTS_PUBLISH_WINDOW
    start = _at(local.date(), win[0])
    end = _at(local.date(), win[1])
    limit = min(end, local + dt.timedelta(minutes=config.SHORTS_JOB_BUDGET_MIN))
    base = max(local, start)
    # DN2026_0004 : 수동 즉시 게시는 첫 편 지연 없음, 그 외는 안티봇 무작위 지연(1~10분)
    jitter = 0 if immediate else rng.randint(*config.SHORTS_FIRST_JITTER_MIN)
    first = base + dt.timedelta(minutes=jitter)
    if first > limit:
        # DN2026_0002 : build 가 05:06 에 시작하면 publish job 이 일찍 떠서 job 예산 끝(시작+300분)이
        #   Threads 시간대 시작(10:00) 직후에 걸릴 수 있다. 이때 지연값이 예산을 넘으면 Threads 편이
        #   통째로 빠지므로, 시간대 끝을 넘지 않는 한 남은 예산 안에서 지연을 다시 뽑는다.
        #   (시간대 끝이 한계인 경우는 기존대로 게시하지 않는다.)
        if not (base < limit < end):
            return []
        first = base + dt.timedelta(minutes=rng.uniform(0, (limit - base).total_seconds() / 60))
    remaining = (limit - first).total_seconds() / 60
    k = count
    while k > 1 and remaining / (k - 1) < config.SHORTS_GAP_FLOOR_MIN:
        k -= 1
    out = [first]
    if k > 1:
        cap = remaining / (k - 1)
        low = min(config.SHORTS_GAP_MIN[0], cap)
        high = min(config.SHORTS_GAP_MIN[1], cap)
        t = first
        for _ in range(k - 1):
            t = t + dt.timedelta(minutes=rng.uniform(low, high))
            out.append(t)
    return out

"""계정 보호(안전) 모드 — 단일 정책 모듈 (v1.6.0, DESIGN_V16_SAFETY.md).

배경
  운영 계정이 '봇 의심'으로 비활성화되었다. Meta 는 개별 집행 사유를 공개하지 않으므로
  원인은 확정할 수 없다. 코드에서 확인된 위험 요인(사실)은 다음과 같다.
    H1 정기·이벤트 글마다 외부 링크 2개(YouTube·X) 셀프 리플라이
    H2 새 계정을 첫날부터 전부 자동화
    H3 AI 자동 답글 하루 최대 40건 + 외국어 댓글에 정형 문구 반복
    H4 텍스트 CHAT 평일 5~15건(09:00~24:00)
  API 한도(24시간 게시 250 · 답글 1,000)에는 근접한 적이 없다. 한도가 아니라 행동 패턴의 문제로 본다.

이 모듈의 역할
  모든 쓰기 경로(정기 main · CHAT run_chat · STORY run_story · 댓글 답글/이어쓰기 run_reply)가
  '쓸지 말지'를 여기서만 묻는다. 무상태다(Threads API 조회 결과와 Variables 만 본다).

  S1 AUTOMATION_ENABLED(기본 false)  — 전역 킬 스위치
  S2 DAILY_POST_BUDGET(기본 2)       — KST 하루 자동 최상위 게시물 총량, 정기 몫 1건 예약
  S3 LINK_REPLY_PCT(기본 0)          — 링크 셀프 리플라이 비율(게시물 ID 해시)
  S5 WARMUP_UNTIL(기본 없음)         — 워밍업: 정기 1건/일만, 링크·답글·이어쓰기·CHAT·STORY 중지
  S7 회로 차단기                      — 계정·토큰 사용 불가 오류(code 200 / code 190 · HTTP 401) 뒤
                                         같은 실행에서 쓰기 0건, 종료코드 7
  REPLY_CANNED_ENABLED(기본 false)   — 외국어 댓글 정형 문구(S4)

  기능별 스위치(CHAT_ENABLED·EVENT_STORY_ENABLED·REPLY_ENABLED·FOLLOWUP_ENABLED)와 기능별 상한은
  그대로 유지된다. 이 모듈은 그 위에 '더 막는' 조건만 더한다(둘 중 작은 쪽).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import antibot, chat_plan, config, watchdog

VERSION = "1.1.0"   # v1.1.0: 숏폼(KIND_SHORTS · Facebook 게시 판정) — v1.7.0
# v1.0.0: v1.6.0 신규

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# S7: 계정·토큰 사용 불가 오류의 종료코드(기존 code=200 차단 종료코드 7 을 그대로 쓴다).
FATAL_EXIT_CODE = 7

# S2: 오늘 게시물 집계에 쓰는 목록 크기. 한 페이지(25)로 KST 오늘을 덮는다.
#   예산 기본 2·권장 상한 3 보다 훨씬 크다. 예산을 25 이상으로 두면 집계가 포화될 수 있다(문서화).
BUDGET_SCAN_POSTS = 25

# 쓰기 종류
KIND_REGULAR = "regular"     # 정기 발행(main)
KIND_CHAT = "chat"           # CHAT(run_chat)
KIND_STORY = "story"         # 이벤트 STORY(run_story)
KIND_REPLY = "reply"         # 댓글 답글(run_reply.sweep)
KIND_FOLLOWUP = "followup"   # 셀프 이어쓰기(run_reply._followups)
KIND_SHORTS = "shorts"       # v1.7.0 Threads 동영상(run_shorts_publish). 예산상 CHAT·STORY 와 같은 비정기 글

_KIND_LABEL = {
    KIND_REGULAR: "정기 발행",
    KIND_CHAT: "CHAT",
    KIND_STORY: "이벤트 STORY",
    KIND_REPLY: "댓글 답글",
    KIND_FOLLOWUP: "셀프 이어쓰기",
    KIND_SHORTS: "Threads 숏폼 동영상",
}

# S5: 워밍업 중에도 허용되는 쓰기. 정기 발행(하루 1건)만.
_WARMUP_ALLOWED = frozenset({KIND_REGULAR})


# ---------------------------------------------------------------------------
# 기본 판정
# ---------------------------------------------------------------------------


def _kst_today() -> dt.date:
    return dt.datetime.now(KST).date()


def automation_enabled() -> bool:
    """S1 전역 킬 스위치."""
    return bool(config.AUTOMATION_ENABLED)


@dataclass(frozen=True)
class Warmup:
    active: bool
    until: dt.date | None
    invalid: bool       # 값이 있는데 날짜로 읽히지 않음 → 워밍업으로 본다(fail safe)
    raw: str


_WARNED_INVALID: set[str] = set()
# GitHub Variables 는 빈 값을 저장할 수 없어 자리표시자가 들어가는 경우가 있다(config._UNSET_PLACEHOLDERS).
_UNSET = frozenset({"-", "—", "–", "none", "null", "없음", "off"})
# YYYY-MM-DD 만 받는다. date.fromisoformat 은 3.11 부터 '20261001'·'2026-W40' 같은 형식도 받아들인다.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def warmup_state(today: dt.date | None = None) -> Warmup:
    """S5 워밍업 상태. today <= WARMUP_UNTIL(KST, 그날 포함)이면 활성.

    빈 값·자리표시자 = 워밍업 없음. 날짜로 읽히지 않는 값 = 워밍업 활성(fail safe) + 경고.
    """
    raw = (config.WARMUP_UNTIL or "").strip()
    if not raw or raw.lower() in _UNSET:
        return Warmup(False, None, False, raw)
    try:
        if not _DATE_RE.fullmatch(raw):
            raise ValueError(raw)
        until = dt.date.fromisoformat(raw)
    except ValueError:
        if raw not in _WARNED_INVALID:
            _WARNED_INVALID.add(raw)
            log.warning(
                "WARMUP_UNTIL=%r 를 날짜(YYYY-MM-DD)로 읽을 수 없습니다 — 안전을 위해 워밍업으로 봅니다.",
                raw,
            )
        return Warmup(True, None, True, raw)
    today = today or _kst_today()
    return Warmup(today <= until, until, False, raw)


def _warmup_label(w: Warmup) -> str:
    if w.invalid:
        return f"워밍업(WARMUP_UNTIL={w.raw!r} 형식 오류 → 안전 측 적용)"
    return f"워밍업(~{w.until.isoformat() if w.until else '?'} KST)"


def block_reason(kind: str, today: dt.date | None = None) -> str | None:
    """S1·S5 로 이 종류의 쓰기를 막아야 하는 사유. 없으면 None.

    기능별 스위치(CHAT_ENABLED 등)는 호출자가 기존대로 따로 본다.
    """
    label = _KIND_LABEL.get(kind, kind)
    if not automation_enabled():
        return (
            f"AUTOMATION_ENABLED=false — 계정 보호 모드(킬 스위치). "
            f"{label} 없음, Threads 쓰기·Claude 호출 없이 종료"
        )
    w = warmup_state(today)
    if w.active and kind not in _WARMUP_ALLOWED:
        return f"{_warmup_label(w)} — {label} 중지"
    return None


def replies_allowed(today: dt.date | None = None) -> bool:
    """댓글 답글 가능 여부(S1·S5 + REPLY_ENABLED)."""
    return config.REPLY_ENABLED and block_reason(KIND_REPLY, today) is None


def followups_allowed(today: dt.date | None = None) -> bool:
    """셀프 이어쓰기 가능 여부(S1·S5·S6 + FOLLOWUP_ENABLED)."""
    return config.FOLLOWUP_ENABLED and block_reason(KIND_FOLLOWUP, today) is None


def chat_allowed(today: dt.date | None = None) -> bool:
    return config.CHAT_ENABLED and block_reason(KIND_CHAT, today) is None


def story_allowed(today: dt.date | None = None) -> bool:
    return config.EVENT_STORY_ENABLED and block_reason(KIND_STORY, today) is None


def shorts_threads_allowed(today: dt.date | None = None) -> bool:
    """v1.7.0 Threads 동영상 게시 가능 여부(S1·S5 + SHORTS_THREADS_ENABLED). 예산은 budget_block 이 따로 본다."""
    return config.SHORTS_THREADS_ENABLED and block_reason(KIND_SHORTS, today) is None


def face_block_reason() -> str | None:
    """v1.7.0 Facebook 릴스 게시를 막을 사유. 없으면 None.

    Threads 워밍업(WARMUP_UNTIL)·하루 총량(DAILY_POST_BUDGET)은 Threads 계정 기준이라 적용하지 않는다.
    Facebook 은 램프(FACE_RAMP_START · FACE_DAILY_MAX, shorts_plan.face_daily_target)로 양을 정한다.
    """
    if not automation_enabled():
        return "AUTOMATION_ENABLED=false — 계정 보호 모드(킬 스위치). Facebook 게시 없음"
    if not config.FACE_ENABLED:
        return "FACE_ENABLED=false — Facebook 게시 꺼짐"
    return None


def canned_enabled() -> bool:
    """외국어 댓글 정형 문구 사용 여부(S4). false 면 외국어 댓글은 건너뛴다."""
    return bool(config.REPLY_CANNED_ENABLED)


# ---------------------------------------------------------------------------
# S2 일일 예산
# ---------------------------------------------------------------------------


def effective_post_budget(today: dt.date | None = None) -> int:
    """오늘 적용되는 자동 최상위 게시물 예산. 워밍업이면 min(예산, 1). 음수는 0."""
    budget = max(0, int(config.DAILY_POST_BUDGET))
    if warmup_state(today).active:
        budget = min(budget, 1)
    return budget


def _parse(post: dict) -> dt.datetime | None:
    return watchdog.parse_threads_timestamp(str(post.get("timestamp", "")))


def count_top_level_today(posts: Iterable[dict], now: dt.datetime) -> int:
    """오늘(KST) 최상위 게시물 수.

    GET /{user-id}/threads 는 내 게시물(최상위) 목록이다. 답글은 별도 엔드포인트(/replies)라
    섞이지 않는 것으로 보고, 응답에 is_reply=true 가 있으면 방어적으로 뺀다.
    앱에서 사람이 직접 올린 글도 세어진다(보수적). timestamp 를 읽지 못한 글은 오늘 글로 센다
    (형식이 바뀌어 0건으로 보이면 예산이 무력화되므로 막는 쪽으로 기운다).
    """
    today = now.astimezone(KST).date()
    count = 0
    for post in posts:
        if post.get("is_reply") is True:
            continue
        parsed = _parse(post)
        if parsed is None or parsed.astimezone(KST).date() == today:
            count += 1
    return count


def regular_slot_at(today: dt.date) -> dt.datetime | None:
    """오늘 당첨 정기 슬롯의 cron 시각(KST). 휴식일·슬롯 없음이면 None.

    main._slot_gate 와 같은 함수·솔트(antibot.choose_slot, ANTIBOT_SLOT_SALT_PUBLISH)를 쓴다.
    """
    if antibot.is_rest_day(today, config.PUBLISH_WEEKLY_REST_DAYS):
        return None
    slots = list(config.PUBLISH_SLOTS)
    if not slots:
        return None
    slot = antibot.choose_slot(today, slots, config.ANTIBOT_SLOT_SALT_PUBLISH)
    hhmm = config.PUBLISH_SLOT_TIMES.get(slot)
    if not hhmm:
        return None
    return dt.datetime.combine(today, dt.time.fromisoformat(hhmm), tzinfo=KST)


def regular_reserved(posts: Iterable[dict], now: dt.datetime) -> dt.datetime | None:
    """정기 발행 몫 1건을 예약해야 하면 그 예약이 풀리는 시각, 아니면 None.

    규칙(S2 우선순위)
      - 오늘 당첨 정기 슬롯 시각 + 판정 창(PUBLISH_CLASSIFY_WINDOW_MIN, 지연 최대 20분 + 여유 27분)
        이 지나기 전까지 1건을 정기 발행 몫으로 남긴다.
      - 그 창 안에 CHAT 이 아닌 글이 이미 있으면(정기 발행 완료) 예약하지 않는다.
      - 창이 지났으면 정기 발행이 실패했더라도 예약을 푼다(남은 예산은 CHAT·STORY 가 쓸 수 있다).
      - 휴식일·슬롯 없음이면 예약하지 않는다.
    """
    local = now.astimezone(KST)
    slot_at = regular_slot_at(local.date())
    if slot_at is None:
        return None
    release = slot_at + dt.timedelta(minutes=config.PUBLISH_CLASSIFY_WINDOW_MIN)
    if local >= release:
        return None
    for post in posts:
        parsed = _parse(post)
        if parsed is None:
            continue
        media_type = str(post.get("media_type") or "")
        # v1.7.0: 숏폼 동영상은 정기 발행 완료로 보지 않는다(정기 몫 예약 유지).
        if slot_at <= parsed <= release and not chat_plan.is_chat_post(parsed, media_type) \
                and not chat_plan.is_shorts_post(media_type):
            return None
    return release


def budget_block(kind: str, posts: list[dict], now: dt.datetime) -> str | None:
    """S2 예산으로 이 최상위 게시물을 막아야 하는 사유. 없으면 None.

    정기(regular): 오늘 게시물 < 예산이면 허용.
    CHAT·STORY  : 정기 몫 예약 중이면 오늘 게시물 < 예산 - 1 일 때만 허용(CHAT 이 정기 몫을 먹지 않게).
    """
    today = now.astimezone(KST).date()
    budget = effective_post_budget(today)
    used = count_top_level_today(posts, now)
    if used >= budget:
        return f"오늘(KST) 최상위 게시물 {used}건 — DAILY_POST_BUDGET 적용값 {budget}건 도달"
    if kind != KIND_REGULAR:
        release = regular_reserved(posts, now)
        if release is not None and used >= budget - 1:
            return (
                f"오늘(KST) 최상위 게시물 {used}건 / 예산 {budget}건 — 정기 발행 몫 1건 예약 중 "
                f"({release.strftime('%H:%M')} KST 까지)"
            )
    return None


# ---------------------------------------------------------------------------
# S3 링크 셀프 리플라이
# ---------------------------------------------------------------------------


def effective_link_reply_pct(today: dt.date | None = None) -> int:
    """링크 셀프 리플라이 비율(0~100). 워밍업이면 0."""
    if warmup_state(today).active:
        return 0
    return max(0, min(100, int(config.LINK_REPLY_PCT)))


def link_reply_selected(post_id: str, today: dt.date | None = None) -> bool:
    """이 게시물에 링크 셀프 리플라이를 달지. 게시물 ID 해시(무상태·멱등).

    0 이면 항상 False, 100 이면 항상 True. 같은 ID 는 항상 같은 결과.
    """
    pct = effective_link_reply_pct(today)
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    digest = hashlib.sha256(f"{post_id}::link".encode()).hexdigest()[:8]
    return int(digest, 16) % 100 < pct


# ---------------------------------------------------------------------------
# S7 회로 차단기
# ---------------------------------------------------------------------------

_TRIPPED: list[BaseException] = []


def is_account_fatal(exc: BaseException) -> bool:
    """계정·토큰을 쓸 수 없다는 오류인지.

    threads_client.ThreadsApiError 가 이미 다루는 두 분류만 쓴다(추측으로 코드를 늘리지 않는다).
      is_blocked    : code 200 또는 본문 'access blocked' — 접근 차단
      is_auth_error : code 190(OAuthException) 또는 HTTP 401 — 토큰 무효·만료·권한 박탈
    """
    return bool(getattr(exc, "is_blocked", False) or getattr(exc, "is_auth_error", False))


def trip(exc: BaseException) -> None:
    """차단기를 연다. 이 프로세스(실행)에서는 이후 쓰기를 하지 않는다."""
    if not _TRIPPED:
        _TRIPPED.append(exc)
        log.error("회로 차단 — 계정·토큰 사용 불가 오류. 이번 실행의 이후 쓰기를 모두 멈춥니다: %s", exc)


def tripped() -> BaseException | None:
    return _TRIPPED[0] if _TRIPPED else None


def reset_circuit() -> None:
    """테스트 전용. 실행(프로세스)마다 새로 시작하므로 운영 경로에서는 부르지 않는다."""
    _TRIPPED.clear()


def guard_write() -> None:
    """쓰기 직전에 부른다. 차단기가 열려 있으면 원래 오류를 다시 올린다(재시도 없음)."""
    exc = tripped()
    if exc is not None:
        raise exc


def fatal_message(exc: BaseException, runner: str) -> str:
    kind = "접근 차단 (code=200)" if getattr(exc, "is_blocked", False) else \
        "토큰·인증 무효 (code=190 / HTTP 401) — 재인가 필요: authorize -> 단기 -> 장수명"
    return (
        f"[Threads][최우선] {runner} — {kind}\n"
        "이번 실행의 쓰기를 즉시 멈췄습니다(재시도 없음).\n"
        "developers.facebook.com · Threads 앱에서 계정·앱·토큰 상태를 확인하고,\n"
        "해소 전까지 Variables AUTOMATION_ENABLED=false 로 두십시오.\n"
        f"{exc}"
    )


def handle_fatal(
    exc: BaseException, runner: str, notify: Callable[[str], None]
) -> int:
    """러너 main() 공통 처리. 알림 1회 + 종료코드 FATAL_EXIT_CODE."""
    trip(exc)
    msg = fatal_message(exc, runner)
    log.error(msg)
    notify(msg)
    return FATAL_EXIT_CODE


# ---------------------------------------------------------------------------
# 요약 (로그·golive_check·watchdog)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    automation: bool
    warmup: Warmup
    budget: int             # 적용값(워밍업 반영)
    budget_raw: int
    link_pct: int           # 적용값(워밍업 반영)
    link_pct_raw: int
    replies: bool
    followups: bool
    chat: bool
    story: bool
    canned: bool


def profile(today: dt.date | None = None) -> Profile:
    return Profile(
        automation=automation_enabled(),
        warmup=warmup_state(today),
        budget=effective_post_budget(today),
        budget_raw=int(config.DAILY_POST_BUDGET),
        link_pct=effective_link_reply_pct(today),
        link_pct_raw=int(config.LINK_REPLY_PCT),
        replies=replies_allowed(today),
        followups=followups_allowed(today),
        chat=chat_allowed(today),
        story=story_allowed(today),
        canned=canned_enabled(),
    )


def describe(today: dt.date | None = None) -> str:
    """한 줄 요약(실행 로그 첫머리용)."""
    p = profile(today)
    w = p.warmup
    warm = "없음" if not w.active and not w.until else (
        _warmup_label(w) + (" 활성" if w.active else " 종료")
    )
    return (
        f"안전모드 v{VERSION}: 자동화={'켜짐' if p.automation else '꺼짐'} · "
        f"예산 {p.budget}(설정 {p.budget_raw}) · 링크리플 {p.link_pct}%(설정 {p.link_pct_raw}) · "
        f"워밍업 {warm} · 답글 {'허용' if p.replies else '중지'} · "
        f"이어쓰기 {'허용' if p.followups else '중지'} · CHAT {'허용' if p.chat else '중지'} · "
        f"STORY {'허용' if p.story else '중지'} · 정형문구 {'사용' if p.canned else '미사용'}"
    )


# 안전 프로필 기준값(S8). 넘으면 golive_check 가 WARN 한다(FAIL 아님).
SAFE_MAX_LINK_REPLY_PCT = 0
SAFE_MAX_REPLY_DAILY_CAP = 10
SAFE_MAX_DAILY_POST_BUDGET = 3


def safe_profile_warnings() -> list[str]:
    """AUTOMATION_ENABLED=true 이면서 안전 프로필을 넘는 설정 목록. 꺼져 있으면 빈 목록."""
    if not automation_enabled():
        return []
    out: list[str] = []
    if int(config.LINK_REPLY_PCT) > SAFE_MAX_LINK_REPLY_PCT:
        out.append(f"LINK_REPLY_PCT={config.LINK_REPLY_PCT} > {SAFE_MAX_LINK_REPLY_PCT}")
    if int(config.REPLY_DAILY_CAP) > SAFE_MAX_REPLY_DAILY_CAP:
        out.append(f"REPLY_DAILY_CAP={config.REPLY_DAILY_CAP} > {SAFE_MAX_REPLY_DAILY_CAP}")
    if int(config.DAILY_POST_BUDGET) > SAFE_MAX_DAILY_POST_BUDGET:
        out.append(f"DAILY_POST_BUDGET={config.DAILY_POST_BUDGET} > {SAFE_MAX_DAILY_POST_BUDGET}")
    return out


def watch_publish_exempt(today: dt.date | None = None) -> str | None:
    """워치독 발행 신선도 판정을 생략할 사유(오탐 방지). 없으면 None."""
    if not automation_enabled():
        return "AUTOMATION_ENABLED=false — 자동 발행이 꺼져 있어 발행 공백 판정 생략"
    if effective_post_budget(today) <= 0:
        return "DAILY_POST_BUDGET=0 — 자동 발행이 없어 발행 공백 판정 생략"
    return None

"""Facebook 숏폼 스토리 연속성 — Notion 회차 원장 (v1.8.0, 상세설계 v1.0 2026-10-04).

마스터 결정 2026-10-04: 저장소 도입 A안. Facebook 기준 단독 Notion DB(FACE_NOTION_DB_ID).
기존 Tracker DB(NOTION_DB_ID · notion_source.py)는 쓰지 않는다 — 그 DB 의 새 행은 STORY 근거로 읽힌다.

  읽기(build)  : load_context — 상태=게시완료 행만 날짜 내림차순으로 읽어 '지난 이야기'·열린 떡밥을 만든다.
  쓰기(publish): Ledger.upsert — Facebook 게시 결과(게시완료/확인필요/실패)를 회차ID 기준 1행으로 기록한다.
  재조정       : Ledger.pending — 확인필요 행을 publish 가 Facebook 상태로 다시 확인한다.

원칙
  - 후보와 확정을 섞지 않는다. 원장 쓰기는 publish(승인 후)에만 있다. 읽기는 게시완료만 쓴다.
  - 회차 날짜는 content_id(KST)에서 온다. 실행 시각을 쓰지 않는다.
  - 0 은 정상 값이다(None 만 '없음').
  - 원장 장애는 영상 생성·게시를 막지 않는다(마스터 미결 D1 의 권장안). 대신 알림으로 알린다.
  - Notion 에서 사람이 고친 글도 읽을 때 다시 린트한다(REG-03·04). 위반한 값은 버린다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import config, content, shorts_plan
from .redact import redact

VERSION = "1.2.0"   # v1.8.0 신규 · 1.1.0/1.2.0: 3인 QC/코드리뷰 1·2차 반영

log = logging.getLogger(__name__)

# 기존 notion_source.py 와 같은 API 버전을 쓴다(버전 전환은 별도 결정 — 상세설계 V6).
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
RETRY_AFTER_CAP_SEC = 60
# 재시도할 응답(공식 status codes 문서: 429·529 는 Retry-After, 502·503·504 는 재시도 권고).
#   쓰기(pages 생성·갱신)는 처리 여부가 확정되는 429 만 재시도한다(중복 행 방지).
RETRY_READ_STATUSES = (429, 502, 503, 504, 529)
RETRY_WRITE_STATUSES = (429,)
RECONCILE_MAX_ROWS = 5          # 실행당 재조정 행 상한(시간 예산 보호)
COPY_SIMILARITY = 0.6           # 문자 bigram 자카드 유사도 — 이 이상이면 복사로 본다
COPY_MIN_CONTAIN = 8            # 포함 판정은 짧은 쪽이 정규화 8자 이상일 때만(짧은 떡밥이 모든 문장을 막지 않게)
RECONCILE_GIVEUP_DAYS = 2       # 회차 날짜로부터 이 일수가 지나도 확인 못 하면 '실패(확인 불가)'로 닫는다

# DB 속성 이름 (상세설계 v1.0 §3). 운영 DB 를 이 이름으로 만든다.
P_ID = "회차ID"
P_DATE = "날짜"
P_FORMAT = "포맷"
P_STATUS = "상태"
P_SERIES = "시리즈회차"
P_HOOK = "훅유형"
P_THEMES = "테마"
P_SUMMARY = "줄거리요약"
P_THREAD_ID = "떡밥ID"
P_THREAD = "떡밥"
P_THREAD_STATE = "떡밥상태"
P_THREAD_AGE = "떡밥경과"
P_RESOLUTION = "회수내용"
P_VIDEO_ID = "FB영상ID"
P_CAPTION_HASH = "캡션해시"
P_ERROR = "오류"

# 속성 이름 → Notion 타입. DB 를 만들 때와 쓸 때 같은 표를 쓴다.
SCHEMA: dict[str, str] = {
    P_ID: "title", P_DATE: "date", P_FORMAT: "select", P_STATUS: "select", P_SERIES: "number",
    P_HOOK: "select", P_THEMES: "multi_select", P_SUMMARY: "rich_text", P_THREAD_ID: "rich_text",
    P_THREAD: "rich_text", P_THREAD_STATE: "select", P_THREAD_AGE: "number",
    P_RESOLUTION: "rich_text", P_VIDEO_ID: "rich_text", P_CAPTION_HASH: "rich_text",
    P_ERROR: "rich_text",
}

STATUS_PUBLISHED = "게시완료"
STATUS_PENDING = "확인필요"
STATUS_FAILED = "실패"

FORMAT_SAGA = shorts_plan.FORMAT_SAGA

ACTION_OPEN = "OPEN"
ACTION_PROGRESS = "PROGRESS"
ACTION_RESOLVE = "RESOLVE"
ACTION_NONE = "NONE"
ACTIONS = (ACTION_OPEN, ACTION_PROGRESS, ACTION_RESOLVE, ACTION_NONE)

STATE_OPEN = "OPEN"
STATE_PROGRESSED = "PROGRESSED"
STATE_RESOLVED = "RESOLVED"
STATE_NONE = "없음"
OPEN_STATES = (STATE_OPEN, STATE_PROGRESSED)

# 길이 (상세설계 §3)
SUMMARY_MIN, SUMMARY_MAX = 40, 120
THREAD_MIN, THREAD_MAX = 15, 60
ERROR_MAX = 300
THREAD_ID_PREFIX = "th-"

_NORMALIZE = re.compile(r"[\s\.,·!?~…'\"“”‘’()\-]+")


class LedgerError(RuntimeError):
    """원장 호출 실패. 호출자는 게시 결과를 바꾸지 않고 알림만 보낸다."""


# ---------------------------------------------------------------------------
# 읽기 결과 — 대본 프롬프트에 넣는 맥락
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PastEpisode:
    content_id: str
    series_no: int | None
    summary: str


@dataclass(frozen=True)
class OpenThread:
    thread_id: str
    text: str
    age: int          # 이번 F1 회차가 몇 번째 경과인지(떡밥을 연 회차 = 0 이므로 다음 회차는 1 이상)


@dataclass(frozen=True)
class StoryContext:
    available: bool                         # 원장을 읽었는가(비어 있어도 True)
    episodes: tuple[PastEpisode, ...] = field(default_factory=tuple)   # 최신순
    open_thread: OpenThread | None = None
    next_series_no: int | None = None       # 원장을 읽었으면 항상 값이 있다
    # 가장 최근 떡밥 변화가 아직 확인필요 회차에 있다 — 확정 전 내용은 쓰지 않고 이번 화는 떡밥을 다루지 않는다
    #   (리뷰 2차 #2-B: 미확정 떡밥 문장을 프롬프트에 넣지 않는다 · #2-A: 미방송 회수로 새 떡밥을 열지 않는다)
    thread_hold: bool = False
    error: str = ""

    @classmethod
    def unavailable(cls, error: str) -> StoryContext:
        return cls(available=False, error=error)


@dataclass(frozen=True)
class ContinuityRequest:
    """대본 1편에 거는 연속성 계약. None 이면 v1.7.0 과 같은 대본 계약."""

    fmt: str
    prompt_block: str
    open_thread: OpenThread | None
    force_resolve: bool
    allow_thread: bool          # F1 이고 원장을 읽은 경우만 떡밥을 다룬다

    hold: bool = False          # 떡밥 확인 대기(StoryContext.thread_hold)

    def allowed_actions(self) -> tuple[str, ...]:
        if not self.allow_thread or self.hold:
            return (ACTION_NONE,)
        if self.open_thread is None:
            return (ACTION_OPEN, ACTION_NONE)
        if self.force_resolve:
            return (ACTION_RESOLVE,)
        return (ACTION_PROGRESS, ACTION_RESOLVE)


# ---------------------------------------------------------------------------
# 순수 함수 — 정규화 · 검증 · 요청 구성 · 기록 구성
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    return _NORMALIZE.sub("", text or "").strip()


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def is_copy(candidate: str, previous: str) -> bool:
    """candidate 가 previous 문장의 복사인가. ICG '미해결 문장 복사 → 해결' 장애 방지.

    같음 · 한쪽이 다른 쪽을 포함(앞부분만 잘라 쓴 부분 복사 포함) · 문자 bigram 자카드 유사도 0.6 이상.
    """
    a, b = normalize(candidate), normalize(previous)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= COPY_MIN_CONTAIN and shorter in longer:
        return True
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return False
    return len(ga & gb) / len(ga | gb) >= COPY_SIMILARITY


def caption_hash(caption: str) -> str:
    return hashlib.sha256((caption or "").strip().encode("utf-8")).hexdigest()[:16]


content_date = shorts_plan.content_date   # 회차 날짜(KST)는 content_id 에서만 온다


def _safe_text(value: str, *, max_len: int, label: str) -> str:
    """원장에서 읽은 글을 다시 린트한다. 위반이면 빈 문자열(그 값을 버린다)."""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        content.lint_shorts(text, max_len=max_len, label=label, extra_latin=(config.FACE_CHARACTER,))
    except content.ContentPolicyError as exc:
        log.warning("원장 %s 값이 정책 위반이라 버립니다: %s", label, exc)
        return ""
    return text


def request_for(ctx: StoryContext | None, fmt: str) -> ContinuityRequest | None:
    """편 하나의 연속성 계약. 원장을 쓰지 않는 설정(ctx None)이면 None."""
    if ctx is None:
        return None
    allow = ctx.available and fmt == FORMAT_SAGA
    hold = allow and ctx.thread_hold
    thread = ctx.open_thread if allow and not hold else None
    force = bool(thread and thread.age >= config.FACE_THREAD_MAX_EPISODES)
    lines: list[str] = []
    if allow:
        title = f"# 지난 이야기 (시리즈 {ctx.next_series_no}화 예정)" if ctx.next_series_no else "# 지난 이야기"
        if ctx.episodes:
            lines.append(title)
            for ep in ctx.episodes:
                label = f"{ep.series_no}화" if ep.series_no is not None else ep.content_id
                lines.append(f"- {label}: {ep.summary}")
        else:
            lines.append("# 지난 이야기 없음 — 시리즈 첫 화다. 인물과 무대를 소개한다.")
        if hold:
            lines.append("# 떡밥 확인 대기 — 직전 회차 게시 확인 전이다. 이번 화는 떡밥을 열거나 잇지 않는다(NONE).")
        elif thread:
            lines.append(f"# 열린 떡밥 [{thread.thread_id}] ({thread.age}회차 경과): {thread.text}")
            if force:
                lines.append("- 이번 화에서 반드시 회수한다(RESOLVE): 새 사실과 그 결과를 말한다.")
            else:
                lines.append("- 이번 화에서 PROGRESS(새 단서를 더함) 또는 RESOLVE(새 사실과 결과로 매듭) 중 하나를 한다.")
            lines.append("- 떡밥 문장을 그대로 다시 쓰지 않는다. 숫자·실존 기업·인물은 쓰지 않는다.")
        else:
            lines.append("# 열린 떡밥 없음 — 다음 화로 이어질 질문 하나를 열어도 된다(OPEN) 또는 열지 않는다(NONE).")
    else:
        lines.append("# 연속성: 이번 편은 줄거리 요약만 남긴다(thread_action 은 NONE).")
    actions = "|".join(
        ContinuityRequest(fmt, "", thread, force, allow, hold).allowed_actions()
    )
    lines += [
        "# 출력 JSON 에 continuity 객체를 반드시 추가한다",
        '"continuity": {"summary": "이번 편 줄거리 요약", "thread_action": "' + actions + '", '
        '"thread_text": "OPEN·PROGRESS 일 때 떡밥", "resolution": "RESOLVE 일 때 회수 내용"}',
        f"- summary {SUMMARY_MIN}~{SUMMARY_MAX}자, thread_text·resolution {THREAD_MIN}~{THREAD_MAX}자, "
        "모두 대사와 같은 규칙(숫자·실존명·투자조언 금지).",
    ]
    return ContinuityRequest(fmt=fmt, prompt_block="\n".join(lines), open_thread=thread,
                             force_resolve=force, allow_thread=allow, hold=hold)


def validate_continuity(raw: Any, req: ContinuityRequest) -> list[str]:
    """continuity 객체 구조·행동 규칙 검사(결정론). 린트·금지 이름은 script_writer.validate 가 본다."""
    if not isinstance(raw, dict):
        return ["continuity 객체 없음"]
    issues: list[str] = []
    summary = str(raw.get("summary") or "").strip()
    action = str(raw.get("thread_action") or "").strip().upper()
    thread_text = str(raw.get("thread_text") or "").strip()
    resolution = str(raw.get("resolution") or "").strip()
    if not SUMMARY_MIN <= len(summary) <= SUMMARY_MAX:
        issues.append(f"continuity.summary {len(summary)}자 — {SUMMARY_MIN}~{SUMMARY_MAX}자 필요")
    allowed = req.allowed_actions()
    if action not in allowed:
        issues.append(f"continuity.thread_action '{action or '-'}' — 허용: {'/'.join(allowed)}")
        return issues
    if action in (ACTION_OPEN, ACTION_PROGRESS):
        if not THREAD_MIN <= len(thread_text) <= THREAD_MAX:
            issues.append(f"continuity.thread_text {len(thread_text)}자 — {THREAD_MIN}~{THREAD_MAX}자 필요")
        if action == ACTION_PROGRESS and req.open_thread and is_copy(thread_text, req.open_thread.text):
            issues.append("continuity.thread_text 가 이전 떡밥 문장과 같음 — 새 단서를 더해야 함")
    if action == ACTION_RESOLVE:
        if not THREAD_MIN <= len(resolution) <= THREAD_MAX:
            issues.append(f"continuity.resolution {len(resolution)}자 — {THREAD_MIN}~{THREAD_MAX}자 필요")
        if req.open_thread and is_copy(resolution, req.open_thread.text):
            issues.append("continuity.resolution 이 이전 떡밥 문장의 복사 — 새 사실과 결과가 필요")
    return issues


def continuity_texts(raw: Any) -> list[tuple[str, str, int]]:
    """린트 대상 (라벨, 글, 상한). 빈 값은 뺀다."""
    if not isinstance(raw, dict):
        return []
    out = [
        ("연속성 요약", str(raw.get("summary") or "").strip(), SUMMARY_MAX),
        ("연속성 떡밥", str(raw.get("thread_text") or "").strip(), THREAD_MAX),
        ("연속성 회수", str(raw.get("resolution") or "").strip(), THREAD_MAX),
    ]
    return [t for t in out if t[1]]


def build_continuity(raw: dict, req: ContinuityRequest, ctx: StoryContext, content_id: str) -> dict:
    """검증을 통과한 continuity → manifest 에 실을 기록 값. 원장 쓰기는 publish 가 이 값으로 한다."""
    action = str(raw.get("thread_action") or ACTION_NONE).strip().upper()
    out: dict[str, Any] = {
        "summary": str(raw.get("summary") or "").strip(),
        "thread_action": action,
        "series_no": ctx.next_series_no if req.allow_thread else None,
        "thread_id": "",
        "thread_text": "",
        "thread_state": STATE_NONE,
        "thread_age": None,
        "resolution": "",
    }
    thread = req.open_thread
    if action == ACTION_OPEN:
        out.update(thread_id=f"{THREAD_ID_PREFIX}{content_id}", thread_text=str(raw["thread_text"]).strip(),
                   thread_state=STATE_OPEN, thread_age=0)
    elif action == ACTION_PROGRESS and thread:
        out.update(thread_id=thread.thread_id, thread_text=str(raw["thread_text"]).strip(),
                   thread_state=STATE_PROGRESSED, thread_age=thread.age)
    elif action == ACTION_RESOLVE and thread:
        out.update(thread_id=thread.thread_id, thread_text=thread.text, thread_state=STATE_RESOLVED,
                   thread_age=thread.age, resolution=str(raw["resolution"]).strip())
    return out


@dataclass(frozen=True)
class EpisodeRecord:
    content_id: str
    status: str
    fmt: str = ""
    hook_type: str = ""
    themes: tuple[str, ...] = ()
    video_id: str = ""
    caption: str = ""
    error: str = ""
    continuity: dict | None = None


def record_from_item(item: dict, status: str, *, video_id: str = "", error: str = "") -> EpisodeRecord:
    return EpisodeRecord(
        content_id=str(item.get("content_id") or ""),
        status=status,
        fmt=str(item.get("fmt") or ""),
        hook_type=str(item.get("hook_type") or ""),
        themes=tuple(str(t) for t in (item.get("themes") or [])),
        video_id=video_id,
        caption=str(item.get("caption") or ""),
        error=error,
        continuity=item.get("continuity") if isinstance(item.get("continuity"), dict) else None,
    )


def _rt(text: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": (text or "")[:limit]}}] if text else []}


def _select(value: str) -> dict:
    return {"select": {"name": value} if value else None}


def record_properties(rec: EpisodeRecord) -> dict:
    """EpisodeRecord → Notion properties. 상태만 바꾸는 갱신도 같은 함수를 쓴다."""
    props: dict[str, Any] = {
        P_ID: {"title": [{"type": "text", "text": {"content": rec.content_id}}]},
        P_STATUS: _select(rec.status),
        P_ERROR: _rt(redact(rec.error)[:ERROR_MAX]),
    }
    day = content_date(rec.content_id)
    if day:
        props[P_DATE] = {"date": {"start": day.isoformat()}}
    if rec.fmt:
        props[P_FORMAT] = _select(rec.fmt)
    if rec.hook_type:
        props[P_HOOK] = _select(rec.hook_type)
    if rec.themes:
        # multi_select 옵션 이름에 쉼표를 쓸 수 없다(Notion 규칙).
        props[P_THEMES] = {"multi_select": [{"name": t.replace(",", " ")[:100]} for t in rec.themes[:10]]}
    if rec.video_id:
        props[P_VIDEO_ID] = _rt(rec.video_id)
    if rec.caption:
        props[P_CAPTION_HASH] = _rt(caption_hash(rec.caption))
    c = rec.continuity
    if c:
        props[P_SUMMARY] = _rt(str(c.get("summary") or ""))
        props[P_SERIES] = {"number": c.get("series_no")}
        props[P_THREAD_ID] = _rt(str(c.get("thread_id") or ""))
        props[P_THREAD] = _rt(str(c.get("thread_text") or ""))
        props[P_THREAD_STATE] = _select(str(c.get("thread_state") or STATE_NONE))
        props[P_THREAD_AGE] = {"number": c.get("thread_age")}
        props[P_RESOLUTION] = _rt(str(c.get("resolution") or ""))
    return props


# ---------------------------------------------------------------------------
# 원장 행 → 맥락 (순수 함수)
# ---------------------------------------------------------------------------


def _plain(prop: dict | None) -> str:
    if not prop:
        return ""
    ptype = prop.get("type", "")
    if ptype in ("title", "rich_text"):
        return "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                       for part in prop.get(ptype) or [])
    if ptype == "select":
        return (prop.get("select") or {}).get("name", "") or ""
    return ""


def _number(prop: dict | None) -> int | None:
    if not prop or prop.get("type") != "number":
        return None
    value = prop.get("number")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _occupies(props: dict) -> bool:
    """회차번호·떡밥 계산에 넣는 행: 게시완료, 또는 FB영상ID 가 있는 확인필요(처리 확인만 남음).

    3인 리뷰 #2-1: 확인필요를 빼면 다음 날 build 가 그 회차를 건너뛰어 번호가 겹치고 회수한 떡밥이 다시 열린다.
    확인필요의 요약은 '지난 이야기'에 넣지 않는다(게시 확정 전 내용은 기억에 넣지 않는다).
    """
    status = _plain(props.get(P_STATUS))
    if status == STATUS_PUBLISHED:
        return True
    return status == STATUS_PENDING and bool(_plain(props.get(P_VIDEO_ID)).strip())


def context_from_rows(rows: list[dict], lookback: int, today: dt.date | None = None) -> StoryContext:
    """원장 행(날짜 내림차순)으로 맥락을 만든다.

    - 대상 F1 행: 게시완료 + FB영상ID 있는 확인필요(_occupies). today 를 주면 그날 이후 행은 뺀다
      (같은 날 build 재실행이 오늘 회차를 '지난 이야기'로 읽지 않게 — 리뷰 #2-8).
    - 지난 이야기: 게시완료 F1 최근 lookback 건(요약이 린트 통과한 것만)
    - 열린 떡밥: 최신부터 내려가며 떡밥상태가 '없음'이 아닌 첫 행. OPEN/PROGRESSED 면 열림.
      경과 = 그 행의 떡밥경과 + 그보다 최신인 대상 행 수 + 1 (원장 장애로 NONE 이 낀 날도 경과에 센다)
    - 다음 시리즈회차: 번호가 있는 최신 행의 번호 + 그보다 최신인 대상 행 수 + 1.
      번호 있는 행이 없으면 대상 행 수 + 1(행이 없으면 1) — 리뷰 #1-D3·#2-6.
    """
    saga = []
    for row in rows:
        props = row.get("properties") or {}
        if _plain(props.get(P_FORMAT)) != FORMAT_SAGA or not _occupies(props):
            continue
        day = content_date(_plain(props.get(P_ID)))
        if today is not None and day is not None and day >= today:
            continue
        saga.append(props)

    episodes: list[PastEpisode] = []
    for props in saga:
        if len(episodes) >= lookback:
            break
        if _plain(props.get(P_STATUS)) != STATUS_PUBLISHED:
            continue
        summary = _safe_text(_plain(props.get(P_SUMMARY)), max_len=SUMMARY_MAX, label="줄거리요약")
        if summary:
            episodes.append(PastEpisode(_plain(props.get(P_ID)), _number(props.get(P_SERIES)), summary))

    thread: OpenThread | None = None
    hold = False
    for newer, props in enumerate(saga):
        state = _plain(props.get(P_THREAD_STATE))
        if not state or state == STATE_NONE:
            continue
        if _plain(props.get(P_STATUS)) != STATUS_PUBLISHED:
            hold = True          # 최근 떡밥 변화가 미확정 회차에 있다
            break
        if state in OPEN_STATES:
            text = _safe_text(_plain(props.get(P_THREAD)), max_len=THREAD_MAX, label="떡밥")
            if len(text) < THREAD_MIN:
                text = ""        # 사람이 짧게 고친 떡밥은 버린다(리뷰 2차 QC-N2)
            tid = _plain(props.get(P_THREAD_ID)).strip()
            if text and tid.startswith(THREAD_ID_PREFIX):
                base_age = _number(props.get(P_THREAD_AGE))
                thread = OpenThread(tid, text, (base_age if base_age is not None else 0) + newer + 1)
            else:
                log.warning("열린 떡밥 행의 떡밥/떡밥ID 가 비었거나 정책 위반 — 떡밥 없이 진행")
        break

    next_no = len(saga) + 1
    for newer, props in enumerate(saga):
        number = _number(props.get(P_SERIES))
        if number is not None:
            next_no = number + newer + 1
            break
    return StoryContext(available=True, episodes=tuple(episodes), open_thread=thread,
                        next_series_no=next_no, thread_hold=hold)


# ---------------------------------------------------------------------------
# Notion 호출
# ---------------------------------------------------------------------------


class Ledger:
    def __init__(self, token: str, database_id: str, *, session: Any = None, sleep=time.sleep):
        if not token or not database_id:
            raise LedgerError("NOTION_TOKEN / FACE_NOTION_DB_ID 가 필요합니다")
        self._token = token
        self._db = database_id
        self._http = session or requests
        self._sleep = sleep
        self.pending_overflow = 0    # 마지막 pending() 에서 상한을 넘어 남은 행 수

    def _call(self, method: str, path: str, payload: dict | None = None, *, write: bool = False) -> dict:
        """Notion 호출. 어떤 실패든 LedgerError 로만 올린다(호출자의 fail-open 경계 — 리뷰 #1-D1·#2-4·#3-F2).

        재시도: 읽기는 429·502·503·504·529, 쓰기는 429 만 1회. Retry-After 가 상한(60초)을 넘으면
        일찍 재시도하지 않고 실패로 본다(문서: 최소 그 시간만큼 멈출 것).
        """
        url = f"{NOTION_API}/{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Notion-Version": NOTION_VERSION,
                   "Content-Type": "application/json"}
        retryable = RETRY_WRITE_STATUSES if write else RETRY_READ_STATUSES
        for attempt in (1, 2):
            try:
                resp = self._http.request(method, url, headers=headers, json=payload,
                                          timeout=config.HTTP_TIMEOUT_SEC)
            except requests.RequestException as exc:
                raise LedgerError(f"Notion 네트워크 오류: {redact(str(exc))}") from exc
            status = getattr(resp, "status_code", 0)
            if status == 200:
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise LedgerError("Notion 200 응답이 JSON 이 아님") from exc
                if not isinstance(body, dict):
                    raise LedgerError(f"Notion 응답 형식 오류: {type(body).__name__}")
                return body
            if status in retryable and attempt == 1:
                wait = self._retry_after(resp)
                if wait is None:
                    raise LedgerError(f"Notion {status} — Retry-After 가 상한 {RETRY_AFTER_CAP_SEC}초 초과")
                log.warning("Notion %s — %.0f초 뒤 1회 재시도", status, wait)
                self._sleep(wait)
                continue
            hint = {
                401: " — NOTION_TOKEN 확인",
                403: " — 통합 권한(Read·Update·Insert content) 확인",
                404: " — FACE_NOTION_DB_ID 확인, DB 에 통합 연결 필요, 또는 DB 에 데이터 소스가 2개 이상"
                     "(Notion-Version 2022-06-28 은 단일 데이터 소스만 지원)",
                400: " — DB 속성 이름·타입이 상세설계 §3 과 다르거나 DB 에 데이터 소스가 2개 이상",
            }.get(status, "")
            raise LedgerError(f"Notion {status}{hint}: {redact(str(getattr(resp, 'text', ''))[:200])}")
        raise LedgerError("Notion 재시도 후에도 실패")

    @staticmethod
    def _retry_after(resp: Any) -> float | None:
        raw = (getattr(resp, "headers", None) or {}).get("Retry-After", "1")
        try:
            wait = float(raw)
        except (TypeError, ValueError):
            wait = 1.0
        if not math.isfinite(wait) or wait < 0:
            wait = 1.0
        return None if wait > RETRY_AFTER_CAP_SEC else wait

    def _query(self, payload: dict) -> tuple[list[dict], bool]:
        body = self._call("POST", f"databases/{self._db}/query", payload)
        results = body.get("results")
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise LedgerError("Notion 질의 응답에 results 목록이 없음")
        return results, bool(body.get("has_more"))

    def memory_rows(self, limit: int) -> list[dict]:
        """기억 계산용 F1 행(게시완료·확인필요), 날짜 내림차순(같은 날은 생성 시각 내림차순)."""
        rows, _ = self._query({
            "page_size": max(1, min(limit, 100)),
            "filter": {"and": [
                {"or": [{"property": P_STATUS, "select": {"equals": STATUS_PUBLISHED}},
                        {"property": P_STATUS, "select": {"equals": STATUS_PENDING}}]},
                {"property": P_FORMAT, "select": {"equals": FORMAT_SAGA}},
            ]},
            "sorts": [{"property": P_DATE, "direction": "descending"},
                      {"timestamp": "created_time", "direction": "descending"}],
        })
        return rows

    def find(self, content_id: str) -> tuple[str, str] | None:
        """회차ID 행 (page_id, 상태). 없으면 None."""
        rows, _ = self._query({"page_size": 2, "filter": {"property": P_ID, "title": {"equals": content_id}}})
        if not rows:
            return None
        if len(rows) > 1:
            log.warning("원장에 같은 회차ID 행이 %d개 — 첫 행을 갱신합니다: %s", len(rows), content_id)
        page_id = str(rows[0].get("id") or "")
        if not page_id:
            raise LedgerError("Notion 질의 결과 행에 id 가 없음")
        return page_id, _plain((rows[0].get("properties") or {}).get(P_STATUS))

    def upsert(self, rec: EpisodeRecord, *, page_id: str = "", current_status: str = "") -> tuple[str, str]:
        """회차ID 1행(멱등). page_id 를 주면 조회 없이 갱신한다. 반환은 (page_id, 기록 뒤 상태).

        상태 역행 방지(리뷰 #2-2): 이미 게시완료인 행은 확인필요·실패로 내리지 않는다.
        """
        if not page_id:
            found = self.find(rec.content_id)
            if found:
                page_id, current_status = found
        if page_id and current_status == STATUS_PUBLISHED and rec.status != STATUS_PUBLISHED:
            log.info("원장 %s 은 이미 게시완료 — %s 로 내리지 않습니다", rec.content_id, rec.status)
            return page_id, current_status
        props = record_properties(rec)
        if page_id:
            self._call("PATCH", f"pages/{page_id}", {"properties": props}, write=True)
            return page_id, rec.status
        body = self._call("POST", "pages", {"parent": {"database_id": self._db}, "properties": props},
                          write=True)
        new_id = str(body.get("id") or "")
        if not new_id:
            raise LedgerError("Notion 페이지 생성 응답에 id 가 없음")
        return new_id, rec.status

    def set_status(self, page_id: str, content_id: str, status: str, error: str = "",
                   video_id: str = "") -> None:
        props = {P_STATUS: _select(status), P_ERROR: _rt(redact(error)[:ERROR_MAX])}
        if video_id:
            props[P_VIDEO_ID] = _rt(video_id)
        self._call("PATCH", f"pages/{page_id}", {"properties": props}, write=True)
        log.info("원장 상태 갱신 %s → %s", content_id, status)

    def pending(self, limit: int = RECONCILE_MAX_ROWS) -> list[tuple[str, str, str, str]]:
        """확인필요 행 (page_id, 회차ID, FB영상ID, 캡션해시) — 오래된 날짜부터 limit 건.

        limit 을 넘는 행 수는 pending_overflow 에 남긴다(알림용 — 리뷰 #2-9·#3-F7·F8).
        """
        rows, has_more = self._query({
            "page_size": 100,
            "filter": {"property": P_STATUS, "select": {"equals": STATUS_PENDING}},
            "sorts": [{"property": P_DATE, "direction": "ascending"}],
        })
        self.pending_overflow = max(0, len(rows) - limit) + (1 if has_more else 0)
        out = []
        for row in rows[:limit]:
            props = row.get("properties") or {}
            out.append((str(row.get("id") or ""), _plain(props.get(P_ID)),
                        _plain(props.get(P_VIDEO_ID)).strip(), _plain(props.get(P_CAPTION_HASH)).strip()))
        return out


def load_context(token: str, database_id: str, *, today: dt.date | None = None,
                 session: Any = None) -> StoryContext:
    """build 용. 어떤 예외도 올리지 않는다 — 실패하면 available=False(원장 없이 생성, 상세설계 D1 권장안)."""
    if not token or not database_id:
        return StoryContext.unavailable("NOTION_TOKEN 또는 FACE_NOTION_DB_ID 미설정")
    try:
        rows = Ledger(token, database_id, session=session).memory_rows(config.FACE_STORY_SCAN_ROWS)
        ctx = context_from_rows(rows, config.FACE_STORY_LOOKBACK, today)
    except Exception as exc:  # noqa: BLE001 — 원장 장애는 영상 생성을 막지 않는다
        log.warning("원장 조회 실패 — 원장 없이 진행: %s", redact(str(exc)))
        return StoryContext.unavailable(redact(str(exc)) or type(exc).__name__)
    log.info("원장 조회 %d행 → 지난 이야기 %d건 · 열린 떡밥 %s · 다음 시리즈회차 %s", len(rows),
             len(ctx.episodes), ctx.open_thread.thread_id if ctx.open_thread else "-", ctx.next_series_no)
    return ctx

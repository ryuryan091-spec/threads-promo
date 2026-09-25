"""Threads 인사이트 수집 및 집계.

공식 사양 (2026-01-30 문서 기준)
  게시물  GET /{media-id}/insights?metric=views,likes,replies,reposts,quotes,shares
  사용자  GET /{user-id}/threads_insights?metric=...&since=...&until=...

응답 형태가 세 가지라 파싱을 분기한다.
  values[]            시계열 (사용자 views)
  total_value{}       총계 (likes, replies, followers_count 등)
  link_total_values[] 링크별 총계 (clicks)  <- 유입 측정의 핵심

기둥 복원
  인사이트는 게시물 ID 단위로 나오는데, 무상태 원칙상 게시물과 기둥의
  매핑이 저장되어 있지 않다. 발행 슬롯이 고정 시각이므로 timestamp 로
  슬롯을 역추론하고, 날짜 결정론 인덱스로 기둥을 복원한다.
  판정 불가한 건은 숨기지 않고 건수로 보고한다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from . import ai_writer, chat_plan, config, content, watchdog

VERSION = "1.2.0"   # v1.2.0: 이벤트 창 게시물 = STORY

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

MEDIA_METRICS = ("views", "likes", "replies", "reposts", "quotes")
USER_METRICS = ("views", "likes", "replies", "reposts", "quotes",
                "clicks", "followers_count")

# 슬롯 판정 허용 창(분).
#   v1.2.0: cron 지연을 반영해 넓힌다(12→35, 55→90). 좁으면 지연된 글이 판정불가로 빠져
#   weighting 표본이 줄고, 이벤트 STORY 가 'STORY 발행됨'으로 인식되지 않아 같은 회차가
#   두 번 나갈 수 있다(독립 리뷰 2026-09-26).
#   경계: 정기 A 08:23+35=08:58 < CHAT 09:00, 정기 B 12:47+35=13:22 < 이벤트 13:29.
#   이벤트 = 지터 최대 50분 + cron 지연 여유 40분. 23:17 창은 자정을 넘긴다(분 계산 % 1440).
PUBLISH_WINDOW_MIN = 35
EVENT_WINDOW_MIN = 90

PUBLISH_SLOTS = {"08:23": "A", "12:47": "B", "20:31": "C"}
EVENT_SLOTS = ("03:11", "13:29", "17:41", "23:17")

UNKNOWN = "판정불가"
CHAT = "CHAT"   # 오전 잡담. 시간창 + 형식으로 판정한다(chat_plan.is_chat_post).


def parse_timestamp(raw: str) -> dt.datetime | None:
    """Threads 타임스탬프 파싱. 워치독 구현을 재사용한다."""
    return watchdog.parse_threads_timestamp(raw)


@dataclass
class PostStat:
    post_id: str
    posted_at: dt.datetime
    pillar: str
    views: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0


@dataclass
class UserStat:
    profile_views: int = 0
    followers: int = 0
    likes: int = 0
    replies: int = 0
    clicks: dict[str, int] = field(default_factory=dict)


@dataclass
class PillarRow:
    pillar: str
    posts: int = 0
    replies: int = 0
    likes: int = 0
    views: int = 0

    @property
    def reply_avg(self) -> float:
        return self.replies / self.posts if self.posts else 0.0


# ---------------------------------------------------------------------------
# 응답 파싱
# ---------------------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def parse_media_insights(body: dict) -> dict[str, int]:
    """게시물 인사이트 응답에서 메트릭을 뽑는다.

    REPOST_FACADE 게시물은 빈 배열이 온다. 그 경우 전부 0 이다.
    """
    out: dict[str, int] = {}
    for item in body.get("data", []) or []:
        name = str(item.get("name", ""))
        values = item.get("values") or []
        if values:
            out[name] = int(values[0].get("value", 0))
        elif "total_value" in item:
            out[name] = int((item.get("total_value") or {}).get("value", 0))
    return out


def parse_user_insights(body: dict) -> UserStat:
    """사용자 인사이트 응답을 파싱한다. 세 가지 응답 형태를 모두 다룬다."""
    stat = UserStat()
    for item in body.get("data", []) or []:
        name = str(item.get("name", ""))

        if "link_total_values" in item:
            for link in item.get("link_total_values") or []:
                url = str(link.get("link_url", ""))
                if url:
                    stat.clicks[url] = int(link.get("value", 0))
            continue

        if "total_value" in item:
            value = int((item.get("total_value") or {}).get("value", 0))
        else:
            # 시계열은 마지막 값을 쓴다. 프로필 조회는 일별로 온다.
            values = item.get("values") or []
            value = int(values[-1].get("value", 0)) if values else 0

        if name == "views":
            stat.profile_views = value
        elif name == "followers_count":
            stat.followers = value
        elif name == "likes":
            stat.likes = value
        elif name == "replies":
            stat.replies = value

    return stat


# ---------------------------------------------------------------------------
# 기둥 복원
# ---------------------------------------------------------------------------


def discriminator_from_timestamp(posted_at: dt.datetime) -> int | None:
    """발행 시각으로 실행 구분자를 역추론한다.

    정기 슬롯은 고정 시각 + 최대 8분 지터라 창이 좁다.
    이벤트는 최대 50분 지터라 창이 넓고, 정기 슬롯과 겹치지 않게 배치되어 있다.
    어느 창에도 들지 않으면 수동 실행이거나 슬롯 변경 이전 게시물이다.
    """
    local = posted_at.astimezone(KST)
    minute_of_day = local.hour * 60 + local.minute

    for hhmm, slot in PUBLISH_SLOTS.items():
        if (minute_of_day - _minutes(hhmm)) % 1440 <= PUBLISH_WINDOW_MIN:
            return config.DISCRIMINATOR_BY_SLOT[slot]

    for hhmm in EVENT_SLOTS:
        if (minute_of_day - _minutes(hhmm)) % 1440 <= EVENT_WINDOW_MIN:
            return config.DISCRIMINATOR_EVENT

    return None


def restore_pillar(posted_at: dt.datetime, media_type: str = "") -> str:
    """게시물 발행 시각(+형식)에서 기둥을 복원한다. 불가하면 UNKNOWN."""
    if chat_plan.is_chat_post(posted_at, media_type):
        return CHAT
    disc = discriminator_from_timestamp(posted_at)
    if disc is None:
        return UNKNOWN
    if disc == config.DISCRIMINATOR_EVENT:
        # v1.2.0: 이벤트 경로는 기둥을 STORY 로 강제한다(run_story).
        return "STORY"
    idx = content.run_index(posted_at.astimezone(KST).date(), disc)
    return ai_writer.pick_pillar(idx)


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------


def aggregate(posts: list[PostStat]) -> list[PillarRow]:
    """기둥별로 묶는다. 판정 불가 건도 별도 행으로 남긴다."""
    rows: dict[str, PillarRow] = defaultdict(lambda: PillarRow(pillar=""))
    for post in posts:
        row = rows[post.pillar]
        row.pillar = post.pillar
        row.posts += 1
        row.replies += post.replies
        row.likes += post.likes
        row.views += post.views

    order = [*ai_writer.PILLAR_ROTATION]
    seen: list[str] = []
    for key in order:
        if key not in seen:
            seen.append(key)
    seen.append(CHAT)
    seen.append(UNKNOWN)

    return [rows[k] if k in rows else PillarRow(pillar=k) for k in seen]


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------


def render_report(
    today: dt.date,
    user: UserStat,
    rows: list[PillarRow],
    lookback_days: int,
    follower_delta: int | None = None,
    failed_posts: int = 0,
) -> str:
    """텔레그램·Actions Summary 용 리포트.

    답글을 먼저 보여준다. 도달을 결정하는 것은 조회가 아니라 답글이므로
    지표 배치가 판단을 유도해야 한다.
    """
    lines = [f"[Threads 일일 리포트] {today.isoformat()}", ""]

    delta = f" ({follower_delta:+d})" if follower_delta is not None else ""
    lines.append(f"팔로워 {user.followers}{delta}")
    lines.append(f"프로필 조회 {user.profile_views}")

    if user.clicks:
        lines += ["", "링크 클릭"]
        for url, value in sorted(user.clicks.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {_shorten(url):22s} {value}")
    else:
        lines += ["", "링크 클릭 없음"]

    total_posts = sum(r.posts for r in rows)
    lines += ["", f"최근 {lookback_days}일 기둥별 (게시물 {total_posts}건)"]
    lines.append(f"  {'기둥':10s} {'발행':>4s} {'답글':>4s} {'좋아요':>5s} {'조회':>6s}")
    for row in rows:
        if row.pillar in (UNKNOWN, CHAT) and row.posts == 0:
            continue
        lines.append(
            f"  {row.pillar:10s} {row.posts:4d} {row.replies:4d} "
            f"{row.likes:5d} {row.views:6d}"
        )

    ranked = [r for r in rows if r.posts and r.pillar != UNKNOWN]
    if ranked:
        best = max(ranked, key=lambda r: r.reply_avg)
        lines += ["", f"답글 최다: {best.pillar} (평균 {best.reply_avg:.1f})"]

    if failed_posts:
        lines.append(f"조회 실패 {failed_posts}건은 집계에서 제외")

    return "\n".join(lines)


def _shorten(url: str) -> str:
    """리포트 표시용 축약. 도메인과 핸들만 남긴다."""
    trimmed = url.replace("https://", "").replace("http://", "").rstrip("/")
    if trimmed.startswith("www."):
        trimmed = trimmed[4:]
    return trimmed[:22]

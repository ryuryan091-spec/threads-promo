"""워치독 — 파이프라인이 조용히 멈춘 것을 감지한다.

기존 알림과의 차이
  발행 워크플로우는 '실행되었는데 실패한 것'만 알린다.
  워크플로우가 아예 실행되지 않으면(cron 정지, 레포 비활성, GitHub 장애)
  아무 소리도 나지 않는다. 자기 자신을 감시할 수 없는 구조다.

  워치독은 Threads API 를 외부 기준으로 삼아 '오늘 발행이 있었는가'를 확인한다.
  Threads 가 상태 저장소 역할을 하므로 DB 없이 동작한다.

설계 원칙
  조용한 감시자여야 한다. 정상일 때는 아무것도 보내지 않는다.
  감시자가 시끄러우면 사람이 알림을 무시하게 되고, 그러면 감시 자체가 무의미해진다.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from . import config

VERSION = "1.1.0"

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 임계값
#
# 발행은 일 1회지만 슬롯이 08:23 / 12:47 / 20:31 로 분산되어 있다.
# 전날 08:23 에 나가고 다음날 20:31 에 나가면 정상인데도 간격이 36시간 8분이다.
# 임계를 30시간으로 두면 이 정상 케이스가 오탐으로 잡힌다.
#
# 따라서 임계값은 고정하지 않고 슬롯 배치와 휴식일 설정에서 산출한다.
# ---------------------------------------------------------------------------
SLOT_SPREAD_MAX_GAP_HOURS = 36.2   # 08:23 -> 다음날 20:31
JITTER_MARGIN_HOURS = 0.5          # 발행 전 랜덤 지연 최대 8분 + 여유
BASE_STALE_MARGIN_HOURS = 2.0      # 실행 지연·API 지연 흡수

REPLY_STALE_HOURS = 72      # 답글은 대상이 없으면 안 나가므로 넉넉히
QUOTA_ALARM_RATIO = 0.5     # 발행 쿼터를 절반 넘게 쓰면 이상 징후
# 발행 신선도 판정용 (목록 1회 호출). CHAT 도입 후 하루 약 10건이라
# 3건이면 정기 글이 CHAT 에 가려 보이지 않는다.
RECENT_POSTS_TO_SCAN = 25
CHAT_CHECK_AFTER = "12:30"    # KST. CHAT 창 종료 후에만 무발행을 판정한다
CONVERSATION_SCAN_LIMIT = 1   # 답글 활동 확인용. 호출 수를 줄이려 최신 글만 본다.


def stale_threshold_hours(rest_days_per_week: int = 0) -> float:
    """발행 지연 판정 임계값을 슬롯 배치에서 산출한다.

    고정값을 쓰면 슬롯 시각이나 휴식일 설정을 바꿀 때마다 오탐이 생긴다.
    설정에서 계산하면 그 문제가 구조적으로 사라진다.
    """
    base = (
        SLOT_SPREAD_MAX_GAP_HOURS
        + JITTER_MARGIN_HOURS
        + BASE_STALE_MARGIN_HOURS
    )
    return base + max(0, rest_days_per_week) * 24.0


# 하위 호환 및 기본 참조값
POST_STALE_HOURS = stale_threshold_hours(0)


class Severity:
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass
class Finding:
    severity: str
    title: str
    detail: str


@dataclass
class WatchReport:
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def has_alert(self) -> bool:
        return any(f.severity != Severity.OK for f in self.findings)

    @property
    def worst(self) -> str:
        if any(f.severity == Severity.CRITICAL for f in self.findings):
            return Severity.CRITICAL
        if any(f.severity == Severity.WARN for f in self.findings):
            return Severity.WARN
        return Severity.OK

    def to_message(self) -> str:
        """경보 메시지. 정상이면 빈 문자열."""
        alerts = [f for f in self.findings if f.severity != Severity.OK]
        if not alerts:
            return ""

        prefix = "[최우선]" if self.worst == Severity.CRITICAL else "[경고]"
        lines = [f"[Threads Watchdog]{prefix} 이상 {len(alerts)}건"]
        for f in alerts:
            lines.append(f"\n· {f.title}\n  {f.detail}")
        return "".join(lines) if len(lines) == 1 else "\n".join(lines)


def parse_threads_timestamp(raw: str) -> dt.datetime | None:
    """Threads API 타임스탬프를 파싱한다.

    형식이 바뀔 수 있으므로 실패해도 예외를 올리지 않는다.
    파싱 못 하면 그 항목은 검사에서 제외한다.
    """
    if not raw:
        return None
    text = raw.strip()
    # '+0000' 형태를 fromisoformat 이 읽을 수 있게 보정
    if len(text) >= 5 and (text[-5] in "+-") and ":" not in text[-5:]:
        text = f"{text[:-2]}:{text[-2:]}"
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        log.warning("타임스탬프 파싱 실패: %s", raw[:40])
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def hours_since(when: dt.datetime, now: dt.datetime) -> float:
    return (now - when).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# 개별 검사
# ---------------------------------------------------------------------------


def check_publish_freshness(
    posts: list[dict],
    now: dt.datetime,
    threshold_hours: float | None = None,
) -> Finding:
    """마지막 발행이 얼마나 됐는지. 워치독의 존재 이유."""
    if threshold_hours is None:
        threshold_hours = stale_threshold_hours(config.PUBLISH_WEEKLY_REST_DAYS)

    if not posts:
        return Finding(
            Severity.CRITICAL,
            "발행 이력 없음",
            "Threads 에서 내 글을 하나도 찾지 못했습니다. "
            "발행이 한 번도 안 됐거나 계정·토큰에 문제가 있습니다.",
        )

    stamps = [
        parsed
        for post in posts
        if (parsed := parse_threads_timestamp(str(post.get("timestamp", ""))))
    ]
    if not stamps:
        return Finding(
            Severity.WARN,
            "발행 시각 확인 불가",
            "글은 있으나 timestamp 를 읽지 못했습니다. API 응답 형식을 확인하십시오.",
        )

    latest = max(stamps)
    elapsed = hours_since(latest, now)

    # CRITICAL 은 하루치를 더 넘긴 경우로 둔다.
    # 두 배로 잡으면 휴식일 설정에 따라 임계가 과하게 벌어진다.
    if elapsed > threshold_hours + 24:
        return Finding(
            Severity.CRITICAL,
            "발행 장기 중단",
            f"마지막 발행 후 {elapsed:.0f}시간 경과 "
            f"({latest.astimezone().strftime('%Y-%m-%d %H:%M')}). "
            "cron 정지·레포 비활성·토큰 문제를 확인하십시오.",
        )
    if elapsed > threshold_hours:
        return Finding(
            Severity.WARN,
            "발행 지연",
            f"마지막 발행 후 {elapsed:.0f}시간 경과. "
            f"정상이라면 {threshold_hours:.0f}시간 이내에 새 글이 있어야 합니다. "
            "(슬롯 분산 기준 최대 정상 간격 36시간 8분)",
        )
    return Finding(Severity.OK, "발행 정상", f"마지막 발행 {elapsed:.1f}시간 전")


def check_reply_activity(
    owned_reply_stamps: list[dt.datetime],
    now: dt.datetime,
    *,
    enabled: bool,
    threshold_hours: float = REPLY_STALE_HOURS,
) -> Finding:
    """답글 엔진이 살아 있는지.

    답글은 대상 댓글이 없으면 나가지 않는 것이 정상이다.
    따라서 임계를 넉넉히 두고 WARN 까지만 올린다.
    """
    if not enabled:
        return Finding(Severity.OK, "답글 비활성", "REPLY_ENABLED=false — 검사 생략")

    if not owned_reply_stamps:
        return Finding(
            Severity.OK,
            "답글 이력 없음",
            "최근 글에 내 답글이 없습니다. 대상 댓글이 없었을 수 있습니다.",
        )

    elapsed = hours_since(max(owned_reply_stamps), now)
    if elapsed > threshold_hours:
        return Finding(
            Severity.WARN,
            "답글 활동 정체",
            f"마지막 답글 후 {elapsed:.0f}시간 경과. "
            "댓글이 없었을 수도 있으나, 워크플로우 실행 여부를 확인하십시오.",
        )
    return Finding(Severity.OK, "답글 정상", f"마지막 답글 {elapsed:.1f}시간 전")


def check_chat_activity(
    chat_today: int, now: dt.datetime, *, enabled: bool
) -> Finding:
    """CHAT 이 켜져 있는데 오늘 한 건도 없으면 경고한다.

    CHAT 은 목표가 soft(cron 누락 허용)라 건수 미달은 경보하지 않는다. 0건만 본다.
    창이 끝나기 전에는 판정하지 않는다.
    """
    if not enabled:
        return Finding(Severity.OK, "CHAT 비활성", "CHAT_ENABLED=false — 검사 생략")

    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo("Asia/Seoul"))
    hh, mm = (int(x) for x in CHAT_CHECK_AFTER.split(":"))
    if (local.hour, local.minute) < (hh, mm):
        return Finding(Severity.OK, "CHAT 판정 보류", f"{CHAT_CHECK_AFTER} 이전")

    if chat_today == 0:
        return Finding(
            Severity.WARN,
            "CHAT 무발행",
            "오늘 CHAT 창에 발행된 글이 없습니다. chat.yml 실행 여부, "
            "CLAUDE_AI_KEY, 린트 실패 로그를 확인하십시오.",
        )
    return Finding(Severity.OK, "CHAT 정상", f"오늘 {chat_today}건")


def check_quota(used: int, total: int) -> Finding:
    """쿼터 급증은 중복 발행이나 폭주를 뜻할 수 있다."""
    if total <= 0:
        return Finding(Severity.WARN, "쿼터 조회 이상", "총 쿼터가 0으로 보고되었습니다.")

    ratio = used / total
    if ratio >= QUOTA_ALARM_RATIO:
        return Finding(
            Severity.CRITICAL,
            "발행 쿼터 급증",
            f"24시간 내 {used}/{total} 사용. 일 1~2회 발행 설계와 맞지 않습니다. "
            "중복 실행이나 슬롯 매핑 오류를 확인하십시오.",
        )
    return Finding(Severity.OK, "쿼터 정상", f"{used}/{total}")


def check_token_expiry(today: dt.date, issued_at: str) -> Finding:
    """영속화가 안 된 상태에서 토큰 만료가 다가오는지."""
    from . import token_manager

    assessment = token_manager.assess_expiry(today, issued_at)

    if assessment.level in ("critical",):
        return Finding(Severity.CRITICAL, "토큰 만료 임박", assessment.message)
    if assessment.level in ("urgent", "warn"):
        return Finding(Severity.WARN, "토큰 만료 접근", assessment.message)
    if assessment.level == "unknown":
        # 발급일 미상은 워치독에서 반복 경보하지 않는다.
        # 발행 워크플로우가 이미 알리고 있으므로 중복이다.
        return Finding(Severity.OK, "토큰 발급일 미상", assessment.message)
    return Finding(Severity.OK, "토큰 정상", assessment.message)


def build_report(findings: list[Finding]) -> WatchReport:
    report = WatchReport(findings=findings)
    report.checked = [f.title for f in findings]
    return report

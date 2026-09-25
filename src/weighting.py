"""기둥 비중 자동 조절.

설계 의도
  이 모듈의 본질은 "자동으로 바꾸는 것"이 아니라
  **"바꿔도 되는 조건을 엄격히 정의하는 것"** 이다.

  표본이 부족한 상태에서 조절을 켜면 노이즈를 따라 비중이 진동하고,
  어느 기둥도 충분한 표본을 쌓지 못한다. 자동화가 학습을 방해한다.

안전장치 7종
  S1 최소 표본     기둥당 10건
  S2 조정 주기     30일 1회
  S3 조정 폭       1회 ±1칸
  S4 기둥 하한     각 1칸
  S5 PROMO 상한    2칸
  S6 유의 임계     1위가 2위의 1.5배 이상
  S7 STORY 하한    2칸 (근거 보유 기둥 보호)

점수 산식
  score = 일평균클릭 × WEIGHT_SCORE_CLICKS(현재 0.0) + 일평균답글 × 0.3
  클릭은 계정 합계만 제공되어 기둥 귀속이 불가능하다(config 주석 참고).
  조회·좋아요는 제외한다. 행동으로 이어지지 않기 때문이다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field

from . import ai_writer, config

VERSION = "1.1.0"   # v1.1.0: 로테이션 판정 ai_writer 위임, AUTO 분리

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PillarScore:
    pillar: str
    posts: int
    clicks: int
    replies: int

    @property
    def click_avg(self) -> float:
        return self.clicks / self.posts if self.posts else 0.0

    @property
    def reply_avg(self) -> float:
        return self.replies / self.posts if self.posts else 0.0

    @property
    def score(self) -> float:
        return (
            self.click_avg * config.WEIGHT_SCORE_CLICKS
            + self.reply_avg * config.WEIGHT_SCORE_REPLIES
        )


@dataclass
class AdjustResult:
    adjusted: bool
    reason: str
    scores: list[PillarScore] = field(default_factory=list)
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    promoted: str = ""
    demoted: str = ""
    next_check: dt.date | None = None


# ---------------------------------------------------------------------------
# 로테이션 해석
# ---------------------------------------------------------------------------


def current_rotation() -> tuple[str, ...]:
    """현재 적용 중인 로테이션. 판정은 ai_writer.active_rotation 단일 진입점에 위임한다.

    v1.1.0: OVERRIDE > AUTO > 기본. 자체 파싱을 두면 발행 경로와 판정이 어긋난다.
    """
    return ai_writer.active_rotation()


# ---------------------------------------------------------------------------
# 불변식 (S4 S5 S7 + 연속 중복)
# ---------------------------------------------------------------------------


def validate_rotation(rotation: tuple[str, ...]) -> list[str]:
    """로테이션이 지켜야 할 조건. 위반 항목명을 돌려준다(ai_writer 에 위임)."""
    return ai_writer.rotation_violations(rotation)


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------


def _gate(
    scores: list[PillarScore], today: dt.date, last_adjust: str
) -> tuple[bool, str, dt.date | None]:
    """조정해도 되는 상태인지. (통과, 사유, 다음판정일)"""
    if not config.ADAPTIVE_WEIGHTS_ENABLED:
        return False, "ADAPTIVE_WEIGHTS_ENABLED=false — 리포트만 발송", None

    if config.PILLAR_ROTATION_OVERRIDE:
        return False, "수동 로테이션이 지정되어 있어 자동 조절을 건너뜁니다", None

    # S2 조정 주기
    if last_adjust:
        try:
            last = dt.date.fromisoformat(last_adjust)
        except ValueError:
            log.warning("LAST_WEIGHT_ADJUST 형식 오류: %s", last_adjust)
        else:
            nxt = last + dt.timedelta(days=config.WEIGHT_ADJUST_INTERVAL_DAYS)
            if today < nxt:
                return False, f"조정 주기 미도래 (다음 {nxt})", nxt

    # S1 최소 표본
    short = [s.pillar for s in scores if s.posts < config.WEIGHT_MIN_SAMPLE]
    if short:
        detail = ", ".join(
            f"{s.pillar} {s.posts}건" for s in scores if s.pillar in short
        )
        return (
            False,
            f"표본 부족 — {detail} (최소 {config.WEIGHT_MIN_SAMPLE}건)",
            None,
        )

    # S6 유의 임계
    ranked = sorted(scores, key=lambda s: -s.score)
    if len(ranked) < 2:
        return False, "비교 대상 부족", None

    top, second = ranked[0], ranked[1]
    if second.score <= 0:
        if top.score <= 0:
            return False, "전 기둥 성과 0 — 조정 근거 없음", None
    elif top.score < second.score * config.WEIGHT_SIGNIFICANCE_RATIO:
        ratio = top.score / second.score
        return (
            False,
            f"차이 미미 — 1위가 2위의 {ratio:.2f}배 "
            f"(임계 {config.WEIGHT_SIGNIFICANCE_RATIO}배)",
            None,
        )

    return True, "", None


# ---------------------------------------------------------------------------
# 조정
# ---------------------------------------------------------------------------


def build_rotation(counts: dict[str, int], size: int) -> tuple[str, ...]:
    """칸 수만 정해주면 연속 중복 없이 배치한다.

    단순 치환(한 칸을 바꾸는 방식)은 실패하는 배치가 존재한다.
    예: M,P,M,S,M,P,M,S 에서 M 하나를 S 로 바꾸려 하면
    모든 M 이 S 와 인접해 있어 어느 위치도 쓸 수 없다.

    그래서 치환이 아니라 재배치로 푼다.
    매 칸마다 남은 수가 가장 많은 기둥을 고르되, 직전과 같으면 차선을 쓴다.
    이 방식은 어떤 칸 수 조합에서도 간격을 최대로 벌린다.
    """
    remaining = dict(counts)
    out: list[str] = []

    for _ in range(size):
        # 남은 수 내림차순. 동수면 이름순으로 안정 정렬한다(결정론 유지).
        candidates = sorted(
            (p for p, n in remaining.items() if n > 0),
            key=lambda p: (-remaining[p], p),
        )
        if not candidates:
            break

        pick = candidates[0]
        if out and pick == out[-1] and len(candidates) > 1:
            pick = candidates[1]

        out.append(pick)
        remaining[pick] -= 1

    # 순환 경계 확인. 첫 칸과 마지막 칸이 같으면 뒤쪽에서 다른 기둥과 맞바꾼다.
    if len(out) > 2 and out[0] == out[-1]:
        for i in range(len(out) - 2, 0, -1):
            if out[i] != out[0] and out[i - 1] != out[-1]:
                out[i], out[-1] = out[-1], out[i]
                break

    return tuple(out)


def decide(
    scores: list[PillarScore], today: dt.date, last_adjust: str
) -> AdjustResult:
    """조정 여부와 새 로테이션을 판정한다."""
    before = current_rotation()

    passed, reason, nxt = _gate(scores, today, last_adjust)
    if not passed:
        return AdjustResult(
            adjusted=False, reason=reason, scores=scores,
            before=before, after=before, next_check=nxt,
        )

    ranked = sorted(scores, key=lambda s: -s.score)
    promote, demote = ranked[0].pillar, ranked[-1].pillar

    if promote == demote:
        return AdjustResult(
            adjusted=False, reason="1위와 최하위가 동일", scores=scores,
            before=before, after=before,
        )

    counts = Counter(before)
    counts[promote] += config.WEIGHT_ADJUST_STEP
    counts[demote] -= config.WEIGHT_ADJUST_STEP

    after = build_rotation(dict(counts), len(before))
    if after == before:
        return AdjustResult(
            adjusted=False,
            reason="재배치 결과가 기존과 동일",
            scores=scores, before=before, after=before,
        )

    violations = validate_rotation(after)
    if violations:
        return AdjustResult(
            adjusted=False,
            reason=f"불변식 위반으로 조정 취소 — {', '.join(violations)}",
            scores=scores, before=before, after=before,
        )

    return AdjustResult(
        adjusted=True, reason="", scores=scores,
        before=before, after=after,
        promoted=promote, demoted=demote,
        next_check=today + dt.timedelta(days=config.WEIGHT_ADJUST_INTERVAL_DAYS),
    )


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------


def render(result: AdjustResult, today: dt.date, window_days: int) -> str:
    lines = [f"[Threads 비중 조정] {today.isoformat()}", ""]
    lines.append(f"{window_days}일 집계")
    lines.append(
        f"  {'기둥':8s} {'발행':>4s} {'클릭':>4s} {'답글':>4s} {'score':>7s}"
    )
    for s in sorted(result.scores, key=lambda x: -x.score):
        lines.append(
            f"  {s.pillar:8s} {s.posts:4d} {s.clicks:4d} {s.replies:4d} "
            f"{s.score:7.2f}"
        )

    lines.append("")
    if result.adjusted:
        before_n = Counter(result.before)
        after_n = Counter(result.after)
        lines.append(f"판정: {result.promoted} 상향, {result.demoted} 하향")
        lines.append("")
        lines.append("조정")
        for pillar in sorted(set(result.before) | set(result.after)):
            b, a = before_n.get(pillar, 0), after_n.get(pillar, 0)
            mark = " <-" if b != a else ""
            lines.append(f"  {pillar:8s} {b}칸 -> {a}칸{mark}")
        lines.append("")
        lines.append(f"새 로테이션: {','.join(result.after)}")
        if result.next_check:
            lines.append(f"다음 조정 가능일: {result.next_check}")
    else:
        lines.append(f"조정 보류 — {result.reason}")
        if result.next_check:
            lines.append(f"다음 판정: {result.next_check}")

    return "\n".join(lines)

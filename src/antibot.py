"""안티봇 안전장치.

원칙 (기존 X 운영에서 확립된 것을 그대로 적용)
  1. 동일 일정·동일 문구 금지. 시각과 본문을 모두 랜덤화한다.
  2. 발행 간격은 고정 sleep 이 아니라 랜덤 지연을 쓴다.
  3. 일일 발행 상한을 둔다.
  4. cron 은 정각/반각을 피한다. (혼잡 시간대 fire rate 저하)

GitHub Actions 비용 주의
  sleep 도 Actions 분을 소모한다. 따라서 지터를 길게 잡지 않고,
  '여러 슬롯 중 하루 하나만 실행' 방식으로 시각 분산을 얻는다.
  선택되지 않은 슬롯은 즉시 종료하므로 분 소모가 거의 없다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
import time

VERSION = "1.0.0"

log = logging.getLogger(__name__)


def _day_seed(today: dt.date, salt: str) -> int:
    """날짜+용도로 결정론적 시드를 만든다. 같은 날 재실행 시 동일 판단."""
    raw = f"{today.isoformat()}::{salt}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def choose_slot(today: dt.date, slots: list[str], salt: str = "publish") -> str:
    """오늘 실행할 슬롯 하나를 고른다.

    slots 는 워크플로우가 넘기는 슬롯 이름 목록이다.
    날짜 기반이므로 같은 날 재실행해도 같은 슬롯이 선택된다.
    """
    if not slots:
        raise ValueError("슬롯 목록이 비어 있습니다.")
    rng = random.Random(_day_seed(today, salt))
    return rng.choice(slots)


def should_run_this_slot(
    today: dt.date, current_slot: str, slots: list[str], salt: str = "publish"
) -> bool:
    """현재 슬롯이 오늘의 당첨 슬롯인지 판정한다."""
    chosen = choose_slot(today, slots, salt)
    decision = chosen == current_slot
    log.info(
        "슬롯 판정 — 오늘 선택=%s 현재=%s 실행=%s",
        chosen, current_slot, "예" if decision else "아니오",
    )
    return decision


def jitter_sleep(
    min_sec: int, max_sec: int, *, label: str = "", dry_run: bool = False
) -> int:
    """랜덤 지연. 고정 sleep 을 쓰지 않기 위한 함수.

    시드를 고정하지 않는다. 매 실행마다 달라져야 패턴이 생기지 않는다.
    반환값은 실제 대기한 초.
    """
    if min_sec < 0 or max_sec < min_sec:
        raise ValueError(f"잘못된 지연 범위: {min_sec}~{max_sec}")

    seconds = random.randint(min_sec, max_sec)
    log.info("랜덤 지연 %d초%s", seconds, f" ({label})" if label else "")

    if dry_run:
        log.info("DRY_RUN — 실제로 대기하지 않습니다.")
        return 0

    time.sleep(seconds)
    return seconds


def within_daily_cap(used: int, cap: int, *, label: str = "") -> bool:
    """일일 상한 확인. API 쿼터와 별개로 자체 상한을 둔다."""
    ok = used < cap
    if not ok:
        log.warning("일일 상한 도달 — %s %d/%d", label or "작업", used, cap)
    return ok


def shuffled(items: list, *, seed: int | None = None) -> list:
    """처리 순서를 섞는다. 항상 같은 순서로 도는 패턴을 없앤다."""
    copy = list(items)
    rng = random.Random(seed) if seed is not None else random
    rng.shuffle(copy)
    return copy

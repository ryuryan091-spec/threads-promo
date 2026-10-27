"""
p76/cli.py
==========
파이프라인 공통 진입점: 로깅·마스킹·시작 로그·킬 스위치·실행 기록·예외 방어.
성공·실패·예외 어느 경로든 실행 기록은 정확히 1번 남는다(FR-06).
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from p76 import safety, settings
from p76.notify import send
from p76.redact import redact, register_masks
from p76.runlog import RunLog

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(levelname)s %(message)s")
    for name in ("urllib3", "requests"):
        logging.getLogger(name).setLevel(logging.WARNING)  # 요청 URL(토큰 포함) 로그 차단 (STD-SEC-01)
    register_masks()


def execute(pipeline: str, version: str, body: Callable[[RunLog], None], require_enabled: bool = True) -> int:
    safety.start_banner(pipeline, version)
    if require_enabled and not settings.ENABLED:
        logger.info(f"[{pipeline}] PAGE76_ENABLED=false — 종료")
        return safety.EXIT_OK
    log = RunLog(pipeline, version)
    try:
        body(log)
    except Exception as e:  # noqa: BLE001 — 마지막 방어선: 메시지는 마스킹
        logger.error(f"[{pipeline}] 예외 {type(e).__name__}: {redact(e)[:200]}")
        log.fail(f"exception:{type(e).__name__}")
        send(f"{pipeline} 실행 예외 {type(e).__name__} (run {settings.RUN_ID})")
    code = log.finish()
    if safety.tripped():
        send(f"{pipeline} 회로 차단 — {safety.tripped()}. 토큰·권한 확인 후 fb-page-publisher 와 함께 교체", urgent=True)
    return code

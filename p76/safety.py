"""
p76/safety.py
=============
킬 스위치 · 모드 · 회로 차단 (DSN §9, SEC C-12·C-21).

  dry_run : Graph·노션 쓰기 없음 (읽기만)
  shadow  : 노션 기록만 (Graph 쓰기 없음)
  live    : 전부
회로 차단: Graph code 190(토큰) · 10/200대(권한) · 368(정책 차단) → 그 실행의 이후 Graph 쓰기 0건, 종료코드 7.
  노션 쓰기(결과 기록)는 막지 않는다 — 막으면 원장이 발행중으로 남는다.
"""
from __future__ import annotations

import logging

from p76 import settings

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

EXIT_OK, EXIT_ERROR, EXIT_TRIPPED = 0, 1, 7

_tripped: str = ""


class Tripped(RuntimeError):
    """회로 차단 이후 쓰기 시도."""


def is_trip_code(code: int | None, http_status: int = 0) -> bool:
    if code == 190 or http_status == 401:
        return True
    if code == 10 or code == 368:
        return True
    return code is not None and 200 <= code <= 299


def trip(reason: str) -> None:
    global _tripped
    if not _tripped:
        logger.error(f"[Safety] 회로 차단: {reason} — 이번 실행의 쓰기를 모두 멈춘다")
        _tripped = reason


def tripped() -> str:
    return _tripped


def reset() -> None:
    """테스트용."""
    global _tripped
    _tripped = ""


def notion_write_allowed() -> bool:
    return settings.MODE in ("shadow", "live")


def graph_write_allowed() -> bool:
    return settings.MODE == "live" and not _tripped


def guard_graph_write() -> None:
    if _tripped:
        raise Tripped(_tripped)
    if settings.MODE != "live":
        raise Tripped(f"mode:{settings.MODE}")


def guard_notion_write() -> None:
    if settings.MODE not in ("shadow", "live"):
        raise Tripped(f"mode:{settings.MODE}")


def start_banner(pipeline: str, version: str) -> None:
    """STD-OBS-01 시작 로그. 모드 오타는 dry_run 으로 실행하고 경고한다(STD-MODE-06)."""
    if settings.MODE_INVALID:
        logger.warning("[Safety] PAGE76_MODE 값이 dry_run/shadow/live 가 아님 — dry_run 으로 실행")
    logger.info(
        f"[Start] pipeline={pipeline} version={version} mode={settings.MODE} "
        f"enabled={str(settings.ENABLED).lower()} run={settings.RUN_ID}"
    )

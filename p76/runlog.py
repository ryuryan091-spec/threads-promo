"""
p76/runlog.py
=============
실행 기록 — 한 실행 = DB-11 1행 (STD-OBS-05, SEC C-17).
dry_run 은 노션에 쓰지 않으므로 기록하지 않는다(로그만). 기록 실패는 실행 결과를 바꾸지 않는다.

    run = RunLog("calendar", VERSION)
    ... run.writes += 1 ... run.degrade("code")
    return run.finish()        # 종료코드
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from p76 import notion, safety, settings
from p76.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger(__name__)


class RunLog:
    def __init__(self, pipeline: str, version: str):
        self.pipeline = pipeline
        self.version = version
        self.started = datetime.now(UTC)
        self.writes = 0
        self.codes: list[str] = []
        self.failed = False
        self.recorded = False

    def degrade(self, code: str) -> None:
        self.codes.append(code)

    def fail(self, code: str) -> None:
        self.failed = True
        self.codes.append(code)

    @property
    def status(self) -> str:
        if safety.tripped():
            return "tripped"
        if self.failed:
            return "failed"
        return "degraded" if self.codes else "ok"

    def exit_code(self) -> int:
        status = self.status
        if status == "tripped":
            return safety.EXIT_TRIPPED
        return safety.EXIT_ERROR if status == "failed" else safety.EXIT_OK

    def finish(self) -> int:
        status = self.status
        codes = list(self.codes)
        if safety.tripped() and safety.tripped() not in codes:
            codes.insert(0, safety.tripped())
        error_code = redact(";".join(codes))[:500]
        logger.info(f"[Run] pipeline={self.pipeline} status={status} writes={self.writes} codes={error_code or '-'}")
        self._record(status, error_code)
        return self.exit_code()

    def _record(self, status: str, error_code: str) -> None:
        if self.recorded:
            return
        self.recorded = True
        if settings.MODE == "dry_run" or not settings.ENABLED or not settings.DB11_ID:
            return
        props = {
            settings.R_RUN_ID: notion.v_title(f"{settings.RUN_ID}-{self.pipeline}"),
            settings.R_PIPELINE: notion.v_select(self.pipeline),
            settings.R_VERSION: notion.v_rich(self.version),
            settings.R_MODE: notion.v_select(settings.MODE),
            settings.R_STATUS: notion.v_select(status),
            settings.R_ERROR: notion.v_rich(error_code),
            settings.R_WRITES: notion.v_number(self.writes),
            settings.R_START: notion.v_date(self.started),
            settings.R_END: notion.v_date(datetime.now(UTC)),
        }
        try:
            notion.create_page(settings.DB11_ID, props)
        except notion.NotionError as e:
            logger.warning(f"[Run] 실행 기록 실패(무시): {e}")

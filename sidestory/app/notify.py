"""P4 run notification text (success and failure). Pure: report JSON in, message out.

The workflow sends the text to the internal Telegram channel; this module never sees a
Telegram secret. Empty output = nothing to send (e.g. a later publish attempt on an
episode that is already published).

CLI: python -m sidestory.app.notify --report FILE --stage STAGE --date YYYY-MM-DD
     --run-url URL [--final]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))
MAX_REASON = 500
RELEASE_LABEL = "p1 / publish: release a held episode and resume from its last artifact"
NOTIFY_STAGES = ("p1", "publish")


def _kst(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone(KST).strftime("%H:%M KST")
    except ValueError:
        return "-"


def _clip(text: Any) -> str:
    text = str(text or "-").strip()
    return text if len(text) <= MAX_REASON else text[:MAX_REASON] + "…"


def _skip_reason(result: dict[str, Any]) -> str:
    reason = (result.get("detail") or {}).get("reason")
    if reason:
        return reason
    skipped = [g.get("reason") for g in result.get("gates") or [] if g.get("skip")]
    return "; ".join(r for r in skipped if r) or "-"


def build(report: dict[str, Any] | None, *, stage: str, side_date: str, run_url: str,
          final: bool = False) -> str | None:
    head = f"외전 {side_date}"
    log = f"로그: {run_url}"
    if not report or not isinstance(report.get("results"), list) or not report["results"]:
        if stage not in NOTIFY_STAGES and report is not None:
            return None
        why = (report or {}).get("error") or (report or {}).get("status") or "결과 없음(실행 중단)"
        return f"🚨 {head} {stage} 실행 실패\n사유: {_clip(why)}\n{log}"
    if stage not in NOTIFY_STAGES:
        return None
    last = report["results"][-1]
    status, detail = last.get("status"), last.get("detail") or {}
    title = (report.get("summary") or {}).get("title")
    named = f"{head} 「{title}」" if title else head
    reason = _clip(detail.get("reason"))

    if stage == "p1":
        if status == "skipped":
            return f"ℹ️ {head} 제작 건너뜀\n사유: {_clip(_skip_reason(last))}\n{log}"
        if status in {"hold", "error"}:
            return (f"🚨 {named} 제작 중단 [{last.get('stage')}]\n사유: {reason}\n"
                    f"조치: 원인 확인 후 p1 재실행 (\"{RELEASE_LABEL}\" ☑)\n{log}")
        if status == "assembled":
            return (f"✅ {named} 제작 완료\n조립 완료 후 60분이 지나면 다음 예약 실행에서 "
                    f"자동 게시됩니다.\n{log}")
        return f"ℹ️ {named} 제작 상태: {status}\n{log}"

    # publish (up to three scheduled attempts: repeat-free unless it is the last one)
    if detail.get("reason") == "already published":
        return None
    if (status == "skipped" or detail.get("still_on_hold")) and not final:
        return None
    if status == "published":
        post = detail.get("post_id") or ""
        link = f"https://www.facebook.com/{post}" if post else "-"
        extra = " (이전 게시물 대조로 확인)" if detail.get("reconciled") else ""
        return f"✅ {named} Facebook 게시 완료{extra}\n게시물: {link}\n{log}"
    if status == "skipped":
        return f"ℹ️ {head} 게시 건너뜀\n사유: {reason}\n{log}"
    if detail.get("waiting"):
        if final:
            return (f"🚨 {named} 게시 보류: 마지막 예약 시도까지 게시 조건 미충족\n사유: {reason}\n"
                    f"조치: publish 수동 실행\n{log}")
        return (f"⏳ {named} 게시 대기 (게시 가능 시각 {_kst(detail.get('ready_at'))})\n"
                f"다음 예약 시도에서 게시합니다.\n{log}")
    if status == "hold":
        return (f"🚨 {named} 게시 중단 (hold)\n사유: {reason}\n"
                f"조치: 원인 확인 후 publish 재실행 (\"{RELEASE_LABEL}\" ☑)\n{log}")
    if status == "error":
        return f"🚨 {named} 게시 실패\n사유: {reason}\n{log}"
    if detail.get("dry_run"):
        return (f"ℹ️ {named} 게시 연습 실행만 완료 (실제 게시 아님)\n"
                f"실제 게시는 저장소 변수 SIDESTORY_PUBLISH_LIVE=true일 때만 진행됩니다.\n{log}")
    return f"ℹ️ {named} 게시 상태: {status}\n{log}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sidestory.app.notify")
    p.add_argument("--report", required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--run-url", required=True)
    p.add_argument("--final", action="store_true")
    a = p.parse_args(argv)
    try:
        report = json.loads(Path(a.report).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    text = build(report, stage=a.stage, side_date=a.date, run_url=a.run_url, final=a.final)
    if text:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

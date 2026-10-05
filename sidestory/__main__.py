"""CLI: python -m sidestory --stage gate|echo|narrative|image|assembly|inspect|p1|refgen|
   publish|verify [--date YYYY-MM-DD] [--force] [--no-persist] [--retry-hold]
   [--ref-revision N] [--live] [--wait-minutes N] [--report FILE]
   --stage locate prints "run_id=<assembly run id>" (scheduled publish restores that artifact)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from sidestory.app.pipeline import P0_STAGES, P1_STAGES, STAGES, run_gate_and_echo
from sidestory.app.settings import load_settings

KST = timezone(timedelta(hours=9))


def _dxy_source():
    if os.environ.get("SIDESTORY_DXY_SOURCE", "yfinance").lower() == "yfinance":
        from sidestory.adapters.market.yfinance_dxy import YFinanceDxySource

        return YFinanceDxySource()
    return None


def _p1_deps(stage: str, settings, feed, store):
    from sidestory.app.p1 import P1Deps
    from sidestory.app.settings import load_characters

    deps = P1Deps(feed=feed, store=store, characters=load_characters(),
                  nn_stage=settings.nn_stage, dxy_source=_dxy_source(),
                  run_id=os.environ.get("GITHUB_RUN_ID") or None)
    if stage in {"narrative", "p1"}:
        from sidestory.adapters.icg.llm_adapter import ClaudeNarrativeLLM
        from sidestory.adapters.notion.prompt_loader import NotionPromptSource

        deps.llm, deps.prompts = ClaudeNarrativeLLM(), NotionPromptSource()
    if stage in {"image", "p1"}:
        from sidestory.adapters.icg.image_adapter import GeminiPanelGenerator

        deps.images = GeminiPanelGenerator()
    if stage in {"image", "inspect", "p1"}:
        from sidestory.adapters.icg.vision_adapter import ClaudePanelInspector

        deps.inspector = ClaudePanelInspector()
    if stage in {"assembly", "p1"}:
        from sidestory.adapters.icg.composer_adapter import PilSlideComposer

        deps.composer = PilSlideComposer()
        from functools import partial

        from sidestory.adapters.icg.composer_adapter import KOREAN_FONT_CANDIDATES
        from sidestory.app.disclaimer_slide import render

        font = next((p for p in KOREAN_FONT_CANDIDATES if p.is_file()), None)
        if font is not None:   # missing font is refused by the composer itself
            deps.disclaimer = partial(render, font_path=font)
    return deps


def _emit(args, payload: dict) -> None:
    """Print the stage result and, with --report, also save it for the notify step (P4)."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(text)
    if getattr(args, "report", ""):
        from pathlib import Path

        Path(args.report).write_text(text, encoding="utf-8")


def _summary(store, side_day) -> dict:
    from sidestory.app.pipeline import side_episode_id

    sid = side_episode_id(side_day)
    row = store.get_episode(sid) or {}
    return {"side_episode_id": sid, "status": row.get("status"),
            "title": (row.get("script_json") or {}).get("title")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sidestory")
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--date", default="", help="side slot date (KST). default=today KST")
    parser.add_argument("--force", action="store_true", help="ignore Tue/Thu slot check")
    parser.add_argument("--no-persist", action="store_true",
                        help="read-only (gate/echo only; P1 stages always persist)")
    parser.add_argument("--retry-hold", action="store_true",
                        help="p1: release a held episode and resume from its last artifact")
    parser.add_argument("--ref-revision", type=int, default=1,
                        help="refgen: REF revision folder r<N> (new N for a new set of prompts)")
    parser.add_argument("--live", action="store_true",
                        help="publish: post to Facebook for real (also needs DRY_RUN=false)")
    parser.add_argument("--wait-minutes", type=int, default=0,
                        help="publish: post only when the assembly is at least N minutes old")
    parser.add_argument("--report", default="", help="also write the result JSON to this file")
    args = parser.parse_args(argv)

    if args.stage not in P0_STAGES + P1_STAGES + ("refgen", "inspect", "publish", "verify",
                                                  "locate"):
        print(json.dumps({"stage": args.stage, "status": "not_implemented"}))
        return 2
    if args.stage in P1_STAGES and args.no_persist:
        print(json.dumps({"stage": args.stage, "status": "usage_error",
                          "error": "--no-persist is not supported for P1 stages"}))
        return 2

    settings = load_settings()
    if args.live and (args.stage != "publish" or settings.dry_run):
        print(json.dumps({"stage": args.stage, "status": "usage_error",
                          "error": "--live is only for publish and requires DRY_RUN=false"}))
        return 2
    side_day = date.fromisoformat(args.date) if args.date else datetime.now(KST).date()

    from sidestory.adapters.supabase.client import SideSetupError, preflight, side_client
    from sidestory.adapters.supabase.main_feed_reader import SupabaseMainFeedReader
    from sidestory.adapters.supabase.side_store import SupabaseSideStore

    client = side_client(settings)
    try:
        preflight(client)
    except SideSetupError as exc:
        _emit(args, {"stage": args.stage, "status": "setup_error", "error": str(exc)})
        return 3
    feed, store = SupabaseMainFeedReader(client), SupabaseSideStore(client)

    if args.stage == "locate":
        from sidestory.app.pipeline import side_episode_id

        row = store.get_episode(side_episode_id(side_day)) or {}
        run_id = str((row.get("manifest_json") or {}).get("run_id") or "")
        print(f"run_id={run_id if run_id.isdigit() else ''}")
        return 0

    if args.stage == "refgen":
        from sidestory.adapters.icg.image_adapter import GeminiPanelGenerator
        from sidestory.adapters.notion.prompt_loader import NotionPromptSource
        from sidestory.app.refgen import run_refgen

        res = run_refgen(args.ref_revision, images=GeminiPanelGenerator(),
                         prompts=NotionPromptSource(), store=store)
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
        snippet = res.detail.get("characters_side_yaml_refs")
        if snippet:
            print("\n# characters_side.yaml refs (after master review):\n" + snippet)
        return 0 if res.status == "ok" else 1

    if args.stage in {"publish", "verify"}:
        from sidestory.app.publish import PublishDeps, run_publish, run_verify

        publisher = None
        if settings.face_page_id and settings.face_page_token:
            from sidestory.adapters.facebook.graph import FacebookPagePublisher

            publisher = FacebookPagePublisher(settings.face_page_id, settings.face_page_token)
        pdeps = PublishDeps(feed=feed, store=store, publisher=publisher, live=args.live,
                            wait_minutes=max(0, args.wait_minutes))
        res = (run_publish(side_day, pdeps, retry_hold=args.retry_hold)
               if args.stage == "publish" else run_verify(side_day, pdeps))
        _emit(args, {"stage": args.stage, "results": [res.as_dict()],
                     "summary": _summary(store, side_day)})
        return 0 if res.ok else 1

    if args.stage in P1_STAGES + ("inspect",):
        from sidestory.app.p1 import run_inspect, run_p1, run_stage

        deps = _p1_deps(args.stage, settings, feed, store)
        if args.stage == "p1":
            results = run_p1(side_day, deps, force=args.force, retry_hold=args.retry_hold)
        elif args.stage == "inspect":
            results = [run_inspect(side_day, deps)]
        else:
            results = [run_stage(args.stage, side_day, deps)]
        out: dict = {"stage": args.stage, "results": [r.as_dict() for r in results]}
        usage = getattr(deps.inspector, "usage", None)
        if usage:
            out["vision_usage"] = {"calls": len(usage),
                                   "input_tokens": sum(u["input"] for u in usage),
                                   "output_tokens": sum(u["output"] for u in usage)}
        out["summary"] = _summary(store, side_day)
        _emit(args, out)
        if not results:   # defensive: run_p1 always reports at least one result
            return 1
        last = results[-1]
        if last.status == "skipped":
            return 0
        return 0 if all(r.ok for r in results) else 1

    persist = not (args.no_persist or args.stage == "gate")
    result = run_gate_and_echo(side_day, feed, store, force=args.force, persist=persist,
                               dxy_source=_dxy_source())
    print(json.dumps({
        "side_episode_id": result.side_episode_id,
        "skipped": result.skipped,
        "persisted": result.persisted,
        "gates": [g.model_dump() for g in result.gates],
        "echo": result.echo if args.stage == "echo" else None,
    }, ensure_ascii=False, indent=2))
    if result.skipped:
        return 0
    return 0 if all(g.passed for g in result.gates) else 1


if __name__ == "__main__":
    sys.exit(main())

"""P1 stages: narrative → image → assembly (+ p1 = all remaining). No publishing here.

Every stage: load row → transition check → SG-1 main fingerprint → work → gates →
compare-and-set status → SG-7. Any failure moves the episode to ``hold`` with the reason.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from PIL import Image

from sidestory.app.imaging import MAX_KEPT_ASPECT, MIN_KEEP_RATIO, trim_panel
from sidestory.app.pipeline import run_gate_and_echo, side_episode_id
from sidestory.app.settings import CONFIG_DIR
from sidestory.core import gates, lifecycle
from sidestory.core.beats import beats_for
from sidestory.core.canon_rules import DISCLAIMER, REACTION_BEATS
from sidestory.core.image_prompt import build_panel_spec, registered_refs
from sidestory.core.models import EchoPack, GateResult
from sidestory.core.outcome import CLASS_LABEL_KO, outcome_label_ko, scenario_label_ko
from sidestory.core.panel_check import SG8, Expectation, correction, judge
from sidestory.core.schedule import previous_slot
from sidestory.core.script import SideScript, normalize, validate
from sidestory.ports.image import ImageGenerator, ImageHold, SlideComposer
from sidestory.ports.llm import LLMError, NarrativeLLM, PromptSource
from sidestory.ports.main_feed import MainFeedReader
from sidestory.ports.market_source import DxySource
from sidestory.ports.store import SideStore
from sidestory.ports.vision import PanelInspector, VisionError

MAX_NARRATIVE_ATTEMPTS = 3          # 1 + 2 regenerations (P1 §2-4)
MAX_FEEDBACK_ITEMS = 15
IMAGE_PANELS = range(1, 7)          # 7 data card / 8 disclaimer are PIL text slides
SLIDE_SIZE = (1080, 1350)
RETAKE_DIR = "v2"                   # one retake per panel per slot (icg_side 0003 scope)


@dataclass
class P1Deps:
    feed: MainFeedReader
    store: SideStore
    llm: NarrativeLLM | None = None
    prompts: PromptSource | None = None
    images: ImageGenerator | None = None
    composer: SlideComposer | None = None
    characters: dict[str, Any] = field(default_factory=dict)
    nn_stage: str = "E01"
    output_root: Path = Path("output/sidestory")
    ref_root: Path = Path(".")
    dxy_source: DxySource | None = None
    inspector: PanelInspector | None = None
    run_id: str | None = None         # GITHUB_RUN_ID of this run (artifact to restore later)
    # P2: renders the side disclaimer slide over the composer's last slide (font-safe, no emoji)
    disclaimer: Callable[[Path], Path] | None = None


@dataclass
class StageResult:
    stage: str
    side_episode_id: str
    status: str                       # resulting episode status, or "skipped"
    gates: list[GateResult] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status not in {"hold", "error"}

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "side_episode_id": self.side_episode_id,
                "status": self.status, "gates": [g.model_dump() for g in self.gates],
                "detail": self.detail}


class StageFailure(RuntimeError):
    def __init__(self, reason: str, gates_: list[GateResult] | None = None,
                 detail: dict[str, Any] | None = None, *, keep_status: bool = False):
        super().__init__(reason)
        self.gates = gates_ or []
        self.detail = detail or {}
        # keep_status: an operator input problem (e.g. wrong artifact restored), not a content
        # problem — report an error without moving the episode to hold.
        self.keep_status = keep_status


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def accumulate_feedback(attempts: list[list[str]]) -> list[str]:
    """Unique problems across all attempts, latest attempt first, capped."""
    out: list[str] = []
    for problems in reversed(attempts):
        for item in problems:
            if item not in out:
                out.append(item)
    return out[:MAX_FEEDBACK_ITEMS]


def render_user_prompt(*, side_episode_id_: str, echo: EchoPack, nn_stage: str,
                       previous_hook: str | None, feedback: list[str]) -> str:
    env = Environment(loader=FileSystemLoader(str(CONFIG_DIR / "prompts")),
                      undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)
    return env.get_template("user_side.j2").render(
        side_episode_id=side_episode_id_, echo=echo, nn_stage=nn_stage,
        previous_hook=previous_hook, reaction=REACTION_BEATS[echo.outcome_class],
        class_label=CLASS_LABEL_KO[echo.outcome_class],
        outcome_label=outcome_label_ko(echo.outcome),
        scenario_label=scenario_label_ko(echo.scenario_type),
        beats=beats_for(echo.outcome_class), disclaimer=DISCLAIMER, feedback=feedback)


# ── stages ──────────────────────────────────────────────────────────────────
def _narrative(sid: str, side_day: date, row: dict[str, Any], deps: P1Deps,
               echo: EchoPack) -> tuple[dict[str, Any], dict[str, Any]]:
    if deps.llm is None or deps.prompts is None:
        raise StageFailure("narrative adapters not configured")
    try:
        system = deps.prompts.system_prompt()
    except LLMError as exc:
        raise StageFailure(f"system prompt unavailable: {exc}") from exc
    prev = deps.store.get_episode(side_episode_id(previous_slot(side_day)))
    previous_hook = ((prev or {}).get("script_json") or {}).get("next_hook_side") or None
    feedback: list[str] = []
    attempts: list[list[str]] = []
    for _ in range(MAX_NARRATIVE_ATTEMPTS):
        user = render_user_prompt(side_episode_id_=sid, echo=echo, nn_stage=deps.nn_stage,
                                  previous_hook=previous_hook, feedback=feedback)
        try:
            raw = deps.llm.generate_script(system, user)
        except LLMError as exc:
            problems = [f"output error: {exc}. Return exactly one JSON object."]
        else:
            script = normalize(raw, side_episode_id=sid, echo=echo)
            _, problems = validate(script, echo, deps.nn_stage)
            if not problems:
                return {"script_json": script}, {"attempts": len(attempts) + 1}
        attempts.append(problems)
        # The model rewrites the whole script each time, so it must see every earlier
        # rejection (pilot 1 retry: fixing one problem reintroduced another).
        feedback = accumulate_feedback(attempts)
    raise StageFailure("narrative rejected after "
                       f"{MAX_NARRATIVE_ATTEMPTS} attempts: {'; '.join(attempts[-1])}",
                       detail={"attempt_problems": attempts})


def _image(sid: str, side_day: date, row: dict[str, Any], deps: P1Deps,
           echo: EchoPack) -> tuple[dict[str, Any], dict[str, Any]]:
    if deps.images is None:
        raise StageFailure("image adapter not configured")
    script = SideScript.model_validate(row["script_json"])
    story = [p for p in script.panels if p.idx in IMAGE_PANELS]
    poses = {p.zero_block_pose for p in story}
    checks = [(pose, registered, sha256_file(deps.ref_root / path) if path else None)
              for pose, path, registered in registered_refs(deps.characters, poses)]
    sg2 = gates.sg2_refs(checks)
    if not sg2.passed:
        raise StageFailure(sg2.reason, [sg2])
    if deps.inspector is None:
        raise StageFailure("panel inspector (SG-8) not configured", [sg2])
    day_dir = deps.output_root / side_day.isoformat()
    panels: list[dict[str, Any]] = []
    sg8: list[GateResult] = []
    total = 0.0

    def fail(reason: str) -> StageFailure:
        return StageFailure(reason, [sg2, *sg8],
                            {"panels_done": panels, "cost_usd": round(total, 4)})

    for panel in story:
        spec = build_panel_spec(panel, deps.characters)
        refs = [deps.ref_root / spec.ref_path] if spec.ref_path else []
        exp = Expectation.of(panel)
        checks: list[dict[str, Any]] = []
        prompt, out_dir = spec.prompt, day_dir / "panels"
        panel_cost = 0.0
        for take in (0, 1):
            try:
                path, cost = deps.images.generate(panel.idx, prompt, refs, out_dir)
            except ImageHold as exc:
                raise fail(f"P{panel.idx} image hold: {exc}") from exc
            total += cost
            panel_cost += cost
            digest = sha256_file(path)
            if digest is None:
                raise fail(f"P{panel.idx} image file missing after generation")
            try:
                report = deps.inspector.inspect(path)
            except VisionError as exc:
                raise fail(f"P{panel.idx} {SG8} inspection unavailable (not passed): "
                           f"{exc}") from exc
            verdict = judge(report, exp)
            checks.append({"take": take + 1, "sha256": digest, **verdict.as_dict(),
                           "report": report.model_dump()})
            if verdict.passed:
                break
            if take == 0:   # one retake: same prompt + fixed correction for the categories
                prompt = f"{spec.prompt}\n{correction(verdict)}"
                out_dir = day_dir / RETAKE_DIR / "panels"
                continue
            sg8.append(GateResult(gate=SG8, passed=False,
                                  reason=f"P{panel.idx}: {'; '.join(verdict.critical)}"))
            raise StageFailure(
                f"P{panel.idx} {SG8} failed after retake: {'; '.join(verdict.critical)}",
                [sg2, *sg8], {"panels_done": panels, "cost_usd": round(total, 4),
                              "sg8_checks": checks})
        minor = checks[-1]["minor"]
        sg8.append(GateResult(gate=SG8, passed=True, reason=(
            f"P{panel.idx} take {len(checks)}" + (f"; minor: {'; '.join(minor)}" if minor else ""))))
        panels.append({"idx": panel.idx, "path": _rel(path), "sha256": digest,
                       "pose": spec.pose, "ref": spec.ref_path,
                       "cost_usd": round(panel_cost, 4), "sg8": checks})
    return ({"panels_json": {"panels": panels, "cost_usd": round(total, 4),
                             "run_id": deps.run_id}},
            {"gates": [sg2, *sg8], "cost_usd": round(total, 4)})


def _assembly(sid: str, side_day: date, row: dict[str, Any], deps: P1Deps,
              echo: EchoPack) -> tuple[dict[str, Any], dict[str, Any]]:
    if deps.composer is None:
        raise StageFailure("composer not configured")
    script = SideScript.model_validate(row["script_json"])
    stored = {p["idx"]: p for p in (row.get("panels_json") or {}).get("panels", [])}
    images: list[Path | None] = []
    for panel in script.panels:
        if panel.idx not in IMAGE_PANELS:
            images.append(None)
            continue
        entry = stored.get(panel.idx)
        path = Path(entry["path"]) if entry else None
        if path is None or sha256_file(path) != entry.get("sha256"):
            run = (row.get("panels_json") or {}).get("run_id")
            where = (f"enter {run} in \"Resume only: previous run id or run URL\"" if run else
                     "enter the run id of the image-stage run in \"Resume only: previous run id "
                     "or run URL\"")
            raise StageFailure(f"P{panel.idx} panel artifact missing or changed — {where}. "
                               "Status unchanged (no hold).", keep_status=True)
        images.append(path)
    # B1: cut letterbox / frame bands from a copy (the paid original keeps its ledger hash).
    trim_dir = deps.output_root / side_day.isoformat() / "panels_trimmed"
    trims: dict[str, list[int]] = {}
    for i, path in enumerate(images):
        if path is None:
            continue
        used, res = trim_panel(path, trim_dir)
        if res.kept_ratio < MIN_KEEP_RATIO:
            raise StageFailure(f"{path.name} band trim would keep only {res.kept_ratio:.0%} "
                               f"(< {MIN_KEEP_RATIO:.0%}, misdetection) — review the panel")
        if res.aspect > MAX_KEPT_ASPECT:
            raise StageFailure(f"{path.name} band trim would leave a {res.aspect:.2f}:1 strip "
                               f"(> {MAX_KEPT_ASPECT:.0f}:1) — review the panel")
        if res.trimmed:
            trims[path.stem] = list(res.box)
        images[i] = used
    out_dir = deps.output_root / side_day.isoformat() / "slides"
    try:
        slides = deps.composer.compose(
            [p.model_dump() for p in script.panels], images, out_dir)
    except ValueError as exc:
        raise StageFailure(f"composer rejected sources: {exc}") from exc
    if len(slides) != len(script.panels):
        raise StageFailure(f"expected {len(script.panels)} slides, got {len(slides)}")
    if deps.disclaimer is not None and script.panels[-1].panel_type == "DISCLAIMER":
        try:
            deps.disclaimer(Path(slides[-1]))
        except (OSError, ValueError) as exc:
            raise StageFailure(f"side disclaimer slide failed: {exc}") from exc
    expected: dict[str, str] = {}
    for slide in slides:
        digest = sha256_file(Path(slide))
        if digest is None:
            raise StageFailure(f"slide missing: {slide}")
        with Image.open(slide) as img:
            if img.size != SLIDE_SIZE:
                raise StageFailure(f"slide {Path(slide).name} size {img.size} != {SLIDE_SIZE}")
        expected[Path(slide).name] = digest
    actual = {Path(s).name: sha256_file(Path(s)) or "" for s in slides}
    sg6 = gates.sg6_manifest(expected, actual)
    if not sg6.passed:
        raise StageFailure(sg6.reason, [sg6])
    manifest = {"slides": expected, "script_sha256": gates.fingerprint(row["script_json"]),
                "panels": {str(k): v["sha256"] for k, v in stored.items()},
                "trimmed": trims, "run_id": deps.run_id,
                # P4: scheduled publish waits until this is >= 60 minutes old.
                "assembled_at": utcnow().isoformat()}
    slides_json = [{"name": Path(s).name, "path": _rel(Path(s)), "sha256": expected[Path(s).name]}
                   for s in slides]
    return {"slides_json": slides_json, "manifest_json": manifest}, {"gates": [sg6]}


_STAGES = {"narrative": _narrative, "image": _image, "assembly": _assembly}


def run_stage(stage: str, side_day: date, deps: P1Deps) -> StageResult:
    sid = side_episode_id(side_day)
    row = deps.store.get_episode(sid)
    if not row:
        return StageResult(stage, sid, "error", detail={"reason": "slot not echoed yet"})
    current = row.get("status", "")
    try:
        lifecycle.check_transition(stage, current)
    except lifecycle.TransitionError as exc:
        return StageResult(stage, sid, "error", detail={"reason": str(exc)})
    echo = EchoPack.model_validate(row["echo_pack_json"])
    main_day = echo.main_date
    before = deps.feed.main_fingerprint(main_day)
    sg1 = gates.sg1_record(before)
    result = StageResult(stage, sid, current, gates=[sg1])
    try:
        if not sg1.passed:
            raise StageFailure(sg1.reason)
        fields, detail = _STAGES[stage](sid, side_day, row, deps, echo)
        result.gates += detail.pop("gates", [])
        result.detail.update(detail)
        sg7 = gates.sg7_unchanged(before, deps.feed.main_fingerprint(main_day))
        result.gates.append(sg7)
        if not sg7.passed:
            raise StageFailure(sg7.reason)
        target = lifecycle.target_status(stage)
        if not deps.store.update_episode(sid, {**fields, "status": target,
                                               "error_message": None}, expect_status=current):
            result.status = "error"
            result.detail["reason"] = "status changed concurrently (compare-and-set failed)"
            deps.store.log(stage, "error", {"sid": sid, **result.detail})
            return result
        result.status = target
        deps.store.log(stage, "ok", {"sid": sid, **_loggable(result.detail)})
    except StageFailure as exc:
        result.gates += exc.gates
        result.detail.update({"reason": str(exc), **exc.detail})
        if exc.keep_status:
            result.status = "error"
            deps.store.log(stage, "error", {"sid": sid, **_loggable(result.detail)})
            return result
        deps.store.update_episode(sid, {"status": "hold", "error_message": f"{stage}: {exc}"[:2000]},
                                  expect_status=current)
        result.status = "hold"
        deps.store.log(stage, "hold", {"sid": sid, **_loggable(result.detail)})
    return result


def _loggable(detail: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in detail.items() if k != "gates"}


def run_p1(side_day: date, deps: P1Deps, *, force: bool = False,
           retry_hold: bool = False) -> list[StageResult]:
    """echo (if the slot is new) → remaining P1 stages; stops at the first non-ok stage."""
    sid = side_episode_id(side_day)
    results: list[StageResult] = []
    row = deps.store.get_episode(sid)
    if not row:
        echo_run = run_gate_and_echo(side_day, deps.feed, deps.store, force=force,
                                     persist=True, dxy_source=deps.dxy_source)
        status = "draft" if echo_run.persisted and all(g.passed for g in echo_run.gates) else (
            "skipped" if echo_run.skipped else "error")
        results.append(StageResult("echo", sid, status, gates=echo_run.gates))
        if status != "draft":
            return results
        row = deps.store.get_episode(sid) or {}
    status = row.get("status", "")
    if status == "hold":
        if not retry_hold:
            results.append(StageResult("p1", sid, "hold", detail={
                "reason": f"episode on hold: {row.get('error_message')} (use --retry-hold)"}))
            return results
        status = lifecycle.resume_status(row)
        if not deps.store.update_episode(sid, {"status": status, "error_message": None},
                                         expect_status="hold"):
            results.append(StageResult("p1", sid, "error",
                                       detail={"reason": "hold release lost a race"}))
            return results
        deps.store.log("p1", "resume", {"sid": sid, "status": status})
    try:
        stages = lifecycle.remaining_stages(status)
    except lifecycle.TransitionError as exc:
        results.append(StageResult("p1", sid, "error", detail={"reason": str(exc)}))
        return results
    if not stages:
        results.append(StageResult("p1", sid, status, detail={
            "reason": f"nothing to run (already {status})"}))
        return results
    for stage in stages:
        res = run_stage(stage, side_day, deps)
        results.append(res)
        if not res.ok:
            break
    return results


def run_inspect(side_day: date, deps: P1Deps) -> StageResult:
    """SG-8 on already generated panels. Read-only: no image call, no status change."""
    sid = side_episode_id(side_day)
    row = deps.store.get_episode(sid)
    status = (row or {}).get("status", "")
    result = StageResult("inspect", sid, status)
    if not row or not row.get("script_json") or not row.get("panels_json"):
        result.status, result.detail["reason"] = "error", "no generated panels for this slot"
        return result
    if deps.inspector is None:
        result.status, result.detail["reason"] = "error", "panel inspector not configured"
        return result
    script = SideScript.model_validate(row["script_json"])
    stored = {p["idx"]: p for p in row["panels_json"].get("panels", [])}
    found: dict[str, Any] = {}
    for panel in script.panels:
        entry = stored.get(panel.idx)
        if panel.idx not in IMAGE_PANELS or entry is None:
            continue
        path = Path(entry["path"])
        if sha256_file(path) != entry.get("sha256"):
            result.status = "error"
            run = row["panels_json"].get("run_id")
            result.detail["reason"] = (
                f"P{panel.idx} panel artifact missing or changed — restore it with "
                "\"Resume only: previous run id or run URL\"" + (f" = {run}" if run else ""))
            return result
        try:
            report = deps.inspector.inspect(path)
        except VisionError as exc:
            result.status, result.detail["reason"] = "error", f"P{panel.idx}: {exc}"
            return result
        verdict = judge(report, Expectation.of(panel))
        result.gates.append(GateResult(gate=SG8, passed=verdict.passed, reason=(
            f"P{panel.idx}: " + "; ".join([*verdict.critical, *verdict.minor] or ["clean"]))))
        found[f"P{panel.idx}"] = {**verdict.as_dict(), "report": report.model_dump()}
    result.detail["panels"] = found
    result.detail["failed"] = [k for k, v in found.items() if v["critical"]]
    deps.store.log("inspect", "ok", {"sid": sid, "failed": result.detail["failed"],
                                     "panels": found})
    return result

"""SG-8 picture check (v8.9): judge rules, retake flow, holds, inspect stage, adapter."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from sidestory.adapters.icg.vision_adapter import ClaudePanelInspector
from sidestory.app.p1 import P1Deps, run_inspect, run_p1, run_stage
from sidestory.core.panel_check import (
    CORRECTIONS,
    Expectation,
    VisionReport,
    correction,
    judge,
    vision_prompt,
)
from sidestory.ports.vision import VisionError
from sidestory.tests.fixtures import FakeFeed, FakeStore, main_row
from sidestory.tests.p1_fixtures import (
    CLEAN,
    FakeComposer,
    FakeImages,
    FakeInspector,
    FakeLLM,
    FakePrompts,
    make_refs,
    raw_script,
)

TUE = date(2026, 10, 6)
SID = "SIDE-2026-10-06-01"
MAJOR = {"kind": "other", "prominence": "major"}
MINOR = {"kind": "other", "prominence": "minor"}
ZB = {"kind": "zero_block", "prominence": "major"}


def rep(figures=(), text="none", watermark=False) -> VisionReport:
    return VisionReport(figures=list(figures), text=text, watermark=watermark)


# ── judge ────────────────────────────────────────────────────────────────────
def test_clean_and_expected_figures_pass() -> None:
    assert judge(rep(), Expectation(False, 0)).passed
    assert judge(rep([ZB]), Expectation(True, 0)).passed
    v = judge(rep([ZB, MAJOR]), Expectation(True, 1))     # requested node silhouette
    assert v.passed and v.minor == ()


def test_pilot1_s2_case_is_critical() -> None:
    # S2: no pose, no silhouettes, hooded figures with glowing eyes on both cliffs.
    v = judge(rep([MAJOR, MAJOR, MINOR, MINOR]), Expectation(False, 0))
    assert not v.passed and v.critical[0].startswith("extra_figure: 2")
    assert v.minor[0].startswith("extra_figure: 2 faint")


def test_minor_only_issues_pass_with_warning() -> None:
    v = judge(rep([MINOR], text="minor"), Expectation(False, 0))
    assert v.passed and len(v.minor) == 2


def test_allowance_absorbs_minor_first_then_major() -> None:
    v = judge(rep([MAJOR, MINOR]), Expectation(False, 1))
    assert not v.passed and v.minor == ()
    assert judge(rep([MAJOR, MAJOR]), Expectation(False, 2)).passed


@pytest.mark.parametrize("report,exp,key", [
    (rep(text="legible"), Expectation(False, 0), "text"),
    (rep(watermark=True), Expectation(False, 0), "watermark"),
    (rep(), Expectation(True, 0), "zero_block_missing"),
    (rep([ZB, ZB]), Expectation(True, 0), "extra_figure"),
    (rep([ZB]), Expectation(False, 0), "extra_figure"),
])
def test_critical_categories(report, exp, key) -> None:
    v = judge(report, exp)
    assert not v.passed and v.critical[0].split(":")[0] == key


def test_correction_depends_only_on_categories() -> None:
    a = judge(rep([MAJOR], text="legible"), Expectation(False, 0))
    b = judge(rep([MAJOR, MAJOR, MAJOR], text="legible"), Expectation(False, 0))
    assert correction(a) == correction(b)
    assert CORRECTIONS["extra_figure"] in correction(a) and CORRECTIONS["text"] in correction(a)


def test_vision_prompt_is_not_told_the_script() -> None:
    assert vision_prompt() == vision_prompt() and "requests" not in vision_prompt()


# ── pipeline ─────────────────────────────────────────────────────────────────
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return P1Deps(
        feed=FakeFeed([main_row("2026-10-06")], fingerprints=("a" * 64,)),
        store=FakeStore(), llm=FakeLLM([raw_script()]), prompts=FakePrompts(),
        images=FakeImages(), composer=FakeComposer(), characters=make_refs(tmp_path),
        output_root=tmp_path / "output/sidestory", ref_root=tmp_path,
        inspector=FakeInspector(), run_id="37319333438")


def test_clean_run_records_sg8_and_run_id(env) -> None:
    res = run_p1(TUE, env)
    assert res[-1].status == "assembled"
    pj = env.store.get_episode(SID)["panels_json"]
    assert pj["run_id"] == "37319333438"
    assert all(p["sg8"][0]["take"] == 1 and p["sg8"][0]["critical"] == [] for p in pj["panels"])
    assert len(env.images.calls) == 6 and len(env.inspector.calls) == 6


def test_critical_panel_is_retaken_once_in_v2_scope(env) -> None:
    env.inspector = FakeInspector({(2, 1): dict(CLEAN, figures=[MAJOR])})
    res = run_p1(TUE, env)
    assert res[-1].status == "assembled"
    p2_calls = [c for c in env.images.calls if c[0] == 2]
    assert len(p2_calls) == 2
    assert p2_calls[1][3].as_posix().endswith("output/sidestory/2026-10-06/v2/panels")
    assert p2_calls[1][1].endswith(correction(judge(rep([MAJOR]), Expectation(False, 0))))
    assert p2_calls[1][1].startswith(p2_calls[0][1])
    p2 = env.store.get_episode(SID)["panels_json"]["panels"][1]
    assert "/v2/panels/P2.png" in p2["path"] and [c["take"] for c in p2["sg8"]] == [1, 2]
    assert p2["cost_usd"] == pytest.approx(0.078)   # both takes are paid
    assert len(env.images.calls) == 7


def test_retake_still_critical_holds_with_checks(env) -> None:
    bad = dict(CLEAN, figures=[MAJOR])
    env.inspector = FakeInspector({(3, 1): bad, (3, 2): bad})
    res = run_p1(TUE, env)
    last = res[-1]
    assert last.stage == "image" and last.status == "hold"
    assert "SG-8 failed after retake" in last.detail["reason"]
    assert [c["take"] for c in last.detail["sg8_checks"]] == [1, 2]
    assert [p["idx"] for p in last.detail["panels_done"]] == [1, 2]
    assert last.gates[-1].gate == "SG-8" and not last.gates[-1].passed
    assert env.store.get_episode(SID)["status"] == "hold"
    assert len([c for c in env.images.calls if c[0] == 3]) == 2   # never a third take


def test_minor_issue_publishes_without_retake(env) -> None:
    env.inspector = FakeInspector({(5, 1): dict(CLEAN, figures=[MINOR, MINOR], text="minor")})
    res = run_p1(TUE, env)
    assert res[-1].status == "assembled" and len(env.images.calls) == 6
    gate = [g for g in res[2].gates if g.gate == "SG-8" and g.reason.startswith("P5")][0]
    assert gate.passed and "minor" in gate.reason


def test_inspection_failure_holds_never_passes(env) -> None:
    env.inspector = FakeInspector({(4, 1): VisionError("529 overloaded")})
    res = run_p1(TUE, env)
    assert res[-1].status == "hold" and "inspection unavailable" in res[-1].detail["reason"]
    assert len(env.images.calls) == 4   # no retake on an unknown verdict


def test_missing_inspector_holds_before_paid_call(env) -> None:
    env.inspector = None
    res = run_p1(TUE, env)
    assert res[-1].status == "hold" and "SG-8" in res[-1].detail["reason"]
    assert env.images.calls == []


def test_inspect_stage_is_read_only(env) -> None:
    run_p1(TUE, env)
    row_before = dict(env.store.get_episode(SID))
    calls = len(env.images.calls)
    env.inspector = FakeInspector({(2, 1): dict(CLEAN, figures=[MAJOR, MINOR])})
    res = run_inspect(TUE, env)
    assert res.ok and res.status == "assembled" and res.detail["failed"] == ["P2"]
    assert len(env.images.calls) == calls
    assert env.store.get_episode(SID) == row_before
    assert env.store.logs[-1][:2] == ("inspect", "ok")


def test_inspect_reports_run_to_restore(env) -> None:
    run_p1(TUE, env)
    Path(env.store.get_episode(SID)["panels_json"]["panels"][4]["path"]).unlink()
    res = run_inspect(TUE, env)
    assert res.status == "error" and "37319333438" in res.detail["reason"]


def test_assembly_mismatch_names_the_run_and_keeps_status(env) -> None:
    run_p1(TUE, env)
    Path(env.store.get_episode(SID)["panels_json"]["panels"][4]["path"]).unlink()
    res = run_stage("assembly", TUE, env)
    assert res.status == "error" and "37319333438" in res.detail["reason"]
    assert env.store.get_episode(SID)["status"] == "assembled"


def test_retry_hold_on_assembled_reports_instead_of_crashing(env) -> None:
    run_p1(TUE, env)
    env.store.get_episode(SID)["status"] = "hold"
    res = run_p1(TUE, env, retry_hold=True)
    assert [(r.stage, r.status) for r in res] == [("p1", "assembled")] and res[0].ok


def test_cli_prints_and_exits_zero_on_nothing_to_run(monkeypatch, capsys) -> None:
    from sidestory import __main__ as cli
    from sidestory.app import p1 as p1mod

    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(nn_stage="E01"))
    import sidestory.adapters.supabase.client as sc
    monkeypatch.setattr(sc, "side_client", lambda s: object())
    monkeypatch.setattr(sc, "preflight", lambda c: None)
    monkeypatch.setattr(cli, "_p1_deps", lambda *a: SimpleNamespace(inspector=None))
    monkeypatch.setattr(p1mod, "run_p1", lambda *a, **k: [p1mod.StageResult(
        "p1", SID, "assembled", detail={"reason": "nothing to run (already assembled)"})])
    import sidestory.adapters.supabase.main_feed_reader as mf
    import sidestory.adapters.supabase.side_store as ss
    monkeypatch.setattr(mf, "SupabaseMainFeedReader", lambda c: None)
    monkeypatch.setattr(ss, "SupabaseSideStore", lambda c: FakeStore())
    assert cli.main(["--stage", "p1", "--date", "2026-10-03", "--retry-hold"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["status"] == "assembled"


# ── adapter ──────────────────────────────────────────────────────────────────
class _Msgs:
    def __init__(self, text=None, exc=None):
        self.text, self.exc, self.kwargs = text, exc, None

    def create(self, *, model, max_tokens, system, messages, **extra):
        self.kwargs = {"model": model, "system": system, "messages": messages, **extra}
        if self.exc:
            raise self.exc
        return SimpleNamespace(content=[SimpleNamespace(text=self.text)],
                               usage=SimpleNamespace(input_tokens=1600, output_tokens=70))


def _png(tmp_path, size=(2048, 1024)) -> Path:
    p = tmp_path / "P2.png"
    Image.new("RGB", size, (40, 20, 90)).save(p)
    return p


def test_adapter_sends_image_and_parses(tmp_path) -> None:
    msgs = _Msgs('```json\n{"figures": [{"kind": "other", "prominence": "major"}],'
                 ' "text": "none", "watermark": false, "notes": "cliff"}\n```')
    insp = ClaudePanelInspector(client=SimpleNamespace(messages=msgs), model="m")
    out = insp.inspect(_png(tmp_path))
    assert out.figures[0].prominence == "major" and insp.usage == [{"input": 1600, "output": 70}]
    content = msgs.kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert msgs.kwargs["system"] == vision_prompt() and msgs.kwargs.get("temperature") == 0
    import base64
    import io
    with Image.open(io.BytesIO(base64.b64decode(content[0]["source"]["data"]))) as img:
        assert max(img.size) == 1024


@pytest.mark.parametrize("text,exc", [
    ("no json", None), ('{"figures": [], "text": "maybe", "watermark": false}', None),
    ('{"figures": []}', None), (None, RuntimeError("529"))])
def test_adapter_errors_are_typed(tmp_path, text, exc) -> None:
    insp = ClaudePanelInspector(client=SimpleNamespace(messages=_Msgs(text, exc)), model="m")
    with pytest.raises(VisionError):
        insp.inspect(_png(tmp_path))


def test_adapter_unreadable_image(tmp_path) -> None:
    bad = tmp_path / "P1.png"
    bad.write_bytes(b"not a png")
    insp = ClaudePanelInspector(client=SimpleNamespace(messages=_Msgs("{}")), model="m")
    with pytest.raises(VisionError):
        insp.inspect(bad)

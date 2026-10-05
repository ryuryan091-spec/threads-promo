"""P4: scheduled p1 → publish ≥60 min later, notices for success and failure (offline)."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from sidestory.app import notify
from sidestory.app import p1 as p1mod
from sidestory.app.p1 import P1Deps, run_p1
from sidestory.app.publish import PublishDeps, run_publish
from sidestory.tests.conftest import REPO_ROOT
from sidestory.tests.fixtures import FakeFeed, FakeStore, main_row
from sidestory.tests.p1_fixtures import (
    FakeComposer,
    FakeImages,
    FakeInspector,
    FakeLLM,
    FakePrompts,
    make_refs,
    raw_script,
)
from sidestory.tests.test_p2_publish import FakePublisher

TUE = date(2026, 10, 6)
SID = "SIDE-2026-10-06-01"
T0 = datetime(2026, 10, 6, 1, 25, tzinfo=timezone.utc)      # 10:25 KST assembly
WF = REPO_ROOT / ".github/workflows/sidestory_run.yml"
URL = "https://github.com/o/r/actions/runs/1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(p1mod, "utcnow", lambda: T0)
    feed = FakeFeed([main_row("2026-10-06")], fingerprints=("a" * 64,))
    store = FakeStore()
    p1 = P1Deps(feed=feed, store=store, llm=FakeLLM([raw_script()]), prompts=FakePrompts(),
                images=FakeImages(), composer=FakeComposer(), characters=make_refs(tmp_path),
                output_root=tmp_path / "output/sidestory", ref_root=tmp_path,
                inspector=FakeInspector(), run_id="777")
    assert run_p1(TUE, p1)[-1].status == "assembled"
    return SimpleNamespace(feed=feed, store=store)


def pdeps(env, pub, minutes_after, wait=60):
    return PublishDeps(feed=env.feed, store=env.store, publisher=pub, live=True,
                       wait_minutes=wait, now=lambda: T0 + timedelta(minutes=minutes_after))


# ── 60-minute wait ───────────────────────────────────────────────────────────
def test_assembled_at_recorded(env) -> None:
    m = env.store.get_episode(SID)["manifest_json"]
    assert m["assembled_at"] == T0.isoformat() and m["run_id"] == "777"


def test_publish_waits_until_60_minutes(env) -> None:
    pub = FakePublisher()
    res = run_publish(TUE, pdeps(env, pub, 52))
    assert res.ok and res.status == "assembled" and res.detail["waiting"] is True
    assert res.detail["ready_at"] == (T0 + timedelta(minutes=60)).isoformat()
    assert pub.uploads == [] and env.store.get_episode(SID)["status"] == "assembled"
    assert env.store.logs[-1][:2] == ("publish", "waiting")
    res = run_publish(TUE, pdeps(env, pub, 60))          # exactly 60 minutes → posts
    assert res.status == "published" and len(pub.posts) == 1
    res = run_publish(TUE, pdeps(env, pub, 120))         # later attempt → no second post
    assert res.detail["reason"] == "already published" and len(pub.posts) == 1


def test_missing_assembly_time_never_posts_unattended(env) -> None:
    env.store.get_episode(SID)["manifest_json"].pop("assembled_at")
    pub = FakePublisher()
    res = run_publish(TUE, pdeps(env, pub, 500))
    assert res.detail["waiting"] is True and "manually" in res.detail["reason"]
    assert pub.uploads == []
    assert run_publish(TUE, pdeps(env, pub, 0, wait=0)).status == "published"   # manual run


def test_failed_production_is_never_published(env) -> None:
    env.store.get_episode(SID).update(status="hold", error_message="image: SG-8 failed")
    pub = FakePublisher()
    res = run_publish(TUE, pdeps(env, pub, 90))
    assert res.status == "skipped" and res.detail["still_on_hold"] is True
    assert pub.uploads == [] and env.store.get_episode(SID)["status"] == "hold"


def test_no_episode_for_slot_is_skipped(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    res = run_publish(TUE, PublishDeps(feed=FakeFeed([]), store=FakeStore(), live=True,
                                       publisher=FakePublisher(), wait_minutes=60))
    assert res.ok and res.status == "skipped"


# ── notices ──────────────────────────────────────────────────────────────────
def R(stage, status, detail=None, gates=None, title="고요한 날의 균열 탐색"):
    return {"stage": stage, "results": [{"stage": stage, "status": status,
                                         "detail": detail or {}, "gates": gates or []}],
            "summary": {"title": title}}


def B(report, stage, final=False):
    return notify.build(report, stage=stage, side_date="2026-10-08", run_url=URL, final=final)


@pytest.mark.parametrize("report,stage,final,starts,needles", [
    (R("assembly", "assembled"), "p1", False, "✅", ["제작 완료", "고요한 날의 균열 탐색", "60분"]),
    ({"stage": "p1", "results": [{"stage": "echo", "status": "skipped", "detail": {},
      "gates": [{"gate": "SG-0", "skip": True, "reason": "no anchor"}]}]}, "p1", False,
     "ℹ️", ["제작 건너뜀", "no anchor"]),
    (R("image", "hold", {"reason": "P3 SG-8 failed after retake"}), "p1", False, "🚨",
     ["제작 중단 [image]", "SG-8", notify.RELEASE_LABEL]),
    (R("publish", "published", {"post_id": "123_9"}), "publish", False, "✅",
     ["게시 완료", "https://www.facebook.com/123_9"]),
    (R("publish", "published", {"post_id": "123_9", "reconciled": True}), "publish", False,
     "✅", ["대조"]),
    (R("publish", "assembled", {"waiting": True, "ready_at": "2026-10-08T02:25:00+00:00",
                                 "reason": "waits"}), "publish", False, "⏳", ["11:25 KST"]),
    (R("publish", "assembled", {"waiting": True, "reason": "waits"}), "publish", True, "🚨",
     ["마지막", "수동"]),
    (R("publish", "hold", {"reason": "post outcome unknown"}), "publish", False, "🚨",
     ["hold", notify.RELEASE_LABEL]),
    (R("publish", "error", {"reason": "slide manifest mismatch"}), "publish", False, "🚨",
     ["게시 실패", "manifest"]),
    (R("publish", "skipped", {"reason": "no side episode for this slot"}), "publish", True,
     "ℹ️", ["건너뜀"]),
    (R("publish", "hold", {"reason": "episode on hold", "still_on_hold": True}), "publish",
     True, "🚨", ["hold"]),
    (R("publish", "assembled", {"dry_run": True}), "publish", False, "ℹ️",
     ["SIDESTORY_PUBLISH_LIVE"]),
    (None, "publish", False, "🚨", ["실행 실패", "결과 없음"]),
    (None, "unknown", False, "🚨", ["실행 실패"]),
    ({"stage": "p1", "status": "setup_error", "error": "icg_side not exposed"}, "p1", False,
     "🚨", ["icg_side not exposed"]),
])
def test_notice_text(report, stage, final, starts, needles) -> None:
    text = B(report, stage, final)
    assert text.startswith(starts), text
    for n in needles:
        assert n in text, (n, text)
    assert text.endswith(URL)


def test_quiet_cases() -> None:
    assert B(R("publish", "published", {"reason": "already published"}), "publish") is None
    # repeat attempts stay quiet until the last one (the cause was already notified)
    assert B(R("publish", "skipped", {"reason": "production on hold"}), "publish") is None
    assert B(R("publish", "hold", {"reason": "x", "still_on_hold": True}), "publish") is None
    assert B(R("inspect", "assembled"), "inspect") is None
    assert B({"stage": "verify", "status": "x"}, "verify") is None


def test_long_reason_is_clipped() -> None:
    text = B(R("publish", "error", {"reason": "x" * 5000}), "publish")
    assert len(text) < 1000 and "…" in text


def test_notify_cli(tmp_path, capsys) -> None:
    f = tmp_path / "r.json"
    f.write_text(json.dumps(R("publish", "published", {"post_id": "1_2"})), encoding="utf-8")
    assert notify.main(["--report", str(f), "--stage", "publish", "--date", "2026-10-08",
                        "--run-url", URL]) == 0
    assert "게시 완료" in capsys.readouterr().out
    assert notify.main(["--report", str(tmp_path / "missing.json"), "--stage", "p1",
                        "--date", "2026-10-08", "--run-url", URL, "--final"]) == 0
    assert "실행 실패" in capsys.readouterr().out


# ── CLI report / locate ──────────────────────────────────────────────────────
def _cli_env(monkeypatch, store):
    import sidestory.adapters.supabase.client as sc
    import sidestory.adapters.supabase.main_feed_reader as mf
    import sidestory.adapters.supabase.side_store as ss
    from sidestory import __main__ as cli

    monkeypatch.setenv("SUPABASE_SCHEMA", "icg_side")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(sc, "side_client", lambda s: object())
    monkeypatch.setattr(sc, "preflight", lambda c: None)
    monkeypatch.setattr(mf, "SupabaseMainFeedReader", lambda c: FakeFeed([]))
    monkeypatch.setattr(ss, "SupabaseSideStore", lambda c: store)
    return cli


def test_cli_locate_and_report(env, monkeypatch, capsys, tmp_path) -> None:
    cli = _cli_env(monkeypatch, env.store)
    assert cli.main(["--stage", "locate", "--date", "2026-10-06"]) == 0
    assert capsys.readouterr().out.strip() == "run_id=777"
    assert cli.main(["--stage", "locate", "--date", "2026-10-07"]) == 0
    assert capsys.readouterr().out.strip() == "run_id="
    rep = tmp_path / "rep.json"
    assert cli.main(["--stage", "publish", "--date", "2026-10-06", "--report", str(rep),
                     "--wait-minutes", "60"]) == 0
    saved = json.loads(rep.read_text(encoding="utf-8"))
    assert saved["results"][0]["detail"]["dry_run"] is True
    assert saved["summary"]["title"] == raw_script()["title"]


def test_cli_locate_ignores_non_numeric_run(env, monkeypatch, capsys) -> None:
    env.store.get_episode(SID)["manifest_json"]["run_id"] = "1; rm -rf /"
    cli = _cli_env(monkeypatch, env.store)
    cli.main(["--stage", "locate", "--date", "2026-10-06"])
    assert capsys.readouterr().out.strip() == "run_id="


# ── workflow wiring (executes the real bash of the Resolve mode step) ────────
def _wf():
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _step(name):
    return next(s for s in _wf()["jobs"]["sidestory"]["steps"] if s["name"] == name)


def test_schedules() -> None:
    crons = [c["cron"] for c in _wf()[True]["schedule"]]
    assert crons == ["17 1 * * 2,4", "17 2 * * 2,4", "17 3 * * 2,4", "17 4 * * 2,4"]
    assert _wf()["jobs"]["sidestory"]["if"] == (
        "github.event_name == 'workflow_dispatch' || vars.SIDESTORY_SCHEDULE_ENABLED == 'true'")


def _mode(tmp_path, **env):
    out = tmp_path / "out.txt"
    out.write_text("")
    base = {"EVENT": "", "SCHED": "", "IN_STAGE": "", "IN_LIVE": "", "LIVE_VAR": "",
            "GITHUB_OUTPUT": str(out), "PATH": os.environ["PATH"]}
    r = subprocess.run(["bash", "-c", _step("Resolve mode")["run"]],
                       env={**base, **env}, capture_output=True, text=True)
    return r.returncode, dict(line.split("=", 1) for line in out.read_text().split())


@pytest.mark.parametrize("env,expect", [
    ({"EVENT": "schedule", "SCHED": "17 1 * * 2,4", "LIVE_VAR": "true"},
     {"stage": "p1", "live": "false", "wait": "0", "final": "false"}),
    ({"EVENT": "schedule", "SCHED": "17 2 * * 2,4", "LIVE_VAR": "true"},
     {"stage": "publish", "live": "true", "wait": "60", "final": "false"}),
    ({"EVENT": "schedule", "SCHED": "17 3 * * 2,4", "LIVE_VAR": "true"},
     {"stage": "publish", "live": "true", "wait": "60", "final": "false"}),
    ({"EVENT": "schedule", "SCHED": "17 4 * * 2,4", "LIVE_VAR": "true"},
     {"stage": "publish", "live": "true", "wait": "60", "final": "true"}),
    ({"EVENT": "schedule", "SCHED": "17 2 * * 2,4", "LIVE_VAR": "false"},
     {"stage": "publish", "live": "false", "wait": "60", "final": "false"}),
    ({"EVENT": "workflow_dispatch", "IN_STAGE": "publish", "IN_LIVE": "true", "LIVE_VAR": "true"},
     {"stage": "publish", "live": "true", "wait": "0", "final": "false"}),
    ({"EVENT": "workflow_dispatch", "IN_STAGE": "publish", "IN_LIVE": "false",
      "LIVE_VAR": "true"}, {"stage": "publish", "live": "false", "wait": "0", "final": "false"}),
    ({"EVENT": "workflow_dispatch", "IN_STAGE": "publish", "IN_LIVE": "true",
      "LIVE_VAR": ""}, {"stage": "publish", "live": "false", "wait": "0", "final": "false"}),
    ({"EVENT": "workflow_dispatch", "IN_STAGE": "p1", "IN_LIVE": "true", "LIVE_VAR": "true"},
     {"stage": "p1", "live": "false", "wait": "0", "final": "false"}),
])
def test_resolve_mode(tmp_path, env, expect) -> None:
    code, out = _mode(tmp_path, **env)
    assert code == 0 and out == expect


def test_resolve_mode_unknown_schedule_fails(tmp_path) -> None:
    code, _ = _mode(tmp_path, EVENT="schedule", SCHED="0 0 * * *")
    assert code != 0


def test_secrets_scope() -> None:
    text = WF.read_text(encoding="utf-8")
    job = _wf()["jobs"]["sidestory"]
    notify_env = _step("Notify (Telegram internal)")["env"]
    assert notify_env["BOT_TOKEN"] == "${{ secrets.TELEGRAM_BOT_TOKEN }}"
    assert notify_env["CHAT_ID"] == "${{ secrets.TELEGRAM_PAID_CHANNEL_ID }}"
    assert text.count("secrets.TELEGRAM_BOT_TOKEN") == 1
    assert text.count("secrets.TELEGRAM_PAID_CHANNEL_ID") == 1
    assert "TELEGRAM" not in json.dumps(job["env"])
    assert "TELEGRAM" not in json.dumps(_step("Run sidestory stage")["env"])
    assert _step("Notify (Telegram internal)")["if"].startswith("always()")
    run_env = _step("Run sidestory stage")["env"]
    assert run_env["LIVE"] == "${{ steps.mode.outputs.live == 'true' && '--live' || '' }}"
    assert run_env["DRY_RUN"] == "${{ steps.mode.outputs.live == 'true' && 'false' || 'true' }}"
    assert "--report sidestory_report.json" in _step("Run sidestory stage")["run"]
    assert "--wait-minutes" in _step("Run sidestory stage")["run"]
    restore = _step("Restore previous artifact")
    assert restore["with"]["run-id"] == (
        "${{ steps.art.outputs.id || steps.loc.outputs.run_id }}")


def test_notify_step_bash(tmp_path) -> None:
    """Runs the real notify bash with a fake curl: success text, nothing-to-send, no secret."""
    run = _step("Notify (Telegram internal)")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    (bindir / "curl").write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {log}\n')
    (bindir / "curl").chmod(0o755)
    work = tmp_path / "w"
    work.mkdir()
    base = {"PATH": f"{bindir}:{os.environ['PATH']}", "PYTHONPATH": str(REPO_ROOT),
            "STAGE": "publish", "SIDE_DATE": "2026-10-08", "FINAL": "false", "RUN_URL": URL,
            "BOT_TOKEN": "tok", "CHAT_ID": "-100"}
    (work / "sidestory_report.json").write_text(
        json.dumps(R("publish", "published", {"post_id": "1_2"})), encoding="utf-8")
    r = subprocess.run(["bash", "-c", run], cwd=work, env=base, capture_output=True, text=True)
    sent = log.read_text()
    assert r.returncode == 0 and "notified" in r.stdout
    assert "text=✅" in sent and "https://www.facebook.com/1_2" in sent and "chat_id=-100" in sent
    log.unlink()
    (work / "sidestory_report.json").write_text(
        json.dumps(R("publish", "published", {"reason": "already published"})), encoding="utf-8")
    r = subprocess.run(["bash", "-c", run], cwd=work, env=base, capture_output=True, text=True)
    assert r.returncode == 0 and "nothing to notify" in r.stdout and not log.exists()
    (work / "sidestory_report.json").unlink()
    r = subprocess.run(["bash", "-c", run], cwd=work, env={**base, "BOT_TOKEN": ""},
                       capture_output=True, text=True)
    assert r.returncode == 1 and "missing" in r.stdout

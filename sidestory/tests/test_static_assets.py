"""Static checks on the side workflow and migration (K-1 isolation)."""
from __future__ import annotations

import re

import pytest
import yaml

from sidestory.tests.conftest import REPO_ROOT, SIDE_ROOT
from sidestory.tests.diff_guard import violations

RUN_WF = REPO_ROOT / ".github/workflows/sidestory_run.yml"
MIGRATION = SIDE_ROOT / "migrations/0001_icg_side_schema.sql"


def test_run_workflow_isolation() -> None:
    text = RUN_WF.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    job = wf["jobs"]["sidestory"]
    assert job["env"]["SUPABASE_SCHEMA"] == "icg_side"
    assert wf["concurrency"]["group"].startswith("sidestory-")
    assert wf[True]["schedule"][0]["cron"] == "17 1 * * 2,4"  # Tue/Thu 10:17 KST
    assert "SIDESTORY_SCHEDULE_ENABLED" in job["if"]
    for forbidden in ("X_API_KEY", "X_ACCESS_TOKEN", "TELEGRAM_FREE_CHANNEL_ID",
                      "NOTION_TRACKER_DS"):
        assert forbidden not in text
    # P4 (master decision A): the internal Watchdog channel, in the notify step only.
    notify = next(s for s in job["steps"] if s["name"] == "Notify (Telegram internal)")
    assert set(k for k, v in notify["env"].items() if "secrets." in v) == {"BOT_TOKEN", "CHAT_ID"}
    assert text.count("TELEGRAM_PAID_CHANNEL_ID") == 2   # secret reference + error message


@pytest.mark.parametrize("migration", sorted((SIDE_ROOT / "migrations").glob("000[1-9]_*.sql")),
                         ids=lambda p: p.name)
def test_migration_never_writes_main_schema(migration) -> None:
    sql = migration.read_text(encoding="utf-8").lower()
    for pattern in (r"\balter\s+table\s+icg\.", r"\binsert\s+into\s+icg\.", r"\bupdate\s+icg\.",
                    r"\bdelete\s+from\s+icg\.", r"\bdrop\s+\w+\s+(if\s+exists\s+)?icg\.",
                    r"\bcreate\s+(or\s+replace\s+)?(table|view|function|index)\s+icg\."):
        assert not re.search(pattern, sql), pattern
    created = re.findall(r"\bcreate\s+(?:unique\s+)?(?:table|view|function|index)\s+(\S+)", sql)
    for name in created:
        if "." in name and not name.startswith("side_"):
            assert name.startswith("icg_side."), name


def test_diff_guard_rules() -> None:
    assert violations(["sidestory/core/x.py", ".github/workflows/sidestory_run.yml"]) == []
    assert violations(["engine/a.py"]) == []  # main-only change sets are out of scope
    assert violations(["sidestory/core/x.py", "engine/a.py"]) == ["engine/a.py"]


def test_run_workflow_p1_wiring() -> None:
    text = RUN_WF.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    stage_opts = wf[True]["workflow_dispatch"]["inputs"]["stage"]["options"]
    assert stage_opts == ["gate", "echo", "narrative", "image", "assembly", "inspect", "p1",
                          "refgen", "publish", "verify"]
    steps = {s["name"]: s for s in wf["jobs"]["sidestory"]["steps"]}
    run_env = steps["Run sidestory stage"]["env"]
    for secret in ("ANTHROPIC_API_KEY", "GEMINI_API_SUB_PAY_KEY", "NOTION_API_KEY",
                   "NOTION_SIDE_SYSTEM_ID"):
        assert run_env[secret] == f"${{{{ secrets.{secret} }}}}"
        assert secret not in wf["jobs"]["sidestory"]["env"]  # step-scoped only
    # P2: FACE_* only in the run step, only for publish / verify; never job-wide.
    for secret in ("FACE_PAGE_ID", "FACE_PAGE_TOKEN"):
        assert secret not in wf["jobs"]["sidestory"]["env"]
        assert run_env[secret] == ("${{ (steps.mode.outputs.stage == 'publish' || "
                                   "steps.mode.outputs.stage == 'verify')"
                                   f" && secrets.{secret} || '' }}}}")
        assert text.count(f"secrets.{secret}") == 1
    # Live lock is decided in "Resolve mode" (executed in test_p4_schedule); job stays dry.
    assert run_env["DRY_RUN"].endswith("&& 'false' || 'true' }}")
    assert wf["jobs"]["sidestory"]["env"]["DRY_RUN"] == "true"
    assert wf[True]["workflow_dispatch"]["inputs"]["publish_live"]["default"] is False
    assert "inputs.stage == 'gate' || inputs.stage == 'echo'" in run_env["NO_PERSIST"]
    assert "--ref-revision" in steps["Run sidestory stage"]["run"]
    upload = steps["Upload side artifacts"]
    assert "refgen" in upload["if"]
    assert upload["with"]["path"] == "output/sidestory/"
    assert "github.run_id" in upload["with"]["name"]
    assert "fonts-noto-cjk" in steps["Install Korean font (slide composer)"]["run"]
    assert "cache-hit != 'true'" in steps["Install Korean font (slide composer)"]["if"]
    assert steps["Restore Korean font cache"]["uses"] == "actions/cache/restore@v4"
    assert steps["Save Korean font cache"]["with"]["key"] == steps["Restore Korean font cache"]["with"]["key"]
    assert "test -s" in steps["Place Korean font"]["run"]
    assert "^[0-9]+$" in steps["Validate artifact_run_id"]["run"]
    restore = steps["Restore previous artifact"]
    assert restore["if"] == "steps.art.outputs.id != '' || steps.loc.outputs.run_id != ''"
    assert restore["with"]["run-id"] == "${{ steps.art.outputs.id || steps.loc.outputs.run_id }}"
    assert restore["with"]["path"] == "output/sidestory"


def test_ledger_scope_matches_p1_output_dir() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "^output/sidestory/[0-9]{4}-[0-9]{2}-[0-9]{2}/panels$" in sql
    from sidestory.app.p1 import P1Deps

    assert P1Deps.__dataclass_fields__["output_root"].default.as_posix() == "output/sidestory"

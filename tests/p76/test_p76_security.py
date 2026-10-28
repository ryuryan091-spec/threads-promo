"""보안 테스트 T-SEC-01~09 (SEC-P76-001 §6) · T1-19 정적 검사."""
from __future__ import annotations

import importlib.util
import logging
import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from p76_fake import (
    DB05,
    DB09,
    FAKE_BOT,
    FAKE_NOTION_TOKEN,
    FAKE_PAGE_ID,
    FAKE_TOKEN,
    FakeResp,
    add_episode,
    err,
)

from p76 import (
    cli,
    notion,
    redact,
    run_calendar,
    run_health,
    run_hiatus_publish,
    run_insights,
    settings,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("p76_*.yml"))
P76_PATHS = [ROOT / "p76", ROOT / "tests" / "p76", ROOT / "scripts" / "p76_secret_scan.py", *WORKFLOWS]


def _load_scanner():
    spec = importlib.util.spec_from_file_location("p76_secret_scan", ROOT / "scripts" / "p76_secret_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan_secrets = _load_scanner()
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
STANDARD_MODE = "${{ github.event_name == 'workflow_dispatch' && inputs.mode || vars.PAGE76_MODE || 'dry_run' }}"


# ── T-SEC-01 로그·알림 비식별 ────────────────────────────
def test_t_sec_01_redact_values(env):
    env(PAGE76_TELEGRAM_BOT_TOKEN=FAKE_BOT, PAGE76_TELEGRAM_CHAT_ID="-100777888999")
    text = f"{FAKE_TOKEN} {FAKE_NOTION_TOKEN} {FAKE_BOT} {FAKE_PAGE_ID} {'d5' * 16} -100777888999 access_token=abc"
    out = redact.redact(text)
    for secret in (FAKE_TOKEN, FAKE_NOTION_TOKEN, FAKE_BOT, FAKE_PAGE_ID, "d5" * 16, "-100777888999", "abc"):
        assert secret not in out


def test_t_sec_01_pipeline_logs_clean(fake_notion, fake_graph, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    body = "비밀 본문 문장\n※ 여러 사례를 재구성한 픽션입니다.\n#사건파일76"
    fake_notion.add(DB09, **{settings.C_KEY: "2026-11-10", settings.C_DATE: "2026-11-10",
                             settings.C_KIND: settings.KIND_HIATUS_CONTENT, settings.C_STATE: settings.S_APPROVED,
                             settings.C_REVIEWED: True, settings.C_BODY: body, settings.C_CONTENT: settings.CONTENT_TEASER})
    fake_graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = err(190, 401)
    from p76_fake import png_bytes

    def render(label, headline, meta, work_dir, runner=None):
        p = Path(work_dir) / "card.png"
        p.write_bytes(png_bytes(1080, 1350))
        return str(p)
    monkeypatch.setattr("p76.card_render.render", render)
    from datetime import datetime
    cli.execute("hiatus_publish", "t", lambda log: run_hiatus_publish.body(
        log, now=datetime(2026, 11, 10, 12, tzinfo=notion.KST)))
    text = caplog.text
    assert "회로 차단" in text
    for secret in (FAKE_TOKEN, FAKE_NOTION_TOKEN, FAKE_PAGE_ID, "d9" * 16, "비밀 본문"):
        assert secret not in text


# ── T-SEC-02·03 쓰기 경계 ────────────────────────────────
@pytest.fixture
def busy_ledger(fake_notion, fake_graph, monkeypatch):
    add_episode(fake_notion, 35, date(2026, 11, 9), feed_id="61_35")
    add_episode(fake_notion, 36, None, state=settings.S_APPROVED)
    g = fake_graph
    g.handlers[("GET", f"{FAKE_PAGE_ID}?id,name,followers_count")] = FakeResp(200, {"followers_count": 1})
    g.handlers[("GET", "debug_token")] = FakeResp(200, {"data": {"is_valid": True, "type": "PAGE", "scopes": []}})
    g.handlers[("GET", f"{FAKE_PAGE_ID}/posts")] = FakeResp(200, {"data": [
        {"id": "61_35", "created_time": "2026-11-09T11:31:00+0000", "status_type": "added_photos"}]})
    g.handlers[("GET", "61_35?reactions.summary(total_count).limit(0),shares")] = FakeResp(200, {})
    g.handlers[("GET", "61_35?comments.summary(total_count).limit(0)")] = FakeResp(200, {})
    return fake_notion, g


def _all_pipelines(today=date(2026, 11, 10)):
    from datetime import datetime
    cli.execute("calendar", "t", lambda log: run_calendar.body(log, today=today))
    cli.execute("health", "t", lambda log: run_health.body(log, today=today), require_enabled=False)
    cli.execute("insights", "t", lambda log: run_insights.body(log, today=date(2026, 11, 12)))
    cli.execute("hiatus_publish", "t", lambda log: run_hiatus_publish.body(
        log, now=datetime(2026, 11, 10, 20, tzinfo=notion.KST)))


@pytest.mark.parametrize("mode", ["dry_run", "shadow", "live"])
def test_t_sec_02_03_write_boundaries(busy_ledger, env, mode):
    fake, g = busy_ledger
    env(PAGE76_MODE=mode)
    _all_pipelines()
    db05_pages = {p["id"] for p in fake.rows(DB05)}
    for _method, path, body in fake.writes():
        assert not any(pid in path for pid in db05_pages), "DB-05 PATCH 금지"
        if body and "parent" in body:
            assert body["parent"]["database_id"] != DB05
    if mode == "dry_run":
        assert fake.writes() == []
    if mode in ("dry_run", "shadow"):
        assert g.writes() == []


def test_t_sec_03_guard_raises(fake_notion):
    page = add_episode(fake_notion, 1, date(2026, 10, 1))
    list(notion.query(DB05, {}))
    with pytest.raises(notion.ReadOnlyViolation):
        notion.update_page(page["id"], {})
    with pytest.raises(notion.ReadOnlyViolation):
        notion.create_page(DB05, {})


# ── T-SEC-07 비밀 패턴 검사 ──────────────────────────────
def test_t_sec_07_scanner_catches(tmp_path):
    leaked = tmp_path / "leak.txt"
    leaked.write_text("token = " + "EAA" + "Qw3" * 15 + "\nid = " + "ab12" * 8 + "\n", encoding="utf-8")
    hits = scan_secrets.scan([leaked])
    assert {h.split(": ")[-1] for h in hits} == {"meta_token", "notion_id"}


def test_t_sec_07_repo_clean():
    files = []
    for base in P76_PATHS:
        files += [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert scan_secrets.scan(files) == []


# ── T-SEC-08 · T1-19 워크플로 정적 검사 ──────────────────
@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_t_sec_08_workflow_rules(path):
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    on = doc.get(True) or doc.get("on")
    assert "pull_request_target" not in on
    assert doc["permissions"] == {"contents": "read"}
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert SHA_PIN.match(step["uses"]), step["uses"]
            assert "${{ inputs" not in step.get("run", ""), "inputs 는 env 경유(STD-SCHED-08)"
        if "${{ secrets." in text:
            assert job.get("if") == "github.ref == 'refs/heads/main'", "RR-06 main 가드"
    assert STANDARD_MODE in text
    for entry in on["schedule"]:
        minute = entry["cron"].split()[0]
        assert minute not in ("0", "30"), "STD-SCHED-01"
        assert entry["cron"] in text.split("name:")[0], "cron↔주석 매핑"
    module = re.search(r"python -m (p76\.\w+)", text).group(1)
    assert (ROOT / (module.replace(".", "/") + ".py")).exists()


def test_p76_workflow_set():
    assert [p.name for p in WORKFLOWS] == [
        "p76_calendar.yml", "p76_health.yml", "p76_hiatus_publish.yml", "p76_insights.yml", "p76_novel.yml",
        "p76_novel_publish.yml"]


def test_reused_secrets_not_duplicated():
    """기존 Secret 재사용 (DN2026_0070 노션·텔레그램, DN2026_0071 Anthropic, DN2026_0072 소설 알림) — 전용 Secret 을 다시 만들지 않는다."""
    reuse = {"PAGE76_CLAUDE_KEY": "CLAUDE_AI_KEY", "PAGE76_NOTION_TOKEN": "NOTION_TOKEN",
             "PAGE76_TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN", "PAGE76_TELEGRAM_CHAT_ID": "TELEGRAM_ALERT_CHAT_ID",
             "PAGE76_NOVEL_TG_BOT_TOKEN": "TELEGRAM_BOT_TOKEN", "PAGE76_NOVEL_TG_CHAT_ID": "TELEGRAM_ALERT_CHAT_ID"}
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        for env_name, existing in reuse.items():
            assert f"secrets.{env_name} " not in text and f"secrets.{env_name}}}" not in text, (path.name, env_name)
            if f"{env_name}:" in text:
                assert f"{env_name}: ${{{{ secrets.{existing} }}}}" in text, (path.name, env_name)


def test_no_reply_or_dm_endpoints():
    """답글·DM 엔드포인트는 코드에 두지 않는다 (DSN §9)."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "p76").glob("*.py"))
    for forbidden in ("/comments\"", "/messages", "private_replies", "is_hidden"):
        assert forbidden not in source

from __future__ import annotations

import pytest
from p76_fake import BASE_ENV, DB05, DB08, DB09, DB11, DB12, FakeGraph, FakeNotion

from p76 import graph, notion, safety, settings


@pytest.fixture
def env(monkeypatch):
    """환경변수 설정 → settings 재적재. env(PAGE76_MODE="dry_run") 처럼 덮어쓴다."""

    def apply(**overrides: str) -> None:
        for key in list(BASE_ENV) + ["PAGE76_EXPECTED_SCOPES", "PAGE76_INSIGHTS_METRICS", "PAGE76_HIATUS_DAYS",
                                   "PAGE76_SCHEDULE_ANCHOR"]:
            monkeypatch.delenv(key, raising=False)
        for key, value in {**BASE_ENV, **overrides}.items():
            monkeypatch.setenv(key, value)
        settings.reload()

    apply()
    yield apply
    monkeypatch.undo()
    settings.reload()


@pytest.fixture(autouse=True)
def _clean_state():
    safety.reset()
    notion._reset_readonly()
    yield
    safety.reset()
    notion._reset_readonly()


@pytest.fixture
def fake_notion(env, monkeypatch):
    fake = FakeNotion()
    fake.add_db(DB05, {**settings.DB05_REQUIRED, "피드본문": "rich_text"})
    fake.add_db(DB08, settings.DB08_REQUIRED)
    fake.add_db(DB09, settings.DB09_REQUIRED)
    fake.add_db(DB11, settings.DB11_REQUIRED)
    fake.add_db(DB12, settings.DB12_REQUIRED)
    monkeypatch.setattr(notion, "_request", fake.request)
    return fake


@pytest.fixture
def fake_graph(env, monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(graph, "requests", fake)
    monkeypatch.setattr(graph, "_sleep", lambda s: None)
    return fake

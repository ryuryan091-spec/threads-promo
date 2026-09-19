"""전수 테스트 보강 — run_insights / run_weighting 엔트리포인트 E2E.

v1.1.0 에서 두 러너의 조회 방식(since·페이지네이션)과 CHAT 분리가 바뀌었는데
run() 자체를 태우는 테스트가 없었다. HTTP 계층만 가짜 응답으로 대체해 검증한다.
"""

from __future__ import annotations

import datetime as dt
import os
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

KST = ZoneInfo("Asia/Seoul")
NOW = dt.datetime.now(dt.UTC)

BASE_ENV = {
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "THREADS_USER_ID": "123456789012345",
}


@pytest.fixture(autouse=True)
def _env():
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("THREADS_", "TELEGRAM_", "GITHUB_STEP")):
            os.environ.pop(key, None)
    os.environ.update(BASE_ENV)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _ts(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _day_at(days_ago: int, hh: int, mm: int) -> dt.datetime:
    d = NOW.astimezone(KST).date() - dt.timedelta(days=days_ago)
    return dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST)


def _posts() -> list[dict]:
    """최근 3일: 하루 정기 1건(이미지 08:25) + CHAT 3건(텍스트 09~11시)."""
    out = []
    for d in range(1, 4):
        out.append({"id": f"reg{d}", "text": "정기", "media_type": "IMAGE",
                    "timestamp": _ts(_day_at(d, 8, 25))})
        for i, (hh, mm) in enumerate(((9, 10), (10, 5), (11, 20))):
            out.append({"id": f"chat{d}{i}", "text": "잡담", "media_type": "TEXT_POST",
                        "timestamp": _ts(_day_at(d, hh, mm))})
    return out


class FakeApi:
    def __init__(self):
        self.list_params: list[dict] = []
        self.media_ids: list[str] = []

    def __call__(self, method, url, *, params):
        if url.endswith("/threads_insights"):
            return {"data": [
                {"name": "clicks", "link_total_values": [
                    {"value": 9, "link_url": "https://www.youtube.com/@x"}]},
                {"name": "followers_count", "total_value": {"value": 42}},
            ]}
        if url.endswith("/threads") and method == "GET":
            self.list_params.append(dict(params))
            return {"data": _posts()}
        if url.endswith("/insights"):
            self.media_ids.append(url.rsplit("/", 2)[-2])
            return {"data": [{"name": "replies", "values": [{"value": 2}]},
                             {"name": "views", "values": [{"value": 10}]}]}
        return {}


def _run(module_name: str):
    import importlib

    mod = importlib.import_module(f"src.{module_name}")
    fake = FakeApi()
    with (
        mock.patch("src.threads_client._request", side_effect=fake),
        mock.patch.object(mod.notifier, "send") as send,
    ):
        code = mod.main()
    return code, fake, send


class TestInsightsRunner:
    def test_run_counts_chat_row_and_uses_since(self):
        code, fake, send = _run("run_insights")
        assert code == 0
        params = fake.list_params[0]
        assert "since" in params and "media_type" in params["fields"]
        report = send.call_args.args[2]
        assert "CHAT" in report
        # 12건 전부 인사이트 조회(리포트는 CHAT 도 보여준다)
        assert len(fake.media_ids) == 12

    def test_chat_rows_classified_by_media_type(self):
        code, _, send = _run("run_insights")
        report = send.call_args.args[2]
        chat_line = next(line for line in report.splitlines() if line.strip().startswith("CHAT"))
        assert chat_line.split()[1] == "9"      # CHAT 9건
        assert "판정불가" not in report          # 정기 3건은 슬롯 A 로 복원된다


class TestWeightingRunner:
    def test_run_skips_chat_insights_calls(self):
        code, fake, send = _run("run_weighting")
        assert code == 0
        # 인사이트 호출은 정기 글 3건에만 (CHAT 9건 제외)
        assert sorted(fake.media_ids) == ["reg1", "reg2", "reg3"]
        params = fake.list_params[0]
        assert "since" in params
        assert send.called

    def test_blocked_returns_7(self):
        from src import run_weighting
        from src.threads_client import ThreadsApiError

        blocked = ThreadsApiError(400, "access blocked", code=200)
        with mock.patch("src.threads_client._request", side_effect=blocked):
            assert run_weighting.main() == 7

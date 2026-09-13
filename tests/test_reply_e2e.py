"""답글 엔진 엔드투엔드 + API 레이어 전수 테스트."""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest
import requests

BASE_ENV = {
    "GITHUB_REPOSITORY": "owner/repo",
    "GITHUB_REF_NAME": "main",
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "YOUTUBE_URL": "https://www.youtube.com/@handle",
    "X_URL": "https://x.com/handle",
    "DRY_RUN": "false",
}


@pytest.fixture(autouse=True)
def _env():
    from src import config

    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("THREADS_", "AI_", "REPLY_", "PUBLISH_", "IMAGE_",
                           "ASSET_", "SLOT", "EVENT_", "CLAUDE_")):
            os.environ.pop(key, None)
    os.environ.update(BASE_ENV)
    with (
        mock.patch.object(config, "YOUTUBE_URL", BASE_ENV["YOUTUBE_URL"]),
        mock.patch.object(config, "X_URL", BASE_ENV["X_URL"]),
        mock.patch.object(config, "REPLY_ENABLED", True),
    ):
        yield
    os.environ.clear()
    os.environ.update(saved)


class FakeReplyApi:
    """답글 시나리오용 응답 모의."""

    def __init__(self, comments: list[dict], *, reply_quota_used: int = 0):
        self.comments = comments
        self.reply_quota_used = reply_quota_used
        self.created: list[dict] = []
        self.published: list[str] = []

    def __call__(self, method: str, url: str, *, params: dict):
        if url.endswith("/me"):
            return {"id": "999", "username": "ryuryan091"}

        if url.endswith("/threads_publishing_limit"):
            if "reply_quota_usage" in params.get("fields", ""):
                return {"data": [{"reply_quota_usage": self.reply_quota_used,
                                  "reply_config": {"quota_total": 1000}}]}
            return {"data": [{"quota_usage": 0, "config": {"quota_total": 250}}]}

        if url.endswith("/conversation"):
            return {"data": self.comments}

        if url.endswith("/threads") and method == "GET":
            return {"data": [{"id": "post1", "text": "내 원글", "timestamp": ""}]}

        if url.endswith("/threads") and method == "POST":
            self.created.append(dict(params))
            return {"id": f"c{len(self.created)}"}

        if url.endswith("/threads_publish"):
            self.published.append(params.get("creation_id", ""))
            return {"id": f"r{len(self.published)}"}

        if "status" in params.get("fields", ""):
            return {"status": "FINISHED", "id": "c1"}

        return {}


def _comment(cid: str, text: str, *, user="u1", mine=False, parent="post1") -> dict:
    return {
        "id": cid,
        "text": text,
        "username": user,
        "timestamp": "2026-09-13T00:00:00+0000",
        "replied_to": {"id": parent},
        "is_reply": True,
        "is_reply_owned_by_me": mine,
        "hide_status": "NOT_HUSHED",
    }


def _run_reply(fake: FakeReplyApi, *, ai_text: str | None = "답글 본문입니다") -> int:
    from src import run_reply

    def gen(api_key, post_text, comment_text):
        if ai_text is None:
            raise RuntimeError("생성 실패")
        return ai_text

    ctx = [
        mock.patch("src.threads_client._request", side_effect=fake),
        mock.patch("src.threads_client.time.sleep"),
        mock.patch("src.antibot.time.sleep"),
        mock.patch.object(run_reply, "_acquire_token", return_value="THAAtoken"),
        mock.patch.object(run_reply.notifier, "send"),
        mock.patch("src.reply_engine._generate_reply", side_effect=gen),
    ]
    for c in ctx:
        c.start()
    try:
        return run_reply.run()
    finally:
        for c in reversed(ctx):
            c.stop()


class TestReplyEngineE2E:
    def test_korean_comment_gets_reply(self):
        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([_comment("r1", "오늘 글 잘 봤습니다")])
        assert _run_reply(fake) == 0
        assert len(fake.published) == 1
        assert fake.created[0]["reply_to_id"] == "r1"

    def test_foreign_comment_gets_canned_korean(self):
        from src import reply_engine

        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([_comment("r1", "Great post, thanks!")])
        assert _run_reply(fake) == 0
        assert fake.created[0]["text"] in reply_engine.NON_KOREAN_REPLIES

    def test_choice_comment_gets_neutral(self):
        from src import reply_engine

        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([_comment("r1", "A랑 B 중에 뭐가 나은가요?")])
        assert _run_reply(fake) == 0
        assert fake.created[0]["text"] in reply_engine.NEUTRAL_THANKS_REPLIES

    def test_own_comment_skipped(self):
        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([_comment("r1", "내가 쓴 댓글", mine=True)])
        assert _run_reply(fake) == 0
        assert fake.published == []

    def test_already_replied_skipped(self):
        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([
            _comment("r1", "남의 댓글"),
            _comment("r2", "내 답글", mine=True, parent="r1"),
        ])
        assert _run_reply(fake) == 0
        assert fake.published == []

    def test_author_cap_enforced(self):
        from src import config

        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        comments = [_comment(f"r{i}", f"한국어 댓글 {i}") for i in range(6)]
        fake = FakeReplyApi(comments)
        assert _run_reply(fake) == 0
        assert len(fake.published) <= config.REPLY_AUTHOR_DAILY_CAP

    def test_daily_cap_enforced(self):
        from src import config

        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        comments = [
            _comment(f"r{i}", f"한국어 댓글 {i}", user=f"user{i}") for i in range(40)
        ]
        fake = FakeReplyApi(comments)
        assert _run_reply(fake) == 0
        assert len(fake.published) <= config.REPLY_DAILY_CAP

    def test_no_ai_key_skips_normal_comments(self):
        fake = FakeReplyApi([_comment("r1", "오늘 글 잘 봤습니다")])
        assert _run_reply(fake) == 0
        assert fake.published == []

    def test_dry_run_publishes_nothing(self):
        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        os.environ["DRY_RUN"] = "true"
        fake = FakeReplyApi([_comment("r1", "오늘 글 잘 봤습니다")])
        assert _run_reply(fake) == 0
        assert fake.published == []

    def test_disabled_returns_early(self):
        from src import config

        fake = FakeReplyApi([_comment("r1", "댓글")])
        with mock.patch.object(config, "REPLY_ENABLED", False):
            assert _run_reply(fake) == 0
        assert fake.created == []

    def test_quota_exhausted_stops(self):
        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([_comment("r1", "댓글입니다")], reply_quota_used=1000)
        assert _run_reply(fake) == 0
        assert fake.published == []

    def test_generation_failure_skips_comment(self):
        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([_comment("r1", "오늘 글 잘 봤습니다")])
        assert _run_reply(fake, ai_text=None) == 0
        assert fake.published == []

    def test_reply_text_passes_lint(self):
        from src import content

        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([
            _comment("r1", "오늘 글 잘 봤습니다"),
            _comment("r2", "Nice work!", user="u2"),
            _comment("r3", "A랑 B 중 뭐가 나은가요", user="u3"),
        ])
        _run_reply(fake)
        for created in fake.created:
            content.lint(created["text"])

    def test_investment_advice_reply_blocked(self):
        os.environ["CLAUDE_AI_KEY"] = "sk-ant-test"
        fake = FakeReplyApi([_comment("r1", "오늘 글 잘 봤습니다")])
        assert _run_reply(fake, ai_text="지금 매수 타이밍입니다") == 0
        assert fake.published == []


class TestApiLayer:
    """_request 의 오류 처리."""

    @staticmethod
    def _resp(status: int, body):
        r = mock.Mock()
        r.status_code = status
        r.text = body if isinstance(body, str) else json.dumps(body)
        r.json.return_value = body if isinstance(body, dict) else {}
        return r

    def test_success_returns_json(self):
        from src import threads_client as tc

        with mock.patch.object(
            tc.requests, "request", return_value=self._resp(200, {"id": "1"})
        ):
            assert tc._request("GET", "https://x/y", params={})["id"] == "1"

    def test_error_raises_with_code(self):
        from src import threads_client as tc

        body = {"error": {"code": 36001, "message": "bad image"}}
        with (
            mock.patch.object(
                tc.requests, "request", return_value=self._resp(400, body)
            ),
            pytest.raises(tc.ThreadsApiError) as info,
        ):
            tc._request("GET", "https://x/y", params={})
        assert info.value.code == 36001

    def test_network_error_raises_api_error(self):
        from src import threads_client as tc

        with (
            mock.patch.object(
                tc.requests, "request", side_effect=requests.RequestException("down")
            ),
            mock.patch.object(tc.time, "sleep"),
            pytest.raises(tc.ThreadsApiError),
        ):
            tc._request("GET", "https://x/y", params={})

    def test_auth_error_flagged(self):
        from src import threads_client as tc

        body = {"error": {"code": 190, "message": "Invalid token"}}
        with (
            mock.patch.object(
                tc.requests, "request", return_value=self._resp(400, body)
            ),
            pytest.raises(tc.ThreadsApiError) as info,
        ):
            tc._request("GET", "https://x/y", params={})
        assert info.value.is_auth_error

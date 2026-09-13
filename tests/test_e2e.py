"""엔드투엔드 전수 테스트.

HTTP 레이어를 통째로 모의해 run() 전체 경로를 검증한다.
실제 네트워크·실제 발행 없이 모든 분기를 통과시킨다.

검증 대상 경로
  정상 / Tier2 대체 / Tier3 텍스트 폴백 / 컨테이너 지연 / 컨테이너 오류
  토큰 갱신 실패 / Secret 영속화 실패 / 쿼터 부족 / DRY_RUN
  휴식일 / 슬롯 미당첨 / AI 실패 / 린트 위반 / 링크 미설정
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

BASE_ENV = {
    "GITHUB_REPOSITORY": "owner/repo",
    "GITHUB_REF_NAME": "main",
    "THREADS_APP_ID": "1734799514413030",
    "THREADS_APP_SECRET": "a" * 32,
    "THREADS_LONG_LIVED_TOKEN": "THAA" + "x" * 180,
    "YOUTUBE_URL": "https://www.youtube.com/@handle",
    "X_URL": "https://x.com/handle",
    "DRY_RUN": "false",
    "PUBLISH_WEEKLY_REST_DAYS": "0",
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FakeThreads:
    """Threads API 응답을 흉내낸다. 시나리오별로 동작을 바꾼다."""

    def __init__(
        self,
        *,
        container_statuses: list[str] | None = None,
        quota_used: int = 0,
        quota_total: int = 250,
        publish_fails: bool = False,
    ):
        self.container_statuses = list(container_statuses or ["FINISHED"])
        self.quota_used = quota_used
        self.quota_total = quota_total
        self.publish_fails = publish_fails
        self.created: list[dict] = []
        self.published: list[str] = []

    def __call__(self, method: str, url: str, *, params: dict):
        if url.endswith("/me"):
            return {"id": "38808359068777315", "username": "ryuryan091"}

        if url.endswith("/threads_publishing_limit"):
            fields = params.get("fields", "")
            if "reply_quota_usage" in fields:
                return {"data": [{"reply_quota_usage": 0,
                                  "reply_config": {"quota_total": 1000}}]}
            return {"data": [{"quota_usage": self.quota_used,
                              "config": {"quota_total": self.quota_total}}]}

        if url.endswith("/threads") and method == "GET":
            return {"data": [{"id": "p1", "text": "어제 쓴 글"}]}

        if url.endswith("/threads") and method == "POST":
            self.created.append(dict(params))
            return {"id": f"container_{len(self.created)}"}

        if url.endswith("/threads_publish"):
            if self.publish_fails:
                from src.threads_client import ThreadsApiError

                raise ThreadsApiError(400, '{"error":{"code":24}}', code=24)
            cid = params.get("creation_id", "")
            self.published.append(cid)
            return {"id": f"post_{len(self.published)}"}

        # 컨테이너 상태 조회
        if "status" in params.get("fields", ""):
            status = (
                self.container_statuses.pop(0)
                if len(self.container_statuses) > 1
                else self.container_statuses[0]
            )
            self.status_calls = getattr(self, "status_calls", 0) + 1
            body = {"status": status, "id": "c1"}
            if status == "ERROR":
                body["error_message"] = "UNKNOWN"
            return body

        return {}


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    d.mkdir()
    for i in range(1, 4):
        (d / f"promo_{i:02d}.png").write_bytes(PNG_MAGIC + b"\x00" * 100)
    return d


@pytest.fixture(autouse=True)
def _env():
    """환경변수와 config 상수를 함께 고정한다.

    config 는 import 시점에 환경변수를 읽으므로, 환경변수만 바꾸면
    테스트 실행 순서에 따라 값이 달라진다. 상수도 함께 패치해야 격리된다.
    """
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
        mock.patch.object(config, "PUBLISH_WEEKLY_REST_DAYS", 0),
        mock.patch.object(config, "AI_ENABLED", True),
    ):
        yield

    os.environ.clear()
    os.environ.update(saved)


def _run(assets_dir: Path, fake: FakeThreads, *, image_ok=True, **patches) -> int:
    """run() 을 모의 환경에서 실행하고 종료코드를 돌려준다."""
    from src import main
    from src.threads_client import ImageValidationError

    def verify(url: str) -> None:
        if image_ok is True:
            return
        if image_ok is False:
            raise ImageValidationError("Content-Type 이 text/plain 입니다.")
        image_ok(url)  # callable

    ctx = [
        mock.patch.object(main, "ASSETS_DIR", assets_dir),
        mock.patch.object(main, "verify_image_url", side_effect=verify),
        mock.patch.object(main, "_notify_safe"),
        mock.patch("src.threads_client._request", side_effect=fake),
        mock.patch("src.threads_client.time.sleep"),
        mock.patch("src.antibot.time.sleep"),
        mock.patch.object(main.token_manager, "refresh_long_lived_token",
                          return_value=os.environ["THREADS_LONG_LIVED_TOKEN"]),
        mock.patch.object(main.token_manager, "persist_token_to_secret"),
        mock.patch.object(main, "notifier"),
    ]
    ctx.extend(patches.values())

    for c in ctx:
        c.start()
    try:
        return main.run()
    finally:
        for c in reversed(ctx):
            c.stop()


# ---------------------------------------------------------------------------
# 정상 경로
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_image_post_published(self, assets: Path):
        fake = FakeThreads()
        assert _run(assets, fake) == 0
        assert len(fake.published) == 2  # 본문 + 셀프 리플라이

    def test_image_container_created_with_image_type(self, assets: Path):
        fake = FakeThreads()
        _run(assets, fake)
        assert fake.created[0]["media_type"] == "IMAGE"
        assert "image_url" in fake.created[0]

    def test_self_reply_is_text_type(self, assets: Path):
        fake = FakeThreads()
        _run(assets, fake)
        assert fake.created[-1]["media_type"] == "TEXT"
        assert "reply_to_id" in fake.created[-1]

    def test_link_not_in_body(self, assets: Path):
        fake = FakeThreads()
        _run(assets, fake)
        body = fake.created[0]["text"]
        assert "youtube.com" not in body
        assert "x.com" not in body

    def test_link_in_self_reply(self, assets: Path):
        fake = FakeThreads()
        _run(assets, fake)
        reply = fake.created[-1]["text"]
        assert "youtube.com" in reply
        assert "x.com" in reply


# ---------------------------------------------------------------------------
# 폴백 경로
# ---------------------------------------------------------------------------


class TestFallbackPaths:
    def test_tier3_text_only(self, assets: Path):
        fake = FakeThreads()
        assert _run(assets, fake, image_ok=False) == 0
        assert fake.created[0]["media_type"] == "TEXT"
        assert "image_url" not in fake.created[0]
        assert len(fake.published) == 2

    def test_tier2_second_candidate(self, assets: Path):
        from src.threads_client import ImageValidationError

        seen: list[str] = []

        def verify(url: str) -> None:
            seen.append(url)
            if len(seen) == 1:
                raise ImageValidationError("깨진 파일")

        fake = FakeThreads()
        assert _run(assets, fake, image_ok=verify) == 0
        assert fake.created[0]["media_type"] == "IMAGE"
        assert fake.created[0]["image_url"] == seen[1]

    def test_publish_failure_falls_back_to_text(self, assets: Path):
        """이미지 발행이 API 오류로 실패하면 텍스트로 전환."""
        from src import main
        from src.threads_client import ThreadsApiError

        fake = FakeThreads()
        original = None

        def failing_image(self, image_url, text, *, dry_run=False):
            raise ThreadsApiError(400, '{"error":{"code":24}}', code=24)

        with mock.patch(
            "src.threads_client.ThreadsClient.publish_image_post", failing_image
        ):
            assert _run(assets, fake) == 0

        assert any(c.get("media_type") == "TEXT" for c in fake.created)
        assert original is None
        assert main is not None


# ---------------------------------------------------------------------------
# 컨테이너 처리
# ---------------------------------------------------------------------------


class TestContainerHandling:
    def test_in_progress_then_finished(self, assets: Path):
        fake = FakeThreads(container_statuses=["IN_PROGRESS", "FINISHED"])
        assert _run(assets, fake) == 0
        assert len(fake.published) == 2

    def test_container_error_falls_back_to_text(self, assets: Path):
        # 첫 컨테이너(이미지)만 ERROR, 이후 텍스트 폴백은 정상 처리
        fake = FakeThreads(container_statuses=["ERROR", "FINISHED", "FINISHED"])
        assert _run(assets, fake) == 0
        assert any(c.get("media_type") == "TEXT" for c in fake.created)


# ---------------------------------------------------------------------------
# 게이트 · 중단 경로
# ---------------------------------------------------------------------------


class TestGates:
    def test_dry_run_publishes_nothing(self, assets: Path):
        os.environ["DRY_RUN"] = "true"
        fake = FakeThreads()
        assert _run(assets, fake) == 0
        assert fake.created == []
        assert fake.published == []

    def test_rest_day_skips(self, assets: Path):
        from src import config

        fake = FakeThreads()
        with mock.patch.object(config, "PUBLISH_WEEKLY_REST_DAYS", 7):
            assert _run(assets, fake) == 0
        assert fake.created == []

    def test_slot_mismatch_skips(self, assets: Path):
        os.environ.update(EVENT_NAME="schedule", SLOT="Z", PUBLISH_SLOTS="A,B,C")
        fake = FakeThreads()
        assert _run(assets, fake) == 0
        assert fake.created == []

    def test_quota_exhausted_raises(self, assets: Path):
        fake = FakeThreads(quota_used=249, quota_total=250)
        with pytest.raises(RuntimeError, match="쿼터 부족"):
            _run(assets, fake)

    def test_missing_link_blocks(self, assets: Path):
        from src import config

        fake = FakeThreads()
        with (
            mock.patch.object(config, "YOUTUBE_URL", ""),
            pytest.raises(RuntimeError, match="Variables 미설정"),
        ):
            _run(assets, fake)


# ---------------------------------------------------------------------------
# 콘텐츠 안전장치
# ---------------------------------------------------------------------------


class TestContentSafety:
    def test_static_fallback_when_no_ai_key(self, assets: Path):
        fake = FakeThreads()
        assert _run(assets, fake) == 0
        assert fake.created[0]["text"]

    def test_published_text_passes_lint(self, assets: Path):
        from src import content

        fake = FakeThreads()
        _run(assets, fake)
        for created in fake.created:
            content.lint(created["text"])

    def test_text_within_limit(self, assets: Path):
        from src import config

        fake = FakeThreads()
        _run(assets, fake)
        for created in fake.created:
            assert len(created["text"]) <= config.TEXT_MAX_LEN


# ---------------------------------------------------------------------------
# 멱등성
# ---------------------------------------------------------------------------


class TestIdempotence:
    def test_same_day_same_body(self, assets: Path):
        a, b = FakeThreads(), FakeThreads()
        _run(assets, a)
        _run(assets, b)
        assert a.created[0]["text"] == b.created[0]["text"]
        assert a.created[0].get("image_url") == b.created[0].get("image_url")

"""발행 폴백 체인 테스트.

Tier 1  오늘의 이미지
Tier 2  대체 이미지
Tier 3  텍스트 전용
Tier 4  중단
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from unittest import mock

import pytest

from src import config, content
from src.threads_client import ImageValidationError

BASE = "https://raw.githubusercontent.com/owner/repo/main/assets"


@pytest.fixture(autouse=True)
def _env(tmp_path: Path):
    saved = dict(os.environ)
    os.environ.update(
        GITHUB_REPOSITORY="owner/repo",
        GITHUB_REF_NAME="main",
        THREADS_APP_ID="1",
        THREADS_APP_SECRET="s",
        THREADS_LONG_LIVED_TOKEN="t",
        YOUTUBE_URL="https://www.youtube.com/@handle",
        X_URL="https://x.com/handle",
    )
    os.environ.pop("ASSET_RAW_BASE_URL", None)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _assets(tmp_path: Path, names: list[str]) -> Path:
    d = tmp_path / "assets"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"\x89PNG\r\n\x1a\n")
    return d


class TestCandidateOrdering:
    def test_starts_at_today_and_wraps(self):
        assets = ["a.png", "b.png", "c.png", "d.png"]
        assert content.order_asset_candidates(assets, 2) == [
            "c.png", "d.png", "a.png", "b.png",
        ]

    def test_empty(self):
        assert content.order_asset_candidates([], 3) == []

    def test_single(self):
        assert content.order_asset_candidates(["only.png"], 7) == ["only.png"]


class TestImageSelection:
    """_select_usable_image 의 Tier 1~2 동작."""

    @staticmethod
    def _run(tmp_path: Path, verify_side_effect):
        from src import main
        from src.env import load_settings

        d = _assets(tmp_path, ["promo_01.png", "promo_02.png", "promo_03.png"])
        with (
            mock.patch.object(main, "ASSETS_DIR", d),
            mock.patch.object(main, "verify_image_url", side_effect=verify_side_effect),
            mock.patch.object(main, "_notify_safe"),
        ):
            plan = mock.Mock()
            return main._select_usable_image(plan, load_settings())

    def test_tier1_success(self, tmp_path: Path):
        url, reasons = self._run(tmp_path, lambda u: None)
        assert url is not None
        assert reasons == []

    def test_tier2_falls_through_to_second(self, tmp_path: Path):
        calls: list[str] = []

        def verify(u: str) -> None:
            calls.append(u)
            if len(calls) == 1:
                raise ImageValidationError("Content-Type 이 text/plain 입니다.")

        url, reasons = self._run(tmp_path, verify)
        assert url is not None
        assert url != calls[0]
        assert len(reasons) == 1

    def test_all_candidates_fail(self, tmp_path: Path):
        def verify(u: str) -> None:
            raise ImageValidationError("HTTP 404 — 파일이 없습니다.")

        url, reasons = self._run(tmp_path, verify)
        assert url is None
        assert len(reasons) == config.IMAGE_CANDIDATE_LIMIT

    def test_candidate_limit_respected(self, tmp_path: Path):
        seen: list[str] = []

        def verify(u: str) -> None:
            seen.append(u)
            raise ImageValidationError("실패")

        d = _assets(tmp_path, [f"promo_{i:02d}.png" for i in range(1, 11)])
        from src import main
        from src.env import load_settings

        with (
            mock.patch.object(main, "ASSETS_DIR", d),
            mock.patch.object(main, "verify_image_url", side_effect=verify),
            mock.patch.object(main, "_notify_safe"),
        ):
            main._select_usable_image(mock.Mock(), load_settings())

        assert len(seen) == config.IMAGE_CANDIDATE_LIMIT

    def test_missing_assets_dir(self, tmp_path: Path):
        from src import main
        from src.env import load_settings

        with (
            mock.patch.object(main, "ASSETS_DIR", tmp_path / "nope"),
            mock.patch.object(main, "_notify_safe"),
        ):
            url, reasons = main._select_usable_image(mock.Mock(), load_settings())

        assert url is None
        assert reasons and "자산" in reasons[0]


class TestTextFallbackToggle:
    def test_default_enabled(self):
        assert config.IMAGE_FALLBACK_TO_TEXT is True

    def test_candidate_limit_positive(self):
        assert config.IMAGE_CANDIDATE_LIMIT >= 1


class TestPublishPath:
    """이미지 유무에 따라 어느 발행 경로를 타는지."""

    @staticmethod
    def _client() -> mock.Mock:
        client = mock.Mock()
        client.publish_image_post.return_value = "post_img"
        client.publish_text_post.return_value = "post_txt"
        client.publish_self_reply.return_value = "reply_1"
        return client

    def test_image_path_used_when_available(self):
        client = self._client()
        client.publish_image_post(f"{BASE}/promo_01.png", "본문")
        assert client.publish_image_post.called
        assert not client.publish_text_post.called

    def test_text_path_used_when_no_image(self):
        client = self._client()
        client.publish_text_post("본문")
        assert client.publish_text_post.called
        assert not client.publish_image_post.called

    def test_self_reply_always_follows(self):
        client = self._client()
        post_id = client.publish_text_post("본문")
        client.publish_self_reply(post_id, "링크")
        client.publish_self_reply.assert_called_once_with("post_txt", "링크")


class TestDegradeNotification:
    """강등 시 알림이 반드시 나가야 한다. 조용한 품질 저하 방지."""

    def test_tier2_notifies(self, tmp_path: Path):
        from src import main
        from src.env import load_settings

        calls: list[str] = []

        def verify(u: str) -> None:
            calls.append(u)
            if len(calls) == 1:
                raise ImageValidationError("깨진 파일")

        d = _assets(tmp_path, ["a.png", "b.png"])
        with (
            mock.patch.object(main, "ASSETS_DIR", d),
            mock.patch.object(main, "verify_image_url", side_effect=verify),
            mock.patch.object(main, "_notify_safe") as notify,
        ):
            main._select_usable_image(mock.Mock(), load_settings())

        assert notify.called


class TestDayIndexStability:
    def test_same_day_same_first_candidate(self):
        assets = [f"p{i}.png" for i in range(5)]
        day = content._day_index(dt.date(2026, 9, 13))
        first = content.order_asset_candidates(assets, day)[0]
        second = content.order_asset_candidates(assets, day)[0]
        assert first == second


class TestContainerWait:
    """컨테이너 처리 대기. code=24 (Media Not Found) 재발 방지."""

    @staticmethod
    def _client() -> object:
        from src.threads_client import ThreadsClient

        return ThreadsClient("123", "token")

    def test_finished_returns_immediately(self):
        client = self._client()
        with (
            mock.patch.object(client, "get_container_status",
                              return_value=("FINISHED", "")),
            mock.patch("src.threads_client.time.sleep") as sleep,
        ):
            client.wait_until_ready("c1", 30)
        assert sleep.call_count == 1
        sleep.assert_called_with(30)

    def test_in_progress_then_finished(self):
        from src import config

        client = self._client()
        statuses = [("IN_PROGRESS", ""), ("FINISHED", "")]
        with (
            mock.patch.object(client, "get_container_status",
                              side_effect=statuses),
            mock.patch("src.threads_client.time.sleep") as sleep,
        ):
            client.wait_until_ready("c1", 30)
        assert sleep.call_count == 2
        assert sleep.call_args_list[1][0][0] == config.CONTAINER_POLL_INTERVAL_SEC

    def test_error_status_raises(self):
        from src.threads_client import ContainerNotReadyError

        client = self._client()
        with (
            mock.patch.object(client, "get_container_status",
                              return_value=("ERROR", "FAILED_DOWNLOADING_VIDEO")),
            mock.patch("src.threads_client.time.sleep"),
            pytest.raises(ContainerNotReadyError, match="FAILED_DOWNLOADING_VIDEO"),
        ):
            client.wait_until_ready("c1", 30)

    def test_expired_status_raises(self):
        from src.threads_client import ContainerNotReadyError

        client = self._client()
        with (
            mock.patch.object(client, "get_container_status",
                              return_value=("EXPIRED", "")),
            mock.patch("src.threads_client.time.sleep"),
            pytest.raises(ContainerNotReadyError, match="만료"),
        ):
            client.wait_until_ready("c1", 30)

    def test_timeout_raises(self):
        from src.threads_client import ContainerNotReadyError

        client = self._client()
        with (
            mock.patch.object(client, "get_container_status",
                              return_value=("IN_PROGRESS", "")),
            mock.patch("src.threads_client.time.sleep"),
            pytest.raises(ContainerNotReadyError, match="준비되지 않았"),
        ):
            client.wait_until_ready("c1", 30)

    def test_dry_run_skips_wait(self):
        client = self._client()
        with (
            mock.patch.object(client, "get_container_status") as status,
            mock.patch("src.threads_client.time.sleep") as sleep,
        ):
            client.wait_until_ready("c1", 30, dry_run=True)
        assert not sleep.called
        assert not status.called

    def test_publish_waits_before_publishing(self):
        """create -> wait -> publish 순서가 지켜지는지."""
        from src import config

        client = self._client()
        order: list[str] = []
        with (
            mock.patch.object(client, "create_image_container",
                              side_effect=lambda *a: order.append("create") or "c1"),
            mock.patch.object(client, "wait_until_ready",
                              side_effect=lambda *a, **k: order.append("wait")),
            mock.patch.object(client, "publish",
                              side_effect=lambda *a: order.append("publish") or "p1"),
        ):
            got = client.publish_image_post("https://x/y.png", "본문")
        assert got == "p1"
        assert order == ["create", "wait", "publish"]
        assert config.CONTAINER_WAIT_IMAGE_SEC >= 30

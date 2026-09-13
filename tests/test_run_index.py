"""인덱스 축 확장 테스트.

배경: 콘텐츠 선택이 날짜 하나만 축으로 쓰면 같은 날 두 번 발행할 때
      이미지·기둥·소재가 전부 같아진다. 이벤트 발행 도입 전 선결 항목이었다.

구현 중 곱셈(day * N + disc) 방식이 로테이션 길이와 배수 관계일 때
한 슬롯이 같은 기둥만 뽑는 결함이 실측으로 확인되어 덧셈으로 교체했다.
그 회귀를 막는 테스트다.
"""

from __future__ import annotations

import collections
import datetime as dt
import os
from pathlib import Path

import pytest

from src import ai_writer, config, content

DAY = dt.date(2026, 9, 14)


@pytest.fixture(autouse=True)
def _clean_env():
    saved = dict(os.environ)
    for key in ("SLOT", "EVENT_RUN"):
        os.environ.pop(key, None)
    os.environ.setdefault("YOUTUBE_URL", "https://www.youtube.com/@handle")
    os.environ.setdefault("X_URL", "https://x.com/handle")
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    d.mkdir()
    for i in range(1, 6):
        (d / f"promo_{i:02d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return d


BASE = "https://raw.githubusercontent.com/owner/repo/main/assets"


class TestRunIndex:
    def test_discriminator_shifts_index(self):
        assert content.run_index(DAY, 1) == content.run_index(DAY, 0) + 1

    def test_same_input_same_output(self):
        assert content.run_index(DAY, 2) == content.run_index(DAY, 2)

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="discriminator"):
            content.run_index(DAY, config.DISCRIMINATOR_MAX)
        with pytest.raises(ValueError):
            content.run_index(DAY, -1)

    def test_additive_not_multiplicative(self):
        """곱셈이면 로테이션 길이와 배수 관계일 때 나머지가 고정된다."""
        rotation_len = len(ai_writer.PILLAR_ROTATION)
        seen = {
            content.run_index(DAY + dt.timedelta(days=i), 0) % rotation_len
            for i in range(rotation_len)
        }
        assert len(seen) == rotation_len


class TestDiscriminatorResolution:
    @pytest.mark.parametrize(("slot", "expected"), [("A", 0), ("B", 1), ("C", 2)])
    def test_slot_mapping(self, slot: str, expected: int):
        os.environ["SLOT"] = slot
        assert content.discriminator_from_env() == expected

    def test_event_run_wins(self):
        os.environ.update(SLOT="A", EVENT_RUN="true")
        assert content.discriminator_from_env() == config.DISCRIMINATOR_EVENT

    def test_unset_defaults_to_zero(self):
        assert content.discriminator_from_env() == 0

    def test_manual_slot_is_zero(self):
        os.environ["SLOT"] = "MANUAL"
        assert content.discriminator_from_env() == 0

    def test_unknown_slot_is_zero(self):
        os.environ["SLOT"] = "ZZZ"
        assert content.discriminator_from_env() == 0


class TestSameDayDistinctness:
    """같은 날 여러 번 발행해도 서로 달라야 한다."""

    def _plan(self, assets: Path, **env):
        for key in ("SLOT", "EVENT_RUN"):
            os.environ.pop(key, None)
        os.environ.update(env)
        return content.build_plan(DAY, assets, BASE)

    def test_slots_differ(self, assets: Path):
        plans = [self._plan(assets, SLOT=s) for s in ("A", "B", "C")]
        assert len({p.pillar for p in plans}) == 3
        assert len({p.image_url for p in plans}) == 3
        assert len({p.seed for p in plans}) == 3

    def test_event_differs_from_regular(self, assets: Path):
        regular = self._plan(assets, SLOT="A")
        event = self._plan(assets, EVENT_RUN="true")
        assert regular.image_url != event.image_url

    def test_idempotent_within_slot(self, assets: Path):
        runs = [self._plan(assets, SLOT="B") for _ in range(3)]
        keys = {(r.pillar, r.image_url, r.seed) for r in runs}
        assert len(keys) == 1


class TestRotationHealth:
    """한 슬롯이 같은 기둥만 뽑으면 안 된다. 실제로 났던 사고다."""

    def test_single_slot_sees_all_pillars(self, assets: Path):
        os.environ["SLOT"] = "A"
        counts = collections.Counter(
            content.build_plan(DAY + dt.timedelta(days=i), assets, BASE).pillar
            for i in range(56)
        )
        assert set(counts) == set(ai_writer.PILLARS)

    def test_promo_share_within_cap(self, assets: Path):
        os.environ["SLOT"] = "A"
        counts = collections.Counter(
            content.build_plan(DAY + dt.timedelta(days=i), assets, BASE).pillar
            for i in range(80)
        )
        total = sum(counts.values())
        assert counts["PROMO"] / total <= 0.3

    def test_assets_cycle(self, assets: Path):
        os.environ["SLOT"] = "A"
        urls = {
            content.build_plan(DAY + dt.timedelta(days=i), assets, BASE).image_url
            for i in range(5)
        }
        assert len(urls) == 5

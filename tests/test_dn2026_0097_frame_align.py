"""DN2026_0097 — 숏폼 비트 길이 프레임 정렬 회귀 테스트.

11-03 run #38(ubuntu-24.04, ffmpeg 6.1.1) '프레임레이트 29.83 ≠ 30', 11-05 run #40 '29.92 ≠ 30'.
원인: plan_timing 비트 길이가 1/30초 배수가 아니어서 concat -c copy 결과의 r_frame_rate 가 30/1 이 아님.
"""

from __future__ import annotations

import random
import shutil
import subprocess

import pytest

from src import config
from src.video import renderer, validator

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

# 운영 로그 '타이밍 총 … 비트 […]' 실측값(build job). (공통 낭독 배율, 비트 길이)
OPS_TIMINGS = {
    "run38_20261103": (1.122, [2.011, 7.431, 7.181, 7.93, 6.932, 7.466, 6.611, 6.825, 5.113]),
    "run40_20261105": (1.073, [2.611, 7.159, 6.973, 7.122, 6.414, 8.017, 6.936, 7.569, 4.699]),
}


def _narration_for(tempo: float, beats: list[float]) -> list[float]:
    """운영 비트 길이를 만든 낭독 길이(원속). 비트 = 낭독/배율 + 여백(SEG_PAD_SEC)."""
    return [(b - renderer.SEG_PAD_SEC) * tempo for b in beats]


def _is_frame_multiple(sec: float) -> bool:
    # Timing 은 소수 3자리로 반올림되므로 프레임 수 기준 오차 0.03 프레임(1ms) 이내면 경계 위
    return abs(sec * config.VIDEO_FPS - round(sec * config.VIDEO_FPS)) < 0.03


class TestFrameAlignedTiming:
    def test_random_narrations_are_frame_aligned(self):
        rng = random.Random(97)
        checked = 0
        for _ in range(500):
            hook = rng.uniform(1.0, 4.5)
            body = [rng.uniform(3.5, 7.5) for _ in range(8)]
            try:
                t = renderer.plan_timing([hook] + body)
            except renderer.RenderLengthError:
                continue
            checked += 1
            assert all(_is_frame_multiple(d) for d in t.durations), t.durations
            # 설계 목표 범위 유지(반올림으로 TARGET_MIN 아래로 내려가지 않음 — 1차 구현 회귀 2건의 원인)
            assert renderer.TARGET_MIN_SEC - 1e-3 <= t.total <= renderer.TARGET_MAX_SEC + 1e-3, t.total
            assert renderer.HOOK_MIN_SEC <= t.durations[0] <= renderer.HOOK_MAX_SEC
            # 본문 비트는 낭독(배율 적용) 길이보다 짧아지지 않는다 — 낭독이 잘리지 않음
            assert all(t.durations[i] + 1e-3 >= body[i - 1] / t.tempo for i in range(1, 9))
        assert checked > 300

    def test_over_limit_still_raises_for_script_regeneration(self):
        """반올림 전에 60초를 넘던 입력은 수정 후에도 RenderLengthError(대본 재생성 경로) — 동작 불변."""
        rng = random.Random(970)
        raised = 0
        for _ in range(3000):
            nar = [rng.uniform(0.5, 9.0) for _ in range(rng.randint(2, 12))]
            hook_index = rng.randrange(len(nar))
            try:
                t = renderer.plan_timing(nar, hook_index)
            except renderer.RenderLengthError:
                raised += 1
                continue
            assert all(_is_frame_multiple(d) for d in t.durations)
            assert t.total <= config.VIDEO_MAX_SEC
        assert raised > 0

    def test_hook_not_first_frame_aligned(self):
        t = renderer.plan_timing([5.0, 5.2, 2.9, 5.4, 5.1, 5.3, 5.0, 5.2, 4.9], hook_index=2)
        assert all(_is_frame_multiple(d) for d in t.durations)
        assert renderer.TARGET_MIN_SEC - 1e-3 <= t.total <= renderer.TARGET_MAX_SEC + 1e-3

    @pytest.mark.parametrize("name", sorted(OPS_TIMINGS))
    def test_ops_timings_frame_aligned(self, name):
        tempo, beats = OPS_TIMINGS[name]
        t = renderer.plan_timing(_narration_for(tempo, beats))
        assert all(_is_frame_multiple(d) for d in t.durations)
        assert all(abs(a - b) <= 0.5 / config.VIDEO_FPS + 1e-3 for a, b in zip(t.durations, beats, strict=True))


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
class TestFrameAlignedRenderE2E:
    @pytest.mark.parametrize("name", sorted(OPS_TIMINGS))
    def test_ops_timing_render_passes_validator(self, name, tmp_path):
        tempo, beats = OPS_TIMINGS[name]
        img = tmp_path / "img.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x336699:s=1024x1792",
                        "-frames:v", "1", str(img)], check=True)
        scenes = []
        for i, sec in enumerate(_narration_for(tempo, beats)):
            wav = tmp_path / f"n{i}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            f"sine=frequency={300 + i * 30}:sample_rate=24000:duration={sec:.4f}",
                            "-ac", "1", str(wav)], check=True)
            scenes.append(renderer.SceneInput(img, wav, "시장에 경고등이 켜졌다", i == 0, None))
        out = tmp_path / "out" / "video.mp4"
        out.parent.mkdir()
        renderer.render(scenes, None, out, rng=random.Random(1), character=config.CHARACTER_GOC)
        assert validator.check(out) == []

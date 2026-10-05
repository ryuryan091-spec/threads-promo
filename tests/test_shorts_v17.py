"""v1.7.0 숏폼(Facebook 릴스 · Threads 동영상) 테스트 (DESIGN_V17_SHORTS.md).

conftest 의 legacy 프로필을 적용하지 않는다(SAFETY_MODULES). 값은 cfg 픽스처로 명시한다.
네트워크는 쓰지 않는다. 렌더 E2E 는 실제 ffmpeg 로 60초 영상을 만들고 ffprobe 로 검사한다.

  A. shorts_plan — 램프 · 계획 · 포맷 · 게시 시각
  B. 훅 · 대본 검증 · 린트
  C. 렌더 타이밍 · 실제 렌더 E2E · 규격 검사
  D. face_client — 3단계 게시 · 오류 분류 · 회로 차단
  E. threads_client 동영상 컨테이너
  F. 러너 — build / publish
  G. 구성 — 워크플로 · verify_repo · redact · safety
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import random
import shutil
import subprocess
import sys
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import yaml

from src import config, content, face_client, mood_source, safety, shorts_plan
from src.threads_client import ThreadsClient
from src.video import hooks, renderer, script_writer, validator

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
sys.path.insert(0, str(ROOT / "scripts"))
KST = ZoneInfo("Asia/Seoul")
DAY = dt.date(2026, 10, 5)   # 월요일

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def cfg(monkeypatch):
    """안전 기본값에서 시작해 테스트가 필요한 값만 바꾼다."""
    base = {
        "AUTOMATION_ENABLED": False, "WARMUP_UNTIL": "", "DAILY_POST_BUDGET": 2,
        "SHORTS_BUILD_ENABLED": False, "SHORTS_THREADS_ENABLED": False, "FACE_ENABLED": False,
        "FACE_RAMP_START": "", "FACE_DAILY_MAX": 3, "SHORTS_WEEKLY_REST_DAYS": 0,
        "PUBLISH_WEEKLY_REST_DAYS": 0, "X_URL": "https://x.com/tiger18272",
    }
    for key, value in base.items():
        monkeypatch.setattr(config, key, value)

    def apply(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setattr(config, key, value)

    return apply


# ---------------------------------------------------------------------------
# A. shorts_plan
# ---------------------------------------------------------------------------


class TestRamp:
    def test_unset_or_invalid_is_zero(self, cfg):
        assert shorts_plan.face_daily_target(DAY) == 0
        cfg(FACE_RAMP_START="2026/10/01")
        assert shorts_plan.face_daily_target(DAY) == 0
        cfg(FACE_RAMP_START="2026-02-30")
        assert shorts_plan.face_daily_target(DAY) == 0

    def test_before_start_is_zero(self, cfg):
        cfg(FACE_RAMP_START="2026-10-06")
        assert shorts_plan.face_daily_target(DAY) == 0

    @pytest.mark.parametrize(("days", "expected"), [
        (0, 1), (13, 1), (14, 2), (41, 2), (42, 3), (400, 3),
    ])
    def test_steps(self, cfg, days, expected):
        cfg(FACE_RAMP_START=(DAY - dt.timedelta(days=days)).isoformat())
        assert shorts_plan.face_daily_target(DAY) == expected

    def test_cap(self, cfg):
        cfg(FACE_RAMP_START="2025-01-01", FACE_DAILY_MAX=2)
        assert shorts_plan.face_daily_target(DAY) == 2
        cfg(FACE_DAILY_MAX=0)
        assert shorts_plan.face_daily_target(DAY) == 0
        cfg(FACE_DAILY_MAX=99)
        assert shorts_plan.face_daily_target(DAY) == 3   # 포맷 수 상한


class TestDailyPlan:
    def test_build_disabled(self, cfg):
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True, FACE_RAMP_START="2025-01-01")
        assert shorts_plan.daily_plan(DAY) == []

    def test_kill_switch_means_nothing_to_post(self, cfg):
        cfg(SHORTS_BUILD_ENABLED=True, FACE_ENABLED=True, FACE_RAMP_START="2025-01-01",
            SHORTS_THREADS_ENABLED=True)
        assert shorts_plan.daily_plan(DAY) == []

    def test_face_three_threads_first(self, cfg):
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, FACE_ENABLED=True,
            FACE_RAMP_START="2025-01-01", SHORTS_THREADS_ENABLED=True)
        plan = shorts_plan.daily_plan(DAY)
        assert [p.content_id for p in plan] == ["sv-20261005-1", "sv-20261005-2", "sv-20261005-3"]
        assert plan[0].channels == ("face", "threads")
        assert all(p.channels == ("face",) for p in plan[1:])
        assert plan[0].fmt == "F1"
        assert sorted(p.fmt for p in plan) == ["F1", "F2", "F3"]

    def test_threads_only(self, cfg):
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, SHORTS_THREADS_ENABLED=True)
        plan = shorts_plan.daily_plan(DAY)
        assert len(plan) == 1 and plan[0].channels == ("threads",)

    def test_threads_blocked_by_warmup(self, cfg):
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, SHORTS_THREADS_ENABLED=True,
            WARMUP_UNTIL="2026-10-31")
        assert shorts_plan.daily_plan(DAY) == []

    def test_rest_day(self, cfg):
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, SHORTS_THREADS_ENABLED=True,
            SHORTS_WEEKLY_REST_DAYS=7)
        assert shorts_plan.daily_plan(DAY) == []

    def test_idempotent(self, cfg):
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, FACE_ENABLED=True,
            FACE_RAMP_START="2025-01-01")
        assert shorts_plan.daily_plan(DAY) == shorts_plan.daily_plan(DAY)

    def test_content_date(self):
        assert shorts_plan.content_date("sv-20261005-2") == DAY
        assert shorts_plan.content_date("sv-2026105-2") is None
        assert shorts_plan.content_date("x") is None


class TestSchedule:
    def _now(self, hh, mm=0):
        return dt.datetime.combine(DAY, dt.time(hh, mm), tzinfo=KST)

    def test_before_window_waits_for_start(self):
        out = shorts_plan.publish_schedule(self._now(8, 30), 1, rng=random.Random(1))
        # DN2026_0004 : 지연 5~40분 → 1~10분
        assert out and out[0] >= self._now(10, 1) and out[0] <= self._now(10, 10)

    def test_gaps_and_budget(self):
        out = shorts_plan.publish_schedule(self._now(11), 3, rng=random.Random(2))
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(out, out[1:], strict=False)]
        assert len(out) == 3
        assert all(config.SHORTS_GAP_FLOOR_MIN <= g <= 200 for g in gaps)
        assert all(t <= self._now(11) + dt.timedelta(minutes=config.SHORTS_JOB_BUDGET_MIN) for t in out)

    def test_after_window_posts_nothing(self):
        # DN2026_0004 : 지연이 1~10분이 되어 21:50 은 시간대 안 → 시간대 종료 후(22:01)로 검증
        assert shorts_plan.publish_schedule(self._now(22, 1), 3, rng=random.Random(3)) == []

    def test_late_drops_tail(self):
        # DN2026_0004 : 편간 1~10분 — 시간대 끝 직전(21:50)이면 끝(22:00)을 넘는 편은 뺀다
        for seed in range(200):
            out = shorts_plan.publish_schedule(self._now(21, 50), 3, rng=random.Random(seed))
            assert 1 <= len(out) <= 3
            assert all(t <= self._now(22) for t in out)

    def test_early_approval_fits_three_by_compressing(self):
        out = shorts_plan.publish_schedule(self._now(9, 13), 3, rng=random.Random(5))
        assert len(out) == 3
        assert out[-1] <= self._now(9, 13) + dt.timedelta(minutes=config.SHORTS_JOB_BUDGET_MIN)

    def test_random_not_fixed(self):
        a = shorts_plan.publish_schedule(self._now(11), 3, rng=random.Random(1))
        b = shorts_plan.publish_schedule(self._now(11), 3, rng=random.Random(2))
        assert a != b


# ---------------------------------------------------------------------------
# B. 훅 · 대본
# ---------------------------------------------------------------------------


class TestHooks:
    def test_select_avoids_used(self):
        first = hooks.select_hook_type("F1", "Debt Titan")
        assert first == hooks.HOOK_B
        assert hooks.select_hook_type("F1", "Debt Titan", {hooks.HOOK_B}) == hooks.HOOK_C
        assert hooks.select_hook_type("F2", "Debt Titan") == hooks.HOOK_C
        assert hooks.select_hook_type("F3", "Bull Brute") == hooks.HOOK_D

    def test_issue(self):
        # v1.8.3 빠른 훅: 8~14자(이전 12~18자)
        assert hooks.hook_issue("짧다", hooks.HOOK_A)
        assert hooks.hook_issue("가" * 15, hooks.HOOK_A)
        assert hooks.hook_issue("가" * 14, hooks.HOOK_A) is None
        assert hooks.hook_issue("가" * 8, hooks.HOOK_A) is None
        assert hooks.hook_issue("방어선이 무너진다", hooks.HOOK_D)
        assert hooks.hook_issue("[긴급] 방어선 위기", hooks.HOOK_D) is None

    def test_specs_complete(self):
        for t in hooks.HOOK_TYPES:
            for key in ("name", "guide", "example", "tts_tone", "sfx"):
                assert hooks.HOOK_SPECS[t][key]
            # 예시 문장도 발행 규칙을 지킨다
            content.lint_shorts(hooks.HOOK_SPECS[t]["example"], max_len=40, label="예시")


def _valid_raw(hook="방어선이 뚫리기 직전이다"):
    body = [
        "금리라는 무게가 오늘도 다시 시장의 어깨를 천천히 누르기 시작했습니다",
        "뎁트타이탄의 거대한 사슬이 도시 위로 소리 없이 길게 내려오고 있습니다",
        "EDT 는 묵직한 체인소를 고쳐 쥐고 흔들리는 숨을 조용히 고르고 있습니다",
        "지금 중요한 건 큰 소리가 아니라 끝까지 버티는 자세라고 그는 말합니다",
        "물가와 고용 이야기가 같은 날 한꺼번에 겹치면서 시장의 소음이 커집니다",
        "흔들릴수록 지금 무엇을 보고 있는지 차분히 적어 두는 편이 낫다고 그는 본다",
        "마침내 사슬 하나가 끊어지면서 그 틈 사이로 빛이 조금씩 새어 나옵니다",
    ]
    return {
        "hook": hook,
        "body": body,
        "closing": "오늘 싸움은 끝나지 않았습니다 내일 다시 봅니다",
        "image_prompts": ["tiger hero facing chains"] * config.SHORTS_IMAGE_COUNT,
        "post_caption": "금리 이야기로 시끄러운 하루였습니다. 소음보다 자세를 보자는 이야기를 EDT 와 함께 정리했습니다.",
    }


class TestScript:
    def test_valid_passes(self):
        assert script_writer.validate(_valid_raw(), hooks.HOOK_B) == []

    def test_digit_rejected(self):
        raw = _valid_raw()
        raw["body"][0] = "금리가 4퍼센트를 넘으며 시장 어깨를 누르고 있습니다"
        issues = script_writer.validate(raw, hooks.HOOK_B)
        assert any("숫자" in i for i in issues)

    def test_advice_and_link_rejected(self):
        raw = _valid_raw()
        raw["post_caption"] = "지금이 매수 기회라는 이야기를 정리했습니다 자세한 건 https://x.com 에서"
        issues = script_writer.validate(raw, hooks.HOOK_B)
        assert any("정책 위반" in i for i in issues)

    def test_counts_and_lengths(self):
        raw = _valid_raw()
        raw["body"] = raw["body"][:5]
        raw["image_prompts"] = ["a"] * 3
        issues = script_writer.validate(raw, hooks.HOOK_B)
        assert any("본문 5개" in i for i in issues)
        assert any("이미지 프롬프트" in i for i in issues)

    def test_duplicate_caption_rejected(self):
        raw = _valid_raw()
        issues = script_writer.validate(raw, hooks.HOOK_B, {raw["post_caption"]})
        assert "캡션이 같은 날 다른 편과 같음" in issues

    def test_villain_mapping(self):
        assert script_writer.select_villain(mood_source.Mood("rss", ("금리",), "안도")) == "Debt Titan"
        assert script_writer.select_villain(mood_source.Mood("rss", ("유가",), "긴장")) == "Chaos Reaper"
        assert script_writer.select_villain(mood_source.Mood("rss", ("반도체",), "낙관")) == "Bull Brute"
        assert script_writer.select_villain(mood_source.Mood("none")) == "Debt Titan"

    def test_write_script_retries_then_succeeds(self, monkeypatch):
        bad = _valid_raw(hook="짧다")
        calls = []

        def fake(api_key, prompt, character="EDT"):
            calls.append(prompt)
            return bad if len(calls) == 1 else _valid_raw()

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        s = script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1",
                                       mood=mood_source.Mood("rss", ("금리",), "관망"))
        assert len(calls) == 2 and "위반 사항" in calls[1]
        assert len(s.beats) == script_writer.BEAT_COUNT
        assert s.beats[0].is_hook and s.beats[0].sfx == "hook_b"
        assert s.caption.endswith(config.SHORTS_AI_NOTICE)

    def test_write_script_exhausts(self, monkeypatch):
        monkeypatch.setattr(script_writer, "_call_claude", lambda k, p, c="EDT": _valid_raw(hook="짧다"))
        with pytest.raises(script_writer.ScriptError):
            script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1",
                                       mood=mood_source.Mood("none"))

    def test_beat_slots(self):
        assert len(script_writer.BEAT_IMAGE_SLOT) == script_writer.BEAT_COUNT
        assert set(script_writer.BEAT_IMAGE_SLOT) == set(range(config.SHORTS_IMAGE_COUNT))


class TestLint:
    def test_shorts_allows_edt_chat_does_not(self):
        content.lint_shorts("EDT 가 금리 이야기를 꺼냅니다", max_len=60, label="본문")
        with pytest.raises(content.ContentPolicyError):
            content.lint_chat("EDT 가 금리 이야기를 꺼냅니다")

    def test_shorts_rejects_ticker(self):
        with pytest.raises(content.ContentPolicyError):
            content.lint_shorts("NVDA 이야기가 많습니다", max_len=60, label="본문")

    def test_ai_notice_passes_lint(self):
        content.lint_shorts(config.SHORTS_AI_NOTICE, max_len=60, label="고지")


# ---------------------------------------------------------------------------
# C. 렌더
# ---------------------------------------------------------------------------


class TestTiming:
    def test_short_narration_stretched(self):
        t = renderer.plan_timing([2.5] + [4.0] * 8)
        assert renderer.TARGET_MIN_SEC <= t.total <= config.VIDEO_MAX_SEC
        # v1.8.3: 훅 최소 2.0초 — 2.5초 낭독 + 여백이 그보다 길면 그 길이를 쓴다
        assert t.durations[0] == max(renderer.HOOK_MIN_SEC, 2.5 + renderer.SEG_PAD_SEC) and t.tempo == 1.0
        assert renderer.plan_timing([1.2] + [4.0] * 8).durations[0] == renderer.HOOK_MIN_SEC

    def test_long_narration_speeds_up(self):
        t = renderer.plan_timing([3.0] + [7.0] * 8)
        assert t.tempo > 1.0 and t.total <= renderer.TARGET_MAX_SEC + 0.01

    def test_too_long_raises(self):
        with pytest.raises(renderer.RenderLengthError):
            renderer.plan_timing([3.0] + [9.0] * 8)

    def test_hook_too_long(self):
        with pytest.raises(renderer.RenderLengthError):
            # v1.8.4: 훅은 최대 1.25배 추가 가속되므로 상한(8초)을 넘기려면 더 긴 훅이 필요하다
            renderer.plan_timing([11.0] + [4.0] * 8)

    def test_wrap(self):
        out = renderer.wrap_korean("금리라는 무게가 다시 시장 어깨를 누르고 있습니다", 13)
        assert all(len(line) <= 13 for line in out.split("\n"))
        assert out.replace("\n", " ") == "금리라는 무게가 다시 시장 어깨를 누르고 있습니다"

    def test_handle(self):
        assert renderer.handle_from_x_url("https://x.com/tiger18272") == "@tiger18272"
        assert renderer.handle_from_x_url("") == ""


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
class TestRenderE2E:
    def test_render_and_validate(self, tmp_path, cfg):
        imgs = []
        for i in range(config.SHORTS_IMAGE_COUNT):
            p = tmp_path / f"img{i}.png"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            f"color=c=0x{i*40:02x}3366:s=1024x1536", "-frames:v", "1", str(p)], check=True)
            imgs.append(p)
        auds = []
        for i in range(9):
            p = tmp_path / f"n{i}.wav"
            d = 2.6 if i == 0 else 5.2
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            f"sine=frequency={300 + i * 30}:sample_rate=24000:duration={d}",
                            "-ac", "1", str(p)], check=True)
            auds.append(p)
        sfx = tmp_path / "hook_b.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=900:sample_rate=44100:duration=0.8", str(sfx)], check=True)
        raw = _valid_raw()
        caps = [raw["hook"], *raw["body"], raw["closing"]]
        scenes = [renderer.SceneInput(imgs[script_writer.BEAT_IMAGE_SLOT[i]], auds[i], caps[i],
                                      i == 0, sfx if i == 0 else None) for i in range(9)]
        out = tmp_path / "out" / "video.mp4"
        out.parent.mkdir()
        timing = renderer.render(scenes, "Debt Titan", out, rng=random.Random(1))
        assert validator.check(out) == []
        assert config.VIDEO_MIN_SEC <= timing.total <= config.VIDEO_MAX_SEC
        assert out.stat().st_size < 50 * 1024 * 1024   # 텔레그램 미리보기 상한 안
        atoms = validator.top_level_atoms(out)
        assert atoms.index("moov") < atoms.index("mdat")

    def test_validator_rejects_bad_spec(self, tmp_path):
        bad = tmp_path / "bad.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=3",
                        "-f", "lavfi", "-i", "sine=duration=3:sample_rate=44100", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(bad)], check=True)
        issues = validator.check(bad)
        assert any("해상도" in i for i in issues)
        assert any("샘플레이트" in i for i in issues)
        assert any("길이" in i for i in issues)
        assert any("faststart" in i for i in issues)


# ---------------------------------------------------------------------------
# D. face_client
# ---------------------------------------------------------------------------


def _resp(status=200, payload=None, text=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = text if text is not None else json.dumps(payload or {})
    return r


class TestFaceClient:
    def test_publish_three_steps(self, tmp_path, monkeypatch):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x" * 1234)
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url, kw))
            if "rupload" in url:
                return _resp(payload={"success": True})
            if kw.get("json", {}).get("upload_phase") == "start":
                return _resp(payload={"video_id": "777", "upload_url": "u"})
            if "fields" in (kw.get("params") or {}):
                return _resp(payload={"status": {"video_status": "ready",
                                                 "uploading_phase": {"status": "complete"},
                                                 "processing_phase": {"status": "complete"},
                                                 "publishing_phase": {"status": "complete"}}})
            return _resp(payload={"success": True})

        monkeypatch.setattr(face_client.requests, "request", fake)
        client = face_client.FaceClient("123", "EAA" + "t" * 30)
        vid, status = client.publish_reel(video, "설명")
        assert vid == "777" and status.published
        start, upload, finish, st = calls
        assert start[1].endswith("/123/video_reels")
        assert upload[1] == f"{config.FACE_RUPLOAD_BASE}/777"
        assert upload[2]["headers"]["file_size"] == "1234"
        assert upload[2]["headers"]["offset"] == "0"
        assert upload[2]["headers"]["Authorization"].startswith("OAuth ")
        assert finish[2]["params"]["upload_phase"] == "finish"
        assert finish[2]["params"]["video_state"] == "PUBLISHED"
        assert finish[2]["params"]["description"] == "설명"

    def test_auth_error_trips_circuit_no_retry(self, tmp_path, monkeypatch):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        req = mock.Mock(return_value=_resp(400, {"error": {"code": 190}}))
        monkeypatch.setattr(face_client.requests, "request", req)
        client = face_client.FaceClient("123", "tok" * 5)
        with pytest.raises(face_client.FaceApiError) as ei:
            client.publish_reel(video, "d")
        assert ei.value.is_auth_error and safety.tripped() is not None
        assert req.call_count == 1
        with pytest.raises(face_client.FaceApiError):
            client.publish_reel(video, "d")   # 차단기 열림 — 쓰기 없이 같은 오류
        assert req.call_count == 1

    def test_write_not_retried_on_5xx(self, tmp_path, monkeypatch):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        req = mock.Mock(return_value=_resp(500, {"error": {"code": 2}}))
        monkeypatch.setattr(face_client.requests, "request", req)
        monkeypatch.setattr(face_client.time, "sleep", lambda s: None)
        with pytest.raises(face_client.FaceApiError):
            face_client.FaceClient("1", "tok" * 5).publish_reel(video, "d")
        assert req.call_count == 1

    def test_read_retried_on_5xx(self, monkeypatch):
        seq = [_resp(500, {}), _resp(200, {"data": [{"id": "1", "description": "a"}]})]
        monkeypatch.setattr(face_client.requests, "request", mock.Mock(side_effect=seq))
        monkeypatch.setattr(face_client.time, "sleep", lambda s: None)
        assert face_client.FaceClient("1", "tok" * 5).recent_descriptions() == ["a"]

    def test_status_error(self, monkeypatch):
        payload = {"status": {"video_status": "error", "processing_phase": {"status": "error",
                                                                              "error": {"message": "bad"}}}}
        monkeypatch.setattr(face_client.requests, "request", mock.Mock(return_value=_resp(200, payload)))
        st = face_client.FaceClient("1", "tok" * 5).status("9")
        assert st.failed and st.error == "bad"

    def test_requires_credentials(self):
        with pytest.raises(ValueError):
            face_client.FaceClient("", "")


# ---------------------------------------------------------------------------
# E. threads_client 동영상
# ---------------------------------------------------------------------------


class TestThreadsVideo:
    def test_video_container_fields(self, monkeypatch):
        sent = {}

        def fake(method, url, *, params):
            sent.update(params)
            return {"id": "c1"}

        monkeypatch.setattr("src.threads_client._request", fake)
        assert ThreadsClient("1", "t").create_video_container("https://u/v.mp4", "글") == "c1"
        assert sent["media_type"] == "VIDEO" and sent["video_url"] == "https://u/v.mp4"

    def test_wait_uses_video_max(self, monkeypatch):
        client = ThreadsClient("1", "t")
        monkeypatch.setattr("src.threads_client.time.sleep", lambda s: None)
        monkeypatch.setattr(client, "get_container_status", lambda cid: ("IN_PROGRESS", ""))
        from src.threads_client import ContainerNotReadyError
        with pytest.raises(ContainerNotReadyError) as ei:
            client.wait_until_ready("c", 60, max_wait_sec=600)
        assert "600초" in str(ei.value)
        with pytest.raises(ContainerNotReadyError) as ei:
            client.wait_until_ready("c", 30)
        assert f"{config.CONTAINER_POLL_MAX_SEC}초" in str(ei.value)


# ---------------------------------------------------------------------------
# F. 러너
# ---------------------------------------------------------------------------


def _manifest(tmp_path, ids, channels=(("face", "threads"), ("face",))):
    items = []
    for cid, ch in zip(ids, channels, strict=False):
        (tmp_path / cid).mkdir(parents=True, exist_ok=True)
        (tmp_path / cid / "video.mp4").write_bytes(b"v")
        items.append({"content_id": cid, "fmt": "F1", "channels": list(ch),
                      "caption": f"캡션 {cid}\n\n{config.SHORTS_AI_NOTICE}", "video": f"{cid}/video.mp4"})
    (tmp_path / "manifest.json").write_text(json.dumps({"items": items}, ensure_ascii=False), "utf-8")


class TestPublishRunner:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_publish as rp
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        monkeypatch.setenv("FACE_PAGE_ID", "123")
        monkeypatch.setenv("FACE_PAGE_TOKEN", "EAA" + "t" * 30)
        monkeypatch.setenv("THREADS_LONG_LIVED_TOKEN", "THAA" + "x" * 40)
        monkeypatch.setenv("THREADS_USER_ID", "1234567890123456")
        monkeypatch.setattr(rp, "_notify", lambda m: None)
        monkeypatch.setattr(rp, "_sleep_until", lambda t: None)
        fixed = dt.datetime.combine(dt.datetime.now(KST).date(), dt.time(11, 0), tzinfo=KST)

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz is None or tz == KST else fixed.astimezone(tz)

        monkeypatch.setattr(rp.dt, "datetime", _DT)
        return rp, tmp_path, fixed.date()

    def test_fresh_items_filters_old(self, env):
        rp, tmp, today = env
        stale = (today - dt.timedelta(days=1)).strftime("%Y%m%d")
        items = rp.fresh_items({"items": [{"content_id": f"sv-{stale}-1"},
                                          {"content_id": f"sv-{today:%Y%m%d}-1"}]}, today)
        assert [i["content_id"] for i in items] == [f"sv-{today:%Y%m%d}-1"]

    def test_dry_run_no_clients(self, env, monkeypatch, cfg):
        rp, tmp, today = env
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True, SHORTS_THREADS_ENABLED=True)
        _manifest(tmp, [f"sv-{today:%Y%m%d}-1"])
        monkeypatch.setenv("DRY_RUN", "true")
        boom = mock.Mock(side_effect=AssertionError("클라이언트 생성 금지"))
        monkeypatch.setattr(rp, "FaceClient", boom)
        monkeypatch.setattr(rp, "_threads_client", boom)
        assert rp.run() == 0

    def test_live_posts_face_and_threads(self, env, monkeypatch, cfg):
        rp, tmp, today = env
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True, SHORTS_THREADS_ENABLED=True)
        ids = [f"sv-{today:%Y%m%d}-1", f"sv-{today:%Y%m%d}-2"]
        _manifest(tmp, ids)
        monkeypatch.setenv("DRY_RUN", "false")
        face = mock.Mock()
        face.recent_descriptions.return_value = []
        face.publish_reel.return_value = ("v1", face_client.ReelStatus("ready", "complete", "complete",
                                                                         "complete", ""))
        monkeypatch.setattr(rp, "FaceClient", mock.Mock(return_value=face))
        threads = mock.Mock()
        threads.get_my_posts.return_value = []
        threads.publish_video_post.return_value = "p1"
        monkeypatch.setattr(rp, "_threads_client", lambda: threads)
        monkeypatch.setattr(rp.media_host, "publish_file", lambda v, n: f"https://raw/{n}")
        monkeypatch.setattr(rp.media_host, "verify", lambda u: "video/mp4")
        assert rp.run() == 0
        assert face.publish_reel.call_count == 2
        threads.publish_video_post.assert_called_once()
        assert threads.publish_video_post.call_args.args[0].endswith(f"{ids[0]}.mp4")

    def test_duplicate_description_skipped(self, env, monkeypatch, cfg):
        rp, tmp, today = env
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True)
        cid = f"sv-{today:%Y%m%d}-1"
        _manifest(tmp, [cid], channels=(("face",),))
        monkeypatch.setenv("DRY_RUN", "false")
        face = mock.Mock()
        face.recent_descriptions.return_value = [f"캡션 {cid}\n\n{config.SHORTS_AI_NOTICE}"]
        monkeypatch.setattr(rp, "FaceClient", mock.Mock(return_value=face))
        assert rp.run() == 0
        face.publish_reel.assert_not_called()

    def test_threads_budget_blocks(self, env, monkeypatch, cfg):
        rp, tmp, today = env
        cfg(AUTOMATION_ENABLED=True, SHORTS_THREADS_ENABLED=True, DAILY_POST_BUDGET=1)
        cid = f"sv-{today:%Y%m%d}-1"
        _manifest(tmp, [cid], channels=(("threads",),))
        monkeypatch.setenv("DRY_RUN", "false")
        threads = mock.Mock()
        threads.get_my_posts.return_value = [{"id": "x", "timestamp": dt.datetime.now(dt.UTC).strftime(
            "%Y-%m-%dT%H:%M:%S+0000"), "media_type": "TEXT_POST"}]
        monkeypatch.setattr(rp, "_threads_client", lambda: threads)
        assert rp.run() == 0
        threads.publish_video_post.assert_not_called()

    def test_kill_switch_blocks_face(self, env, monkeypatch, cfg):
        rp, tmp, today = env
        cfg(AUTOMATION_ENABLED=False, FACE_ENABLED=True)
        _manifest(tmp, [f"sv-{today:%Y%m%d}-1"], channels=(("face",),))
        monkeypatch.setenv("DRY_RUN", "false")
        boom = mock.Mock(side_effect=AssertionError("킬 스위치인데 클라이언트 생성"))
        monkeypatch.setattr(rp, "FaceClient", boom)
        assert rp.run() == 0

    def test_fatal_exit_7(self, env, monkeypatch, cfg):
        rp, tmp, today = env
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True)
        _manifest(tmp, [f"sv-{today:%Y%m%d}-1"], channels=(("face",),))
        monkeypatch.setenv("DRY_RUN", "false")
        face = mock.Mock()
        face.recent_descriptions.side_effect = face_client.FaceApiError(400, "x", code=190)
        monkeypatch.setattr(rp, "FaceClient", mock.Mock(return_value=face))
        assert rp.main() == safety.FATAL_EXIT_CODE


class TestBuildRunner:
    def test_nothing_to_build_writes_manifest(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_build as rb
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        assert rb.run() == 0
        assert json.loads((tmp_path / "manifest.json").read_text("utf-8"))["items"] == []

    def test_missing_secret(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_build as rb
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, SHORTS_THREADS_ENABLED=True)
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        monkeypatch.delenv("CLAUDE_AI_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_SUB_PAY_KEY", raising=False)
        monkeypatch.setattr(rb, "_notify", lambda m: None)
        assert rb.run() == 3

    def test_one_failure_isolated(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_build as rb
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, FACE_ENABLED=True,
            FACE_RAMP_START="2025-01-01")
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_AI_KEY", "k")
        monkeypatch.setenv("GEMINI_API_SUB_PAY_KEY", "g")
        monkeypatch.setattr(rb, "_notify", lambda m: None)
        monkeypatch.setattr(rb.notifier, "send_video", lambda *a, **k: True)
        monkeypatch.setattr(rb.mood_source, "collect", lambda *a, **k: mood_source.Mood("none"))

        def fake_build(item, mood, **kw):
            if item.index == 1:
                raise RuntimeError("이미지 실패")
            return {"content_id": item.content_id, "fmt": item.fmt, "channels": list(item.channels),
                    "caption": "c", "video": f"{item.content_id}/video.mp4", "duration": 57.0}

        monkeypatch.setattr(rb, "build_one", fake_build)
        assert rb.run() == 0
        m = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
        assert len(m["items"]) == 2 and len(m["failed"]) == 1


# ---------------------------------------------------------------------------
# G. 구성
# ---------------------------------------------------------------------------


class TestConfigAndWorkflow:
    def _wf(self):
        return yaml.safe_load((WF / "shorts.yml").read_text("utf-8"))

    def test_publish_runs_without_approval_gate_and_write(self):
        # DN2026_0002 : 승인 게이트(environment: meta-publish) 제거 — build 직후 자동 게시
        job = self._wf()["jobs"]["publish"]
        assert "environment" not in job
        assert job["permissions"]["contents"] == "write"
        assert job["needs"] == "build"
        assert "has_items" in job["if"]

    def test_build_installs_ffmpeg_and_font(self):
        body = (WF / "shorts.yml").read_text("utf-8")
        assert "ffmpeg fonts-noto-cjk" in body

    def test_cron_not_on_hour_or_half(self):
        on = self._wf().get(True) or self._wf().get("on")
        for entry in on["schedule"]:
            assert entry["cron"].split()[0] not in ("0", "30")

    def test_face_prefix_for_facebook_only_values(self):
        body = (WF / "shorts.yml").read_text("utf-8")
        for key in ("FACE_PAGE_ID", "FACE_PAGE_TOKEN", "FACE_ENABLED", "FACE_RAMP_START", "FACE_DAILY_MAX"):
            assert key in body

    def test_safety_defaults_off(self):
        d = config.SAFETY_VARIABLE_DEFAULTS
        assert d["SHORTS_BUILD_ENABLED"] == "false" and d["FACE_ENABLED"] == "false"
        assert d["SHORTS_THREADS_ENABLED"] == "false" and d["FACE_RAMP_START"] == ""

    def test_audio_48k(self):
        assert config.AUDIO_SAMPLE_RATE == 48_000

    def test_verify_repo_nested_check(self, tmp_path, monkeypatch):
        import verify_repo
        monkeypatch.setattr(verify_repo, "REPO_ROOT", tmp_path)
        assert verify_repo.check_nested_dirs() == 0
        (tmp_path / "src" / "src").mkdir(parents=True)
        assert verify_repo.check_nested_dirs() == 1

    def test_verify_repo_lists_shorts(self):
        import verify_repo
        assert ".github/workflows/shorts.yml" in verify_repo.REQUIRED_FILES
        assert "run_shorts_publish" in verify_repo.REQUIRED_MODULES
        assert "shorts.yml" in verify_repo.DRY_RUN_WORKFLOWS


class TestRedactAndSafety:
    def test_redact_new_tokens(self):
        from src.redact import redact
        out = redact("EAA" + "A" * 40 + " AIza" + "B" * 30 + " https://x?key=SECRETVALUE123")
        assert "AAAA" not in out and "BBBB" not in out and "SECRETVALUE123" not in out

    def test_shorts_blocked_in_warmup(self, cfg):
        cfg(AUTOMATION_ENABLED=True, SHORTS_THREADS_ENABLED=True, WARMUP_UNTIL="2026-10-31")
        assert safety.block_reason(safety.KIND_SHORTS, DAY)
        assert not safety.shorts_threads_allowed(DAY)

    def test_face_gate(self, cfg):
        assert "AUTOMATION_ENABLED" in safety.face_block_reason()
        cfg(AUTOMATION_ENABLED=True)
        assert "FACE_ENABLED" in safety.face_block_reason()
        cfg(FACE_ENABLED=True)
        assert safety.face_block_reason() is None

    def test_shorts_counts_as_non_regular_in_budget(self, cfg):
        cfg(AUTOMATION_ENABLED=True, DAILY_POST_BUDGET=2)
        now = dt.datetime.combine(DAY, dt.time(11, 0), tzinfo=KST)
        one = [{"id": "1", "timestamp": now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                "media_type": "TEXT_POST"}]
        with mock.patch.object(safety, "regular_reserved", return_value=now + dt.timedelta(hours=3)):
            assert safety.budget_block(safety.KIND_SHORTS, one, now)       # 정기 몫 예약 → 막음
        with mock.patch.object(safety, "regular_reserved", return_value=None):
            assert safety.budget_block(safety.KIND_SHORTS, one, now) is None


# ---------------------------------------------------------------------------
# H. 기존 Threads 파이프라인과의 상호작용 — 숏폼 동영상(VIDEO)이 계정에 섞여도 기존 판정이 바뀌지 않는다
# ---------------------------------------------------------------------------


def _post(at: dt.datetime, media_type: str, pid: str = "v") -> dict:
    return {"id": pid, "timestamp": at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000"),
            "media_type": media_type}


class TestVideoDoesNotLeakIntoThreadsPipelines:
    def _slot(self):
        # 정기 B 슬롯(12:26 KST) 판정 창 안 시각
        return dt.datetime.combine(DAY, dt.time(12, 40), tzinfo=KST)

    def test_is_shorts_post(self):
        from src import chat_plan
        assert chat_plan.is_shorts_post("VIDEO")
        assert not chat_plan.is_shorts_post("IMAGE") and not chat_plan.is_shorts_post("")

    def test_video_not_chat(self):
        from src import chat_plan
        assert not chat_plan.is_chat_post(dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=KST), "VIDEO")

    def test_restore_pillar_shorts_even_in_regular_window(self):
        from src import insights
        at = self._slot()
        assert insights.discriminator_from_timestamp(at) is not None
        assert insights.restore_pillar(at, "VIDEO") == insights.SHORTS
        assert insights.restore_pillar(at, "IMAGE") != insights.SHORTS   # 기존 동작 유지

    def test_regular_reservation_kept_after_video(self, cfg, monkeypatch):
        cfg(AUTOMATION_ENABLED=True)
        slot_at = self._slot().replace(minute=26)
        monkeypatch.setattr(safety, "regular_slot_at", lambda today: slot_at)
        now = slot_at + dt.timedelta(minutes=30)
        video_only = [_post(slot_at + dt.timedelta(minutes=10), "VIDEO")]
        image = [_post(slot_at + dt.timedelta(minutes=10), "IMAGE")]
        assert safety.regular_reserved(video_only, now) is not None   # 영상은 정기 완료가 아니다
        assert safety.regular_reserved(image, now) is None            # 기존 동작 유지

    def test_story_gap_ignores_video(self):
        from src import run_story
        at = self._slot()
        stamps = run_story._non_chat_stamps([_post(at, "VIDEO"), _post(at, "IMAGE", "i")])
        assert len(stamps) == 1

    def test_watchdog_freshness_ignores_video(self, monkeypatch):
        """run_watchdog 의 정기 신선도 입력에서 VIDEO 가 빠진다(실제 경로 실행)."""
        from src import run_watchdog
        captured = {}

        def fake_publish_finding(regular_posts, now, today):
            captured["regular"] = regular_posts
            raise RuntimeError("stop")   # 이후 경로는 이 테스트 범위 밖

        client = mock.Mock()
        client.get_my_posts.return_value = [
            _post(dt.datetime.now(KST) - dt.timedelta(hours=1), "VIDEO", "v"),
            _post(dt.datetime.now(KST) - dt.timedelta(hours=30), "IMAGE", "i"),
        ]
        monkeypatch.setattr(run_watchdog, "publish_finding", fake_publish_finding)
        monkeypatch.setattr(run_watchdog, "ThreadsClient", mock.Mock(return_value=client))
        monkeypatch.setattr(run_watchdog, "load_settings",
                            lambda: mock.Mock(threads_token="t", threads_user_id="1234567890123456"))
        try:
            run_watchdog.run()
        except Exception:  # noqa: BLE001 — fake_publish_finding 이 중단시킨다
            pass
        assert [p["id"] for p in captured["regular"]] == ["i"]

    def test_insights_report_lists_shorts_but_not_ranked(self):
        from src import insights
        stats = [insights.PostStat(post_id="1", posted_at=dt.datetime.now(dt.UTC), pillar=insights.SHORTS, replies=50),
                 insights.PostStat(post_id="2", posted_at=dt.datetime.now(dt.UTC), pillar="MARKET", replies=1)]
        rows = insights.aggregate(stats)
        assert any(r.pillar == insights.SHORTS and r.posts == 1 for r in rows)
        report = insights.render_report(DAY, insights.UserStat(followers=0, profile_views=0, clicks={}),
                                        rows, 7)
        assert "답글 최다: MARKET" in report

    def test_weighting_excludes_shorts(self):
        from src import insights, run_weighting
        stats = [insights.PostStat(post_id="1", posted_at=dt.datetime.now(dt.UTC), pillar=insights.SHORTS, replies=9)]
        assert all(s.pillar != insights.SHORTS for s in run_weighting._scores(stats, 0))

    def test_budget_still_counts_video(self, cfg):
        cfg(AUTOMATION_ENABLED=True, DAILY_POST_BUDGET=1)
        now = dt.datetime.combine(DAY, dt.time(20, 0), tzinfo=KST)
        assert safety.budget_block(safety.KIND_CHAT, [_post(now - dt.timedelta(hours=1), "VIDEO")], now)


# ---------------------------------------------------------------------------
# I. 점검 2026-10-04 반영 항목
# ---------------------------------------------------------------------------


class TestInspectionFixes:
    def test_concurrency_is_per_job(self):
        wf = yaml.safe_load((WF / "shorts.yml").read_text("utf-8"))
        assert "concurrency" not in wf          # 워크플로 단위 그룹 없음(승인 대기가 다음 날 build 를 막지 않게)
        assert wf["jobs"]["build"]["concurrency"]["group"] == "threads-shorts-build"
        pub = wf["jobs"]["publish"]["concurrency"]
        assert pub["group"] == "threads-shorts-publish" and pub["cancel-in-progress"] is True

    def test_job_budget_leaves_room_for_last_post(self):
        timeout = yaml.safe_load((WF / "shorts.yml").read_text("utf-8"))["jobs"]["publish"]["timeout-minutes"]
        worst_last_post_min, setup_min = 35, 5
        rng = random.Random(7)
        for _ in range(3000):
            start = dt.datetime.combine(DAY, dt.time(rng.randint(8, 20), rng.randint(0, 59)), tzinfo=KST)
            out = shorts_plan.publish_schedule(start, 3, rng=rng)
            if out:
                used = (out[-1] - start).total_seconds() / 60
                assert used + worst_last_post_min + setup_min <= timeout

    def test_size_limit_below_github_push_limit(self):
        assert config.VIDEO_MAX_BYTES < 100 * 1024 * 1024

    def test_face_fatal_message_is_facebook_specific(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_publish as rp
        sent = []
        monkeypatch.setattr(rp, "_notify", sent.append)
        monkeypatch.setattr(rp, "run", mock.Mock(side_effect=face_client.FaceApiError(400, "x", code=190)))
        assert rp.main() == safety.FATAL_EXIT_CODE
        assert sent and sent[0].startswith("[Facebook]") and "authorize" not in sent[0]
        assert "FACE_PAGE_TOKEN" in sent[0]

    def test_stale_only_manifest_notifies(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_publish as rp
        stale = (dt.datetime.now(KST).date() - dt.timedelta(days=1)).strftime("%Y%m%d")
        (tmp_path / "manifest.json").write_text(json.dumps({"items": [{"content_id": f"sv-{stale}-1"}]}), "utf-8")
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        sent = []
        monkeypatch.setattr(rp, "_notify", sent.append)
        assert rp.run() == 0
        assert sent and "지난 날짜" in sent[0]

    def test_render_length_regenerates_script_once(self, monkeypatch, tmp_path):
        from src import run_shorts_build as rb
        item = shorts_plan.PlannedVideo("sv-20261005-1", 0, "F1", ("face",))
        scripts = []

        def fake_write(key, **kw):
            scripts.append(kw.get("extra_instruction", ""))
            return mock.Mock(image_prompts=("a",) * 5, villain="Debt Titan", hook_type="B", warnings=(),
                             caption="c", beats=[mock.Mock(narration="훅입니다", tone="", is_hook=True, sfx="")],
                             to_dict=lambda: {})

        calls = {"n": 0}

        def fake_voice(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise renderer.RenderLengthError("너무 김")
            return renderer.Timing((3.0,), 1.0, 57.0)

        monkeypatch.setattr(rb.script_writer, "write_script", fake_write)
        monkeypatch.setattr(rb.image_gen, "generate_scenes", lambda *a, **k: [tmp_path / "i.png"] * 5)
        monkeypatch.setattr(rb, "_voice_and_render", fake_voice)
        monkeypatch.setattr(rb.validator, "check", lambda p: [])
        entry = rb.build_one(item, mood_source.Mood("none"), claude_key="k", gemini_key="g",
                             used_types=set(), used_hooks=[], base=tmp_path, used_captions=set())
        assert len(scripts) == 2 and scripts[0] == "" and "60초" in scripts[1]
        assert calls["n"] == 2 and entry["duration"] == 57.0

    def test_redact_github_installation_token(self):
        from src.redact import redact
        assert "ZZZZ" not in redact("push failed ghs_" + "Z" * 36)


# ---------------------------------------------------------------------------
# J. Facebook = GOC 단독 (마스터 결정 2026-10-04)
# ---------------------------------------------------------------------------


def _goc_raw(hook="시장에 경고등이 켜졌다"):
    raw = _valid_raw(hook=hook)
    raw["body"] = [
        "금리라는 무게가 오늘도 다시 시장의 어깨를 천천히 누르기 시작했습니다",
        "GOC 는 높은 성벽 위에서 흔들리는 도시의 불빛을 조용히 내려다봅니다",
        "지키는 일은 크게 외치는 일이 아니라 끝까지 자리를 지키는 일입니다",
        "지금 중요한 건 큰 소리가 아니라 끝까지 버티는 자세라고 그녀는 말합니다",
        "물가와 고용 이야기가 같은 날 한꺼번에 겹치면서 시장의 소음이 커집니다",
        "흔들릴수록 지금 무엇을 보고 있는지 차분히 적어 두는 편이 낫다고 그녀는 본다",
        "날개를 접은 GOC 는 아직 지켜야 할 것이 남았다며 다시 앞을 바라봅니다",
    ]
    raw["image_prompts"] = ["guardian heroine watching a city from a wall at dusk"] * config.SHORTS_IMAGE_COUNT
    raw["post_caption"] = "금리 이야기로 무거웠던 하루를 GOC 의 시선으로 정리했습니다. 소음보다 자세를 보자는 이야기입니다."
    return raw


class TestGocOnlyOnFacebook:
    def test_plan_characters(self, cfg):
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, FACE_ENABLED=True,
            FACE_RAMP_START="2025-01-01", SHORTS_THREADS_ENABLED=True)
        plan = shorts_plan.daily_plan(DAY)
        assert all(p.character == "GOC" for p in plan)          # 전부 Facebook 행 → GOC
        cfg(FACE_ENABLED=False)
        only_threads = shorts_plan.daily_plan(DAY)
        assert [p.character for p in only_threads] == ["EDT"]   # Threads 단독은 EDT

    def test_hook_never_villain_type_for_goc(self):
        assert hooks.select_hook_type("F1", None, allowed=hooks.NO_VILLAIN_HOOK_TYPES) == hooks.HOOK_A
        for used in ({hooks.HOOK_A}, {hooks.HOOK_A, hooks.HOOK_C}, set(hooks.NO_VILLAIN_HOOK_TYPES)):
            t = hooks.select_hook_type("F1", None, used, hooks.NO_VILLAIN_HOOK_TYPES)
            assert t in hooks.NO_VILLAIN_HOOK_TYPES

    def test_goc_valid_script_passes(self):
        assert script_writer.validate(_goc_raw(), hooks.HOOK_A, character="GOC") == []

    def test_goc_rejects_other_characters(self):
        raw = _goc_raw()
        raw["body"][1] = "EDT 가 높은 성벽 위에서 흔들리는 도시의 불빛을 조용히 내려다봅니다"
        raw["body"][2] = "뎁트타이탄의 사슬이 도시 위로 소리 없이 길게 내려오고 있습니다 정말로"
        raw["image_prompts"][0] = "a tiger hero with a chainsaw"
        issues = script_writer.validate(raw, hooks.HOOK_A, character="GOC")
        assert any("'EDT'" in i for i in issues)
        assert any("'뎁트타이탄'" in i for i in issues)
        assert any("'tiger'" in i for i in issues) and any("'chainsaw'" in i for i in issues)

    def test_edt_rejects_goc(self):
        raw = _valid_raw()
        raw["body"][0] = "GOC 가 오늘도 다시 시장의 어깨를 천천히 누르기 시작했습니다 조용히"
        issues = script_writer.validate(raw, hooks.HOOK_B, character="EDT")
        assert any("'GOC'" in i for i in issues)

    def test_goc_write_script(self, monkeypatch):
        seen = {}

        def fake(api_key, prompt, character="EDT"):
            seen["prompt"], seen["character"] = prompt, character
            return _goc_raw()

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        s = script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1",
                                       mood=mood_source.Mood("rss", ("금리",), "관망"), character="GOC")
        assert s.character == "GOC" and s.villain is None
        assert s.hook_type != hooks.HOOK_B
        assert seen["character"] == "GOC" and "빌런 없음" in seen["prompt"] and "GOC 수호 서사" in seen["prompt"]
        assert s.to_dict()["character"] == "GOC"

    def test_system_prompt_goc(self):
        sp = script_writer.system_prompt("GOC")
        assert "GOC 혼자만 등장" in sp and "EDT" in sp.split("다음 이름은 쓰지 않습니다:")[1].split("\n")[0]
        assert "영문은 GOC 와" in sp

    def test_goc_image_prompt(self):
        from src.video import image_gen
        p = image_gen.build_prompt("guardian watching the city", None, True, "GOC")
        assert "Guardian of Capital (GOC)" in p and "only character" in p
        assert "tiger" not in p.lower() and "Debt Titan" not in p
        e = image_gen.build_prompt("x", "Debt Titan", False, "EDT")
        assert "tiger" in e and "GOC" not in e

    def test_reference_dirs_isolated(self, tmp_path):
        from src.video import assets
        (tmp_path / "reference" / "edt").mkdir(parents=True)
        (tmp_path / "reference" / "goc").mkdir(parents=True)
        (tmp_path / "reference" / "edt" / "edt.png").write_bytes(b"x")
        (tmp_path / "reference" / "goc" / "goc.png").write_bytes(b"x")
        assert [p.name for p in assets.reference_images("GOC", root=tmp_path)] == ["goc.png"]
        assert [p.name for p in assets.reference_images("EDT", root=tmp_path)] == ["edt.png"]

    def test_generate_scenes_uses_goc_refs(self, monkeypatch, tmp_path):
        from src.video import image_gen
        asked = []
        monkeypatch.setattr(image_gen.assets, "reference_images", lambda c: asked.append(c) or [])

        class Part:
            inline_data = type("I", (), {"data": b"png"})()

        class Resp:
            candidates = [type("C", (), {"content": type("Ct", (), {"parts": [Part()]})()})()]

        sent = []

        class Client:
            class models:
                @staticmethod
                def generate_content(model, contents, config=None):
                    sent.append(contents[-1])
                    return Resp()

        out = image_gen.generate_scenes("k", ["scene"] * 2, None, tmp_path, client=Client, character="GOC")
        assert asked == ["GOC"] and all(p for p in out)
        assert all("Guardian of Capital (GOC)" in s for s in sent)

    def test_build_one_passes_character(self, monkeypatch, tmp_path):
        from src import run_shorts_build as rb
        item = shorts_plan.PlannedVideo("sv-20261005-1", 0, "F1", ("face",), "GOC")
        got = {}

        def fake_write(key, **kw):
            got["script_char"] = kw.get("character")
            return mock.Mock(image_prompts=("a",) * 5, villain=None, hook_type="A", caption="c", warnings=(),
                             beats=[mock.Mock(narration="훅입니다", tone="", is_hook=True, sfx="")],
                             to_dict=lambda: {})

        def fake_images(key, prompts, villain, out, character="EDT"):
            got["image_char"] = character
            return [tmp_path / "i.png"] * 5

        monkeypatch.setattr(rb.script_writer, "write_script", fake_write)
        monkeypatch.setattr(rb.image_gen, "generate_scenes", fake_images)
        monkeypatch.setattr(rb, "_voice_and_render", lambda *a, **k: renderer.Timing((3.0,), 1.0, 57.0))
        monkeypatch.setattr(rb.validator, "check", lambda p: [])
        entry = rb.build_one(item, mood_source.Mood("none"), claude_key="k", gemini_key="g",
                             used_types=set(), used_hooks=[], base=tmp_path, used_captions=set())
        assert got == {"script_char": "GOC", "image_char": "GOC"} and entry["character"] == "GOC"


# ---------------------------------------------------------------------------
# DN2026_0002 — Facebook 릴스를 ICG 웹툰 X 발행(월~토 KST 06:06)에 맞춤
# ---------------------------------------------------------------------------


class TestDN2026_0002:
    def _now(self, hh, mm=0):
        return dt.datetime.combine(DAY, dt.time(hh, mm), tzinfo=KST)

    def test_build_cron_is_mon_to_sat_0506_kst(self):
        wf = yaml.safe_load((WF / "shorts.yml").read_text("utf-8"))
        on = wf.get(True) or wf.get("on")
        assert [e["cron"] for e in on["schedule"]] == ["6 20 * * 0-5"]
        # UTC 일~금 20:06 → KST 월~토 05:06 (일요일 미실행)
        kst_days = set()
        for utc_wd in (6, 0, 1, 2, 3, 4):  # cron 0-5 = UTC 일~금 (Python weekday: 일=6)
            base = dt.datetime(2026, 10, 4, 20, 6, tzinfo=dt.UTC)  # 2026-10-04 = 일요일
            shift = (utc_wd - 6) % 7
            kst = (base + dt.timedelta(days=shift)).astimezone(KST)
            assert kst.strftime("%H:%M") == "05:06"
            kst_days.add(kst.weekday())
        assert kst_days == {0, 1, 2, 3, 4, 5}  # 월~토

    def test_rest_day_default_disabled(self):
        assert config.SHORTS_WEEKLY_REST_DAYS == 0
        body = (WF / "shorts.yml").read_text("utf-8")
        assert "vars.SHORTS_WEEKLY_REST_DAYS || '0'" in body

    def test_face_window_starts_0606_with_jitter(self):
        for seed in range(200):
            out = shorts_plan.publish_schedule(
                self._now(5, 30), 1, rng=random.Random(seed),
                window=config.SHORTS_FACE_PUBLISH_WINDOW)
            assert self._now(6, 7) <= out[0] <= self._now(6, 16)  # DN2026_0004 : 지연 5~40분 → 1~10분

    def test_threads_window_unchanged(self):
        assert config.SHORTS_PUBLISH_WINDOW == ("10:00", "22:00")
        out = shorts_plan.publish_schedule(self._now(5, 30), 1, rng=random.Random(1))
        assert self._now(10, 1) <= out[0] <= self._now(10, 10)  # DN2026_0004 : 지연 5~40분 → 1~10분

    def test_face_three_posts_fit_job_budget_from_early_start(self):
        timeout = yaml.safe_load((WF / "shorts.yml").read_text("utf-8"))["jobs"]["publish"][
            "timeout-minutes"]
        rng = random.Random(11)
        for _ in range(2000):
            start = self._now(5, rng.randint(10, 59))
            out = shorts_plan.publish_schedule(start, 3, rng=rng,
                                               window=config.SHORTS_FACE_PUBLISH_WINDOW)
            assert out and out[0] >= self._now(6, 7)  # DN2026_0004 : 지연 5~40분 → 1~10분
            used = (out[-1] - start).total_seconds() / 60
            assert used + 35 + 5 <= timeout


    def test_threads_kept_when_publish_starts_right_after_early_build(self):
        # publish 가 05:07 에 시작 → 예산 끝 10:07. Threads 첫 편이 빠지지 않고 10:00~10:07 안에 들어와야 한다.
        for seed in range(300):
            start = self._now(5, 7)
            out = shorts_plan.publish_schedule(start, 1, rng=random.Random(seed))
            assert len(out) == 1
            assert self._now(10, 0) <= out[0] <= start + dt.timedelta(
                minutes=config.SHORTS_JOB_BUDGET_MIN)

    def test_window_end_still_blocks(self):
        # 시간대 끝(22:00) 이후로는 여전히 게시하지 않는다(기존 규칙 유지).
        assert shorts_plan.publish_schedule(self._now(22, 1), 1, rng=random.Random(1)) == []


class TestDN2026_0002Runner:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_publish as rp
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        monkeypatch.setenv("FACE_PAGE_ID", "123")
        monkeypatch.setenv("FACE_PAGE_TOKEN", "EAA" + "t" * 30)
        monkeypatch.setenv("THREADS_LONG_LIVED_TOKEN", "THAA" + "x" * 40)
        monkeypatch.setenv("THREADS_USER_ID", "1234567890123456")
        monkeypatch.setattr(rp, "_notify", lambda m: None)
        waits: list[dt.datetime] = []
        monkeypatch.setattr(rp, "_sleep_until", waits.append)
        fixed = dt.datetime.combine(dt.datetime.now(KST).date(), dt.time(5, 40), tzinfo=KST)

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz is None or tz == KST else fixed.astimezone(tz)

        monkeypatch.setattr(rp.dt, "datetime", _DT)
        return rp, tmp_path, fixed, waits

    def test_shared_video_posted_once_per_channel_on_own_window(self, env, monkeypatch, cfg):
        rp, tmp, fixed, waits = env
        today = fixed.date()
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True, SHORTS_THREADS_ENABLED=True)
        ids = [f"sv-{today:%Y%m%d}-1", f"sv-{today:%Y%m%d}-2"]
        _manifest(tmp, ids)  # 1편: face+threads 공용, 2편: face
        monkeypatch.setenv("DRY_RUN", "false")
        face = mock.Mock()
        face.recent_descriptions.return_value = []
        face.publish_reel.return_value = ("v1", face_client.ReelStatus(
            "ready", "complete", "complete", "complete", ""))
        monkeypatch.setattr(rp, "FaceClient", mock.Mock(return_value=face))
        threads = mock.Mock()
        threads.get_my_posts.return_value = []
        threads.publish_video_post.return_value = "p1"
        monkeypatch.setattr(rp, "_threads_client", lambda: threads)
        monkeypatch.setattr(rp.media_host, "publish_file", lambda v, n: f"https://raw/{n}")
        monkeypatch.setattr(rp.media_host, "verify", lambda u: "video/mp4")

        assert rp.run() == 0
        assert face.publish_reel.call_count == 2          # 공용 1편 + 2편, 중복 없음
        threads.publish_video_post.assert_called_once()   # 공용 1편만
        assert waits == sorted(waits) and len(waits) == 3
        at = lambda h, m: dt.datetime.combine(today, dt.time(h, m), tzinfo=KST)  # noqa: E731
        assert at(6, 7) <= waits[0] <= at(6, 16)           # 첫 Facebook — 06:06 + 1~10분 (DN2026_0004)
        assert any(at(10, 1) <= w <= at(10, 10) for w in waits)  # Threads — 10:00 + 1~10분 (DN2026_0004)


# ---------------------------------------------------------------------------
# DN2026_0003 — 발행 거래 분리 (Facebook 게시 = face_publish.yml)
# ---------------------------------------------------------------------------


class TestDN2026_0003Workflows:
    def _wf(self, name):
        return yaml.safe_load((WF / name).read_text("utf-8"))

    def test_shorts_publish_job_is_threads_only(self):
        job = self._wf("shorts.yml")["jobs"]["publish"]
        env = job["steps"][-1]["env"]
        assert env["SHORTS_PUBLISH_CHANNELS"] == "threads"

    def test_shorts_dispatches_face_pipeline_after_build(self):
        job = self._wf("shorts.yml")["jobs"]["dispatch_face"]
        assert job["needs"] == "build" and "has_items" in job["if"]
        assert job["permissions"] == {"actions": "write"}
        run = job["steps"][0]["run"]
        assert "gh workflow run face_publish.yml" in run
        assert "build_run_id=\"${{ github.run_id }}\"" in run and "timing=window" in run

    def test_face_publish_workflow_shape(self):
        wf = self._wf("face_publish.yml")
        on = wf.get(True) or wf.get("on")
        inputs = on["workflow_dispatch"]["inputs"]
        assert set(inputs) == {"mode", "timing", "build_run_id", "target_date"}
        assert inputs["mode"]["default"] == "dry_run"
        assert wf["permissions"] == {"contents": "read", "actions": "read"}
        job = wf["jobs"]["publish"]
        assert job["concurrency"]["cancel-in-progress"] is False
        step = job["steps"][-1]
        assert step["run"] == "python -m src.run_shorts_publish"
        assert step["env"]["SHORTS_PUBLISH_CHANNELS"] == "face"
        assert "THREADS_LONG_LIVED_TOKEN" not in step["env"]   # Threads 자격 증명 미보유
        dl = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact"))
        assert dl["with"]["run-id"] == "${{ steps.art.outputs.run_id }}"


class TestDN2026_0003Runner:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_publish as rp
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        monkeypatch.setenv("FACE_PAGE_ID", "123")
        monkeypatch.setenv("FACE_PAGE_TOKEN", "EAA" + "t" * 30)
        monkeypatch.setenv("THREADS_LONG_LIVED_TOKEN", "THAA" + "x" * 40)
        monkeypatch.setenv("THREADS_USER_ID", "1234567890123456")
        monkeypatch.setattr(rp, "_notify", lambda m: None)
        waits: list[dt.datetime] = []
        monkeypatch.setattr(rp, "_sleep_until", waits.append)
        state = {"now": dt.datetime(2026, 10, 5, 22, 23, tzinfo=KST)}   # 오늘 실제 제외된 시각

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                n = state["now"]
                return n if tz is None or tz == KST else n.astimezone(tz)

        monkeypatch.setattr(rp.dt, "datetime", _DT)
        return rp, tmp_path, state, waits

    def _clients(self, rp, monkeypatch):
        face = mock.Mock()
        face.recent_descriptions.return_value = []
        face.publish_reel.return_value = ("v1", face_client.ReelStatus(
            "ready", "complete", "complete", "complete", ""))
        monkeypatch.setattr(rp, "FaceClient", mock.Mock(return_value=face))
        threads = mock.Mock(side_effect=AssertionError("Threads 클라이언트 생성 금지"))
        monkeypatch.setattr(rp, "_threads_client", threads)
        return face

    def test_face_only_manual_now_publishes_backlog_after_window(self, env, monkeypatch, cfg):
        rp, tmp, state, waits = env
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True, SHORTS_THREADS_ENABLED=True)
        _manifest(tmp, ["sv-20261005-1"])          # face+threads 공용 영상
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("SHORTS_PUBLISH_CHANNELS", "face")
        monkeypatch.setenv("SHORTS_IGNORE_WINDOW", "true")
        face = self._clients(rp, monkeypatch)
        assert rp.run() == 0
        face.publish_reel.assert_called_once()
        assert len(waits) == 1
        assert waits[0] == state["now"]   # DN2026_0004 : 수동 즉시 게시 — 첫 편 지연 없음

    def test_face_window_mode_still_excludes_after_22(self, env, monkeypatch, cfg):
        rp, tmp, state, waits = env
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True)
        _manifest(tmp, ["sv-20261005-1"], channels=(("face",),))
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("SHORTS_PUBLISH_CHANNELS", "face")
        face = self._clients(rp, monkeypatch)
        assert rp.run() == 0
        face.publish_reel.assert_not_called()

    def test_target_date_publishes_previous_day_after_midnight(self, env, monkeypatch, cfg):
        rp, tmp, state, waits = env
        state["now"] = dt.datetime(2026, 10, 6, 0, 30, tzinfo=KST)
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True)
        _manifest(tmp, ["sv-20261005-1"], channels=(("face",),))
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("SHORTS_PUBLISH_CHANNELS", "face")
        monkeypatch.setenv("SHORTS_IGNORE_WINDOW", "true")
        face = self._clients(rp, monkeypatch)
        assert rp.run() == 0
        face.publish_reel.assert_not_called()       # 날짜 미지정 → 신선도 제외
        monkeypatch.setenv("SHORTS_TARGET_DATE", "2026-10-05")
        assert rp.run() == 0
        face.publish_reel.assert_called_once()      # 지정 회차 날짜 → 게시

    def test_threads_only_skips_face(self, env, monkeypatch, cfg):
        rp, tmp, state, waits = env
        state["now"] = dt.datetime(2026, 10, 5, 5, 40, tzinfo=KST)
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True, SHORTS_THREADS_ENABLED=True)
        _manifest(tmp, ["sv-20261005-1"])
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("SHORTS_PUBLISH_CHANNELS", "threads")
        monkeypatch.setattr(rp, "FaceClient", mock.Mock(side_effect=AssertionError("FB 생성 금지")))
        threads = mock.Mock()
        threads.get_my_posts.return_value = []
        threads.publish_video_post.return_value = "p1"
        monkeypatch.setattr(rp, "_threads_client", lambda: threads)
        monkeypatch.setattr(rp.media_host, "publish_file", lambda v, n: f"https://raw/{n}")
        monkeypatch.setattr(rp.media_host, "verify", lambda u: "video/mp4")
        assert rp.run() == 0
        threads.publish_video_post.assert_called_once()

    @pytest.mark.parametrize("name,value", [("SHORTS_PUBLISH_CHANNELS", "facebook"),
                                            ("SHORTS_TARGET_DATE", "2026/10/05"),
                                            ("SHORTS_TARGET_DATE", "2026-10-07")])
    def test_invalid_inputs_fail_closed(self, env, monkeypatch, cfg, name, value):
        rp, tmp, state, waits = env
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True)
        _manifest(tmp, ["sv-20261005-1"], channels=(("face",),))
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv(name, value)
        face = self._clients(rp, monkeypatch)
        assert rp.main() != 0
        face.publish_reel.assert_not_called()


# ---------------------------------------------------------------------------
# DN2026_0004 — 즉시 게시 · 안티봇 지연 1~10분
# ---------------------------------------------------------------------------


class TestDN2026_0004:
    def _now(self, hh, mm=0):
        return dt.datetime.combine(DAY, dt.time(hh, mm), tzinfo=KST)

    def test_config_values(self):
        assert config.SHORTS_FIRST_JITTER_MIN == (1, 10)
        assert config.SHORTS_GAP_MIN == (1, 10)
        assert config.SHORTS_GAP_FLOOR_MIN == 1

    def test_immediate_first_post_has_no_delay(self):
        now = self._now(22, 54)
        out = shorts_plan.publish_schedule(now, 1, rng=random.Random(1),
                                           window=("00:00", "23:59"), immediate=True)
        assert out == [now]

    def test_immediate_following_posts_gap_1_to_10(self):
        now = self._now(14, 0)
        for seed in range(300):
            out = shorts_plan.publish_schedule(now, 3, rng=random.Random(seed),
                                               window=("00:00", "23:59"), immediate=True)
            assert out[0] == now and len(out) == 3
            gaps = [(b - a).total_seconds() / 60 for a, b in zip(out, out[1:], strict=False)]
            assert all(1 <= g <= 10 for g in gaps)

    def test_regular_delays_all_within_1_to_10(self):
        for seed in range(300):
            out = shorts_plan.publish_schedule(self._now(5, 30), 3, rng=random.Random(seed),
                                               window=config.SHORTS_FACE_PUBLISH_WINDOW)
            assert self._now(6, 7) <= out[0] <= self._now(6, 16)
            gaps = [(b - a).total_seconds() / 60 for a, b in zip(out, out[1:], strict=False)]
            assert len(out) == 3 and all(1 <= g <= 10 for g in gaps)
            assert out[-1] <= self._now(6, 36)   # 3편 모두 06:07~06:36 안에 끝난다

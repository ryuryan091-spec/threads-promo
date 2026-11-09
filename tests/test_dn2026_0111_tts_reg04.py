"""2026-11-07·11-09 숏폼 빌드 연속 실패 회귀 테스트.

- 11-07: google-genai 3.0.0 기본 API(v1)가 TTS speech_config 를 거부(400 INVALID_ARGUMENT).
- 11-09: 허용 테마어 '안전자산'이 REG-04 접미 '전자' 패턴에 '안전자'로 걸려 대본 3회 탈락.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from src import config, content, mood_source, shorts_plan
from src.video import script_writer, tts

# ---------------------------------------------------------------- REG-04 오탐


@pytest.mark.parametrize("term", config.CHAT_THEME_ALLOWLIST)
def test_every_allowlisted_theme_passes_entity_check(term):
    # 허용목록 단어는 기업·인물명 검사에 걸리면 안 된다(허용·차단 목록 충돌 회귀 방지).
    assert content._entity_hits(f"요즘은 {term} 이야기가 많습니다") == []


def test_safe_asset_sentence_passes_lint_shorts():
    content.lint_shorts("돈이 안전자산 쪽으로 몰리는 분위기입니다", max_len=48, label="본문5",
                        extra_latin=("GOC",))


def test_safe_asset_passes_reply_and_chat_path():
    content.check_forecast_entity("요즘 안전자산 이야기가 많네요", label="답글")


@pytest.mark.parametrize("text, hit", [
    ("삼성전자 이야기가 많습니다", "삼성전자"),
    ("안전자산과 엘지전자를 함께 봅니다", "엘지전자"),
    ("안전자산 사이로 메타가 보입니다", "메타"),
])
def test_real_entities_still_blocked(text, hit):
    assert any(hit in h for h in content._entity_hits(text))
    with pytest.raises(content.ContentPolicyError):
        content.lint_shorts(text, max_len=48, label="본문")


def test_collision_set_is_only_safe_asset():
    # 새 허용어가 충돌을 만들면 이 테스트가 먼저 알려 준다.
    assert content._allowlist_collisions() == ("안전자산",)


# ---------------------------------------------------------------- TTS


class _Err(Exception):
    def __init__(self, code):
        super().__init__(f"{code} ERR")
        self.code = code


def _fake_client(side_effects):
    calls = []

    def gen(**kw):
        calls.append(kw)
        eff = side_effects[min(len(calls) - 1, len(side_effects) - 1)]
        if isinstance(eff, Exception):
            raise eff
        return eff

    return SimpleNamespace(models=SimpleNamespace(generate_content=gen)), calls


def _audio_resp():
    part = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x00" * 10))
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])


def test_tts_client_pins_v1beta(monkeypatch, tmp_path):
    from google import genai
    got = {}

    def fake_client(**kw):
        got.update(kw)
        return _fake_client([_audio_resp()])[0]

    monkeypatch.setattr(genai, "Client", fake_client)
    tts.synthesize("k", ["대사"], [""], tmp_path)
    assert got["http_options"].api_version == "v1beta"


def test_tts_permanent_400_not_retried(tmp_path):
    client, calls = _fake_client([_Err(400)])
    with pytest.raises(tts.TtsError):
        tts.synthesize("k", ["대사"], [""], tmp_path, client=client)
    assert len(calls) == 1


@pytest.mark.parametrize("code", [429, 500, 503])
def test_tts_transient_errors_retried(tmp_path, code):
    client, calls = _fake_client([_Err(code), _audio_resp()])
    paths = tts.synthesize("k", ["대사"], [""], tmp_path, client=client)
    assert len(calls) == 2 and paths[0].exists()


# ---------------------------------------------------------------- 빌드 순서


def test_tts_failure_skips_paid_image_generation(monkeypatch, tmp_path):
    from src import run_shorts_build as rb
    item = shorts_plan.PlannedVideo("sv-20261109-1", 0, "F1", ("face",), "GOC")
    script = mock.Mock(image_prompts=("p",) * 7, villain=None, to_dict=lambda: {},
                       beats=[mock.Mock(narration="훅", tone="", is_hook=True, sfx="")])
    monkeypatch.setattr(rb.script_writer, "write_script", lambda *a, **k: script)
    images = mock.Mock(return_value=[])
    monkeypatch.setattr(rb.image_gen, "generate_scenes", images)

    def boom(*a, **k):
        raise tts.TtsError("beat 0 내레이션 생성 실패")

    monkeypatch.setattr(rb.tts, "synthesize", boom)
    with pytest.raises(tts.TtsError):
        rb.build_one(item, mood_source.Mood("none"), claude_key="k", gemini_key="g",
                     used_types=set(), used_hooks=[], base=tmp_path)
    images.assert_not_called()


# ---------------------------------------------------------------- 재시도 누적


def test_retry_prompt_accumulates_previous_violations(monkeypatch):
    prompts = []
    monkeypatch.setattr(script_writer, "_call_claude",
                        lambda key, prompt, character: prompts.append(prompt) or {})
    seq = iter([["위반A(REG-04)"], ["위반B(길이)"], ["위반A(REG-04)"]])
    monkeypatch.setattr(script_writer, "validate", lambda *a, **k: next(seq))
    with pytest.raises(script_writer.ScriptError):
        script_writer.write_script("k", content_id="sv-20261109-1", fmt="F1",
                                   mood=mood_source.Mood("none"), character=config.CHARACTER_GOC)
    assert len(prompts) == 3
    assert "위반A(REG-04)" in prompts[2] and "위반B(길이)" in prompts[2]
    assert prompts[2].count("위반A(REG-04)") == 1

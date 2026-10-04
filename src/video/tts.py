"""한국어 내레이션 — Gemini TTS(GEMINI_API_SUB_PAY_KEY).

investment_comic_tube src/tts.py 와 같은 방식이다.
  - response_modalities=["AUDIO"] + prebuilt voice(기본 Charon)
  - 응답은 24kHz / mono / 16-bit PCM 원시 바이트 → WAV 헤더를 씌워 저장
  - 톤 지시문이 그대로 낭독되지 않게 '낭독할 대사:' 로 구간을 분리
차이: 비트 하나라도 음성이 없으면 영상을 만들지 않는다(무음 장면 폴백 없음).
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

from .. import config

VERSION = "1.0.0"

log = logging.getLogger(__name__)

PCM_SAMPLE_RATE = 24_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
DEFAULT_TONE = "차분하지만 힘 있는 톤으로 또박또박"


class TtsError(RuntimeError):
    """내레이션을 만들지 못했다."""


def write_wave(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(PCM_CHANNELS)
        wf.setsampwidth(PCM_SAMPLE_WIDTH)
        wf.setframerate(PCM_SAMPLE_RATE)
        wf.writeframes(pcm)


def _extract_pcm(response) -> bytes | None:
    for cand in getattr(response, "candidates", None) or []:
        parts = getattr(getattr(cand, "content", None), "parts", None) or []
        for part in parts:
            data = getattr(getattr(part, "inline_data", None), "data", None)
            if data:
                return data
    return None


def synthesize(
    api_key: str, lines: list[str], tones: list[str], out_dir: Path, *, client=None
) -> list[Path]:
    """비트별 WAV 경로. 하나라도 실패하면 TtsError."""
    from google import genai
    from google.genai import types

    client = client or genai.Client(api_key=api_key)
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.SHORTS_TTS_VOICE)
            )
        ),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for idx, line in enumerate(lines):
        tone = tones[idx] if idx < len(tones) and tones[idx] else DEFAULT_TONE
        prompt = f"다음 대사를 {tone}으로 낭독해라. 낭독할 대사: {line}"
        pcm = None
        last = ""
        for attempt in range(1, config.SHORTS_TTS_ATTEMPTS + 1):
            try:
                resp = client.models.generate_content(
                    model=config.SHORTS_TTS_MODEL, contents=prompt, config=cfg
                )
                pcm = _extract_pcm(resp)
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
                log.warning("TTS 실패 beat=%d attempt=%d: %s", idx, attempt, last)
                continue
            if pcm:
                break
            last = "응답에 오디오 없음"
        if not pcm:
            raise TtsError(f"beat {idx} 내레이션 생성 실패: {last}")
        path = out_dir / f"narration_{idx}.wav"
        write_wave(path, pcm)
        paths.append(path)
    log.info("TTS 완료 %d건 모델=%s 음성=%s", len(paths), config.SHORTS_TTS_MODEL, config.SHORTS_TTS_VOICE)
    return paths

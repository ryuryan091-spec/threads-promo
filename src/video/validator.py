"""완성 영상 규격 검사 (ffprobe). Facebook 릴스·Threads 동영상 공통 요구를 한 번에 본다.

  컨테이너 MP4 · H.264 · 1080x1920 · 30fps · AAC 48kHz 스테레오 · 55~60초 · 용량 상한
  moov atom 이 mdat 보다 앞(faststart) — Threads 요구
"""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

from .. import config

VERSION = "1.0.0"


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return {}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}


def top_level_atoms(path: Path, limit: int = 32) -> list[str]:
    """MP4 최상위 atom 이름 순서."""
    names: list[str] = []
    with path.open("rb") as fh:
        while len(names) < limit:
            header = fh.read(8)
            if len(header) < 8:
                break
            size, kind = struct.unpack(">I4s", header)
            names.append(kind.decode("latin-1"))
            if size == 1:
                size = struct.unpack(">Q", fh.read(8))[0]
                fh.seek(size - 16, 1)
            elif size == 0:
                break
            else:
                fh.seek(size - 8, 1)
    return names


def _fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def check(path: Path) -> list[str]:
    """위반 목록. 빈 목록이면 통과."""
    if not path.exists():
        return [f"파일 없음: {path}"]
    issues: list[str] = []
    size = path.stat().st_size
    if size > config.VIDEO_MAX_BYTES:
        issues.append(f"용량 {size:,}바이트 > 상한 {config.VIDEO_MAX_BYTES:,}")
    info = _probe(path)
    if not info:
        return issues + ["ffprobe 실패"]
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        issues.append("영상 스트림 없음")
    else:
        if video.get("codec_name") != "h264":
            issues.append(f"영상 코덱 {video.get('codec_name')} ≠ h264")
        if (video.get("width"), video.get("height")) != (config.VIDEO_WIDTH, config.VIDEO_HEIGHT):
            issues.append(f"해상도 {video.get('width')}x{video.get('height')}")
        fps = _fps(str(video.get("r_frame_rate", "0/1")))
        if abs(fps - config.VIDEO_FPS) > 0.01:
            issues.append(f"프레임레이트 {fps:.2f} ≠ {config.VIDEO_FPS}")
        if video.get("pix_fmt") != "yuv420p":
            issues.append(f"픽셀 형식 {video.get('pix_fmt')} ≠ yuv420p")
    if not audio:
        issues.append("오디오 스트림 없음")
    else:
        if audio.get("codec_name") != "aac":
            issues.append(f"오디오 코덱 {audio.get('codec_name')} ≠ aac")
        if int(audio.get("sample_rate", 0)) != config.AUDIO_SAMPLE_RATE:
            issues.append(f"샘플레이트 {audio.get('sample_rate')} ≠ {config.AUDIO_SAMPLE_RATE}")
        if int(audio.get("channels", 0)) != 2:
            issues.append(f"채널 {audio.get('channels')} ≠ 2")
    try:
        duration = float((info.get("format") or {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if not config.VIDEO_MIN_SEC <= duration <= config.VIDEO_MAX_SEC:
        issues.append(f"길이 {duration:.2f}초 — {config.VIDEO_MIN_SEC}~{config.VIDEO_MAX_SEC}초 필요")
    atoms = top_level_atoms(path)
    if "moov" in atoms and "mdat" in atoms and atoms.index("moov") > atoms.index("mdat"):
        issues.append("moov atom 이 mdat 뒤에 있음(faststart 아님)")
    if "moov" not in atoms:
        issues.append("moov atom 없음")
    return issues

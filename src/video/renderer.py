"""60초 세로 숏폼 렌더링 (ffmpeg).

연출 파라미터는 investment_comic_tube src/renderer.py(운영 중)에서 확인한 값을 쓴다.
  - 훅: 펀치인(1.35→1.0) · 비동기 흔들림(9Hz/11Hz, 18px) · 노랑 86pt 자막(화면 68% 높이) · 효과음
  - 본문: Ken Burns(최대 1.12배, 장면마다 줌 방향 교대) · 하단 자막
  - 아웃트로 2초: 표지 + 로고(알파 없으면 크로마키) · 계정 표기 · AI 고지
  - 최종 음량 loudnorm I=-14 / TP=-1.5 / LRA=11, BGM 은 낮은 볼륨으로 깐다
이 레포 요구에 맞춘 차이: 오디오 48kHz(Facebook 릴스), 총 길이 55~60초 맞춤, moov 앞배치(faststart).
"""

from __future__ import annotations

import json
import logging
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import config
from . import assets

VERSION = "1.2.0"   # v1.8.4: 훅 낭독 가속 · v1.8.3: 빠른 훅·움직임 다양화 · v1.8.0 베타 수정

log = logging.getLogger(__name__)

W, H, FPS = config.VIDEO_WIDTH, config.VIDEO_HEIGHT, config.VIDEO_FPS
SR = config.AUDIO_SAMPLE_RATE

# YouTube 렌더러 값
KEN_BURNS_MAX_ZOOM = 1.12
KEN_BURNS_SPEED = 0.0012
# v1.8.3 "변화": 본문 비트마다 카메라 움직임을 바꾼다(같은 이미지를 연속 두 비트에 써도 움직임이 다름).
PAN_ZOOM = 1.18
BODY_MOTIONS: tuple[str, ...] = ("zoom_in", "pan_right", "zoom_out", "pan_left", "tilt_up", "tilt_down")
HOOK_MIN_SEC = 2.0              # v1.8.3 "빠른 훅": 3.0 → 2.0초(훅 8~14자 낭독 길이에 맞춤)
HOOK_MAX_SEC = 8.0
HOOK_PUNCH_START_ZOOM = 1.35
HOOK_PUNCH_SPEED = 0.11
HOOK_SHAKE_PX = 18
HOOK_SHAKE_PAD = 1.10
HOOK_FONT_SIZE = 86
HOOK_FONT_COLOR = "0xFFEE00"
HOOK_WRAP_CHARS = 13
HOOK_TEXT_Y_RATIO = 0.68
OUTRO_DURATION_SEC = 2.0
OUTRO_ZOOM_SPEED = 0.0008
OUTRO_MAX_ZOOM = 1.08
LOGO_WIDTH_RATIO = 0.18
LOGO_MARGIN_PX = 60
LOGO_CHROMA_KEY = "0xFF00FF"
LOGO_CHROMA_SIMILARITY = "0.30"
LOGO_CHROMA_BLEND = "0.10"
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"

# 이 레포 값
SEG_PAD_SEC = 0.3              # 비트 내레이션 뒤 여백
SEG_MAX_SEC = 9.0              # 길이 맞춤으로 늘릴 때 비트 상한
TEMPO_MAX = 1.25               # 전체 낭독 속도 상한(넘으면 대본이 너무 길다)
# v1.8.4 Q1: 훅 낭독이 길면(운영 베타 3.7초) 훅 구간만 더 빠르게 재생한다. 공통 속도에 곱하며 상한을 둔다.
HOOK_FAST_SEC = 2.5            # 훅 낭독 목표 길이
HOOK_TEMPO_MAX = 1.25          # 훅 추가 가속 상한(공통 속도와 곱한 최댓값 1.25×1.25 ≈ 1.56)
TARGET_MIN_SEC = config.VIDEO_MIN_SEC + 0.5
TARGET_MAX_SEC = config.VIDEO_MAX_SEC - 0.5
SUB_FONT_SIZE = 56
SUB_WRAP_CHARS = 16
SUB_Y_RATIO = 0.80
BGM_VOLUME = "0.12"            # 약 -18dB
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
)


class RenderError(RuntimeError):
    """렌더링 실패."""


class RenderLengthError(RenderError):
    """내레이션이 너무 길어 60초 안에 넣을 수 없다(대본 재생성 대상)."""


@dataclass(frozen=True)
class SceneInput:
    image: Path
    audio: Path
    caption: str
    is_hook: bool = False
    sfx: Path | None = None


@dataclass(frozen=True)
class Timing:
    durations: tuple[float, ...]     # 비트별 장면 길이(초)
    tempo: float                     # 공통 낭독 속도 배율(1.0 = 원속)
    total: float                     # 아웃트로 포함 총 길이
    hook_tempo: float = 1.0          # v1.8.4: 훅 구간 추가 가속(공통 속도에 곱함)

    def scene_tempo(self, is_hook: bool) -> float:
        return round(self.tempo * (self.hook_tempo if is_hook else 1.0), 4)


def find_kr_font() -> str | None:
    """한글 글리프가 있는 폰트 경로. fontfile 을 지정하지 않으면 두부(□)가 나온다(YouTube 렌더러 주석)."""
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    if shutil.which("fc-match"):
        out = subprocess.run(["fc-match", "-f", "%{file}", "Noto Sans CJK KR:bold"],
                             capture_output=True, text=True, check=False)
        if out.returncode == 0 and out.stdout.strip() and Path(out.stdout.strip()).exists():
            return out.stdout.strip()
    return None


def wrap_korean(text: str, width: int) -> str:
    """어절 단위 줄바꿈. 한 어절이 width 보다 길면 그 어절만 글자 단위로 자른다."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        while len(word) > width:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = f"{line} {word}".strip()
        if len(candidate) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return "\n".join(lines)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RenderError(f"길이 조회 실패 {path.name}: {out.stderr[:200]}") from exc


def plan_timing(narration_sec: list[float], hook_index: int = 0) -> Timing:
    """비트별 길이와 공통 낭독 속도. 총 길이를 TARGET_MIN~TARGET_MAX 로 맞춘다.

    1) 원속 기준 총 길이가 상한을 넘으면 낭독 속도를 올린다(TEMPO_MAX 초과 시 RenderLengthError).
    2) 하한에 못 미치면 훅을 뺀 비트에 여백을 고르게 더한다(비트 상한 SEG_MAX_SEC).
    """
    if not narration_sec or any(d <= 0 for d in narration_sec):
        raise RenderError("내레이션 길이가 없습니다")
    overhead = SEG_PAD_SEC * len(narration_sec) + OUTRO_DURATION_SEC
    natural = sum(narration_sec)
    tempo = 1.0
    if natural + overhead > TARGET_MAX_SEC:
        tempo = natural / (TARGET_MAX_SEC - overhead)
        if tempo > TEMPO_MAX:
            raise RenderLengthError(
                f"내레이션 {natural:.1f}초 — {TARGET_MAX_SEC}초 안에 넣으려면 {tempo:.2f}배속 필요"
                f"(상한 {TEMPO_MAX})"
            )
    hook_spoken = narration_sec[hook_index] / tempo
    hook_tempo = min(HOOK_TEMPO_MAX, hook_spoken / HOOK_FAST_SEC) if hook_spoken > HOOK_FAST_SEC else 1.0
    durations = [d / tempo + SEG_PAD_SEC for d in narration_sec]
    durations[hook_index] = hook_spoken / hook_tempo + SEG_PAD_SEC
    durations[hook_index] = max(HOOK_MIN_SEC, durations[hook_index])
    if durations[hook_index] > HOOK_MAX_SEC:
        raise RenderLengthError(f"훅 {durations[hook_index]:.1f}초 — 상한 {HOOK_MAX_SEC}초")
    total = sum(durations) + OUTRO_DURATION_SEC
    stretchable = [i for i in range(len(durations)) if i != hook_index]
    while total < TARGET_MIN_SEC:
        room = [i for i in stretchable if durations[i] < SEG_MAX_SEC]
        if not room:
            break
        add = min((TARGET_MIN_SEC - total) / len(room), min(SEG_MAX_SEC - durations[i] for i in room))
        if add <= 1e-6:
            break
        for i in room:
            durations[i] += add
        total = sum(durations) + OUTRO_DURATION_SEC
    if total > config.VIDEO_MAX_SEC:
        raise RenderLengthError(f"총 길이 {total:.1f}초 > {config.VIDEO_MAX_SEC}초")
    return Timing(tuple(round(d, 3) for d in durations), round(tempo, 4), round(total, 3),
                  round(hook_tempo, 4))


def _run(cmd: list[str], log_path: Path) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n$ " + " ".join(cmd) + "\n")
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, check=False)
    if proc.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
        raise RenderError(f"ffmpeg 실패(code {proc.returncode}): {tail}")


def _font_opt(font: str | None) -> str:
    return f"fontfile='{font}':" if font else ""


def _cover_crop() -> str:
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"


def motion_expr(motion: str, frames: int) -> tuple[str, str, str]:
    """zoompan (z, x, y) 식. 알 수 없는 값은 zoom_in."""
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    progress = f"(on/{max(1, frames - 1)})"
    if motion == "zoom_out":
        return f"max({KEN_BURNS_MAX_ZOOM}-{KEN_BURNS_SPEED}*on,1.0)", center_x, center_y
    if motion == "pan_right":
        return f"{PAN_ZOOM}", f"(iw-iw/zoom)*{progress}", center_y
    if motion == "pan_left":
        return f"{PAN_ZOOM}", f"(iw-iw/zoom)*(1-{progress})", center_y
    if motion == "tilt_up":
        return f"{PAN_ZOOM}", center_x, f"(ih-ih/zoom)*(1-{progress})"
    if motion == "tilt_down":
        return f"{PAN_ZOOM}", center_x, f"(ih-ih/zoom)*{progress}"
    return f"min(1+{KEN_BURNS_SPEED}*on,{KEN_BURNS_MAX_ZOOM})", center_x, center_y


def _body_video_filter(duration: float, caption_file: Path, motion: str | bool, font: str | None) -> str:
    frames = max(1, int(duration * FPS))
    if isinstance(motion, bool):   # 이전 호출 호환: True=zoom_in, False=zoom_out
        motion = "zoom_in" if motion else "zoom_out"
    zoom, x, y = motion_expr(motion, frames)
    return (
        f"{_cover_crop()},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
        f"drawtext={_font_opt(font)}textfile='{caption_file.as_posix()}':expansion=none:"
        f"fontcolor=white:fontsize={SUB_FONT_SIZE}:borderw=6:bordercolor=black:line_spacing=12:"
        f"x=(w-text_w)/2:y=h*{SUB_Y_RATIO}-text_h/2,format=yuv420p"
    )


def _hook_video_filter(duration: float, caption_file: Path, font: str | None) -> str:
    frames = max(1, int(duration * FPS))
    pad_w, pad_h = int(W * HOOK_SHAKE_PAD), int(H * HOOK_SHAKE_PAD)
    off_x, off_y = (pad_w - W) // 2, (pad_h - H) // 2
    zoom = f"max({HOOK_PUNCH_START_ZOOM}-{HOOK_PUNCH_SPEED}*on,1.0)"
    return (
        f"scale={pad_w}:{pad_h}:force_original_aspect_ratio=increase,crop={pad_w}:{pad_h},"
        f"crop={W}:{H}:x='{off_x}+{HOOK_SHAKE_PX}*sin(2*PI*t*9)':y='{off_y}+{HOOK_SHAKE_PX}*cos(2*PI*t*11)',"
        f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
        f"drawtext={_font_opt(font)}textfile='{caption_file.as_posix()}':expansion=none:"
        f"fontcolor={HOOK_FONT_COLOR}:fontsize={HOOK_FONT_SIZE}:borderw=8:bordercolor=black:"
        f"line_spacing=14:x=(w-text_w)/2:y=h*{HOOK_TEXT_Y_RATIO}-text_h/2,format=yuv420p"
    )


def _audio_chain(duration: float, tempo: float, sfx: bool) -> str:
    tempo_part = f"atempo={tempo:.4f}," if abs(tempo - 1.0) > 1e-3 else ""
    chain = (f"[1:a]{tempo_part}aresample={SR},aformat=channel_layouts=stereo,"
             f"apad=whole_dur={duration:.3f}[n]")
    if sfx:
        return (chain + f";[2:a]aresample={SR},aformat=channel_layouts=stereo,volume=0.7[s]"
                ";[n][s]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    return chain + ";[n]anull[aout]"


def _encode_args(duration: float) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-maxrate", config.VIDEO_MAXRATE, "-bufsize", config.VIDEO_BUFSIZE, "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-c:a", "aac", "-b:a", config.AUDIO_BITRATE, "-ar", str(SR), "-ac", "2",
        "-t", f"{duration:.3f}",
    ]


def _render_scene(scene: SceneInput, duration: float, tempo: float, motion: bool | str,
                  out: Path, tmp: Path, font: str | None, log_path: Path) -> None:
    caption_file = tmp / f"{out.stem}.txt"
    wrap = HOOK_WRAP_CHARS if scene.is_hook else SUB_WRAP_CHARS
    caption_file.write_text(wrap_korean(scene.caption, wrap), encoding="utf-8")
    vf = (_hook_video_filter(duration, caption_file, font) if scene.is_hook
          else _body_video_filter(duration, caption_file, motion, font))
    cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{duration:.3f}", "-i", str(scene.image),
           "-i", str(scene.audio)]
    use_sfx = bool(scene.is_hook and scene.sfx)
    if use_sfx:
        cmd += ["-i", str(scene.sfx)]
    cmd += ["-filter_complex", f"[0:v]{vf}[vout];{_audio_chain(duration, tempo, use_sfx)}",
            "-map", "[vout]", "-map", "[aout]", *_encode_args(duration), str(out)]
    _run(cmd, log_path)


def _render_outro(background: Path, handle: str, out: Path, tmp: Path, font: str | None,
                  log_path: Path) -> None:
    frames = max(1, int(OUTRO_DURATION_SEC * FPS))
    zoom = f"min(1+{OUTRO_ZOOM_SPEED}*on,{OUTRO_MAX_ZOOM})"
    text_file = tmp / "outro.txt"
    text_file.write_text("\n".join(x for x in (handle, config.SHORTS_AI_NOTICE) if x), encoding="utf-8")
    base = (f"[0:v]{_cover_crop()},zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={W}x{H}:fps={FPS},"
            f"drawtext={_font_opt(font)}textfile='{text_file.as_posix()}':expansion=none:"
            "fontcolor=white:fontsize=48:borderw=5:bordercolor=black:line_spacing=18:"
            "x=(w-text_w)/2:y=h*0.84-text_h/2")
    cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{OUTRO_DURATION_SEC:.3f}", "-i", str(background),
           "-f", "lavfi", "-t", f"{OUTRO_DURATION_SEC:.3f}", "-i", f"anullsrc=r={SR}:cl=stereo"]
    logo = assets.find_logo()
    if logo:
        cmd += ["-i", str(logo)]
        logo_w = int(W * LOGO_WIDTH_RATIO)
        # 운영 베타 발견: 로고 PNG 가 RGBA 이지만 배경이 불투명 마젠타라 그대로 얹으면 마젠타 상자가 보였다.
        #   알파 유무와 무관하게 마젠타 키잉을 한다(투명 PNG 에는 영향 없음).
        logo_chain = (f"[2:v]format=rgba,colorkey={LOGO_CHROMA_KEY}:{LOGO_CHROMA_SIMILARITY}:"
                      f"{LOGO_CHROMA_BLEND},scale={logo_w}:-1[lg]")
        fc = (f"{base}[bg];{logo_chain};"
              f"[bg][lg]overlay=W-w-{LOGO_MARGIN_PX}:{LOGO_MARGIN_PX},format=yuv420p[vout]")
    else:
        fc = f"{base},format=yuv420p[vout]"
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "1:a",
            *_encode_args(OUTRO_DURATION_SEC), str(out)]
    _run(cmd, log_path)


def handle_from_x_url(x_url: str) -> str:
    """X_URL(https://x.com/name) → '@name'. 형식이 다르면 빈 문자열."""
    tail = (x_url or "").rstrip("/").rsplit("/", 1)[-1]
    return f"@{tail}" if tail and tail.replace("_", "").isalnum() else ""


def _concat_line(path: Path) -> str:
    """ffmpeg concat 목록 한 줄. 절대 경로 + 작은따옴표 이스케이프(공식 concat demuxer 인용 규칙)."""
    quoted = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{quoted}'\n"


def render(scenes: list[SceneInput], villain: str, out_path: Path, *,
           rng: random.Random | None = None, character: str | None = None) -> Timing:
    """장면들을 이어 붙여 out_path(mp4, faststart)를 만든다. 반환은 실제 적용한 타이밍."""
    if not scenes:
        raise RenderError("장면이 없습니다")
    rng = rng or random.Random()
    tmp = out_path.parent / "_segments"
    tmp.mkdir(parents=True, exist_ok=True)
    log_path = out_path.parent / "ffmpeg.log"
    font = find_kr_font()
    if not font:
        raise RenderError("한글 폰트를 찾지 못했습니다(fonts-noto-cjk 설치 필요) — 두부 자막 방지를 위해 중단")

    hook_index = next((i for i, s in enumerate(scenes) if s.is_hook), 0)
    timing = plan_timing([probe_duration(s.audio) for s in scenes], hook_index)
    log.info("타이밍 총 %.2f초 · 낭독 %.3f배 · 훅 추가 %.3f배 · 비트 %s", timing.total, timing.tempo,
             timing.hook_tempo, list(timing.durations))

    segments: list[Path] = []
    body_no = 0
    for idx, (scene, duration) in enumerate(zip(scenes, timing.durations, strict=True)):
        seg = tmp / f"seg_{idx:02d}.mp4"
        motion = BODY_MOTIONS[body_no % len(BODY_MOTIONS)]   # 비트별 카메라 움직임
        if not scene.is_hook:
            body_no += 1
        _render_scene(scene, duration, timing.scene_tempo(scene.is_hook), motion, seg, tmp, font, log_path)
        segments.append(seg)

    outro = tmp / "seg_outro.mp4"
    background = assets.find_cover(rng=rng, character=character) or scenes[-1].image
    _render_outro(background, handle_from_x_url(config.X_URL), outro, tmp, font, log_path)
    segments.append(outro)

    concat_list = tmp / "concat.txt"
    # concat demuxer 는 목록 안의 상대 경로를 '목록 파일 위치' 기준으로 푼다. 운영 출력 경로(out/shorts)는 상대 경로라
    #   상대 경로를 쓰면 out/shorts/<id>/_segments/out/shorts/... 로 겹쳐 실패한다(v1.8.0 운영 베타에서 발견) → 절대 경로.
    concat_list.write_text("".join(_concat_line(p) for p in segments), encoding="utf-8")
    joined = tmp / "joined.mp4"
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy",
          str(joined)], log_path)

    bgm = assets.find_bgm(villain, rng=rng)
    if bgm:
        fc = (f"[1:a]aresample={SR},aformat=channel_layouts=stereo,volume={BGM_VOLUME}[b];"
              f"[0:a][b]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{LOUDNORM},"
              f"aresample={SR}[aout]")
        cmd = ["ffmpeg", "-y", "-i", str(joined), "-stream_loop", "-1", "-i", str(bgm),
               "-filter_complex", fc, "-map", "0:v", "-map", "[aout]"]
    else:
        cmd = ["ffmpeg", "-y", "-i", str(joined), "-af", f"{LOUDNORM},aresample={SR}",
               "-map", "0:v", "-map", "0:a"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", config.AUDIO_BITRATE, "-ar", str(SR), "-ac", "2",
            "-t", f"{timing.total:.3f}", "-movflags", "+faststart", str(out_path)]
    _run(cmd, log_path)
    log.info("렌더 완료 %s (BGM=%s)", out_path.name, bgm.name if bgm else "없음")
    return timing

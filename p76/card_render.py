"""
p76/card_render.py
==================
휴재 콘텐츠 카드 이미지(1080×1350 PNG, 4:5)를 HTML 템플릿 + 헤드리스 Chrome 으로 렌더한다.
STD-QUAL-02: 이미지 안 글자는 AI 가 아니라 HTML 로 그린다. 외부 리소스·스크립트 없음.

Chrome 위치: PAGE76_CHROME_BIN → PATH 의 google-chrome / chromium (ubuntu-latest 러너 기본 포함).
한글 폰트: 러너에서 fonts-noto-cjk 설치 (워크플로).
"""
from __future__ import annotations

import html
import os
import shutil
import struct
import subprocess
from pathlib import Path

from p76 import settings

VERSION = "1.0.0"

TEMPLATE = Path(__file__).with_name("templates") / "card.html"
JPEG_MAGIC = bytes((0xFF, 0xD8))
_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


class RenderError(RuntimeError):
    """렌더 실패 (게시 전 확정 실패)."""


def find_chrome() -> str:
    if settings.CHROME_BIN:
        return settings.CHROME_BIN
    for name in _CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise RenderError("render:chrome_missing")


def build_html(label: str, headline: str, meta: str) -> str:
    page = TEMPLATE.read_text(encoding="utf-8")
    for key, value in (("label", label), ("headline", headline), ("meta", meta)):
        page = page.replace("{{" + key + "}}", html.escape(value or ""))
    return page


def png_size(path: str) -> tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise RenderError("render:not_png")
    return struct.unpack(">II", head[16:24])


def jpeg_size(path: str) -> tuple[int, int]:
    with open(path, "rb") as fh:
        data = fh.read(2 * 1024 * 1024)
    if data[:2] != JPEG_MAGIC:
        raise RenderError("image:not_jpeg")
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise RenderError("image:jpeg_no_sof")


def check_attachment(path: str) -> None:
    """첨부 이미지: PNG/JPEG · 10MB 이하 · 4:5(±2%)."""
    if os.path.getsize(path) > settings.PHOTO_MAX_BYTES:
        raise RenderError("image:too_large")
    with open(path, "rb") as fh:
        magic = fh.read(2)
    w, h = jpeg_size(path) if magic == JPEG_MAGIC else png_size(path)
    if h <= 0 or abs(w / h - 0.8) > 0.02:
        raise RenderError(f"image:aspect:{w}x{h}")


def check_card(path: str) -> None:
    if os.path.getsize(path) > settings.PHOTO_MAX_BYTES:
        raise RenderError("render:too_large")
    if png_size(path) != settings.CARD_SIZE:
        raise RenderError("render:size")


def render(label: str, headline: str, meta: str, work_dir: str, runner=subprocess.run) -> str:
    """카드 PNG 경로. 실패하면 RenderError."""
    src = Path(work_dir) / "card.html"
    out = Path(work_dir) / "card.png"
    src.write_text(build_html(label, headline, meta), encoding="utf-8")
    width, height = settings.CARD_SIZE
    cmd = [
        find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
        "--force-device-scale-factor=1", "--default-background-color=00000000",
        f"--window-size={width},{height}", f"--screenshot={out}", src.resolve().as_uri(),
    ]
    try:
        proc = runner(cmd, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RenderError(f"render:chrome_failed:{type(e).__name__}") from None
    if proc.returncode != 0 or not out.exists():
        raise RenderError(f"render:chrome_exit_{proc.returncode}")
    check_card(str(out))
    return str(out)

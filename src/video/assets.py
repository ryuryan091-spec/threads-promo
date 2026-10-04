"""숏폼 연출 자산 찾기 — BGM · 훅 효과음 · 아웃트로 표지/로고 · 캐릭터 참조 이미지.

모두 opt-in 이다. 파일이 없으면 None/빈 목록을 돌려주고 해당 연출 없이 렌더한다.
코드가 음원·이미지를 자동으로 받아오지 않는다(저작권 리스크 없음).
파일 규칙은 investment_comic_tube 렌더러와 같다.
  bgm/       bgm_<빌런슬러그>_*.{mp3,m4a,wav,aac,ogg,mp4} · bgm_common*
  sfx/       hook_a ~ hook_d
  brand/     logo.* = 오버레이 전용, 나머지 이미지 = 아웃트로 표지 후보
  reference/edt/ · reference/goc/  캐릭터별 외형 참조 이미지(이미지 생성 요청에 함께 넣는다)
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

VERSION = "1.0.1"   # v1.8.0 베타: 아웃트로 표지를 캐릭터별로 고른다

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2] / "assets" / "video"
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".mp4"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
LOGO_STEM = "logo"
BGM_COMMON_PREFIX = "bgm_common"
BGM_VILLAIN_SLUG = {
    "Debt Titan": "debt_titan",
    "Chaos Reaper": "chaos_reaper",
    "Bull Brute": "bull_brute",
}


def _files(sub: str, extensions: set[str], root: Path | None = None) -> list[Path]:
    directory = (root or ROOT) / sub
    if not directory.is_dir():
        return []
    return [p for p in sorted(directory.iterdir()) if p.is_file() and p.suffix.lower() in extensions]


def find_bgm(villain: str | None, *, rng: random.Random | None = None,
             root: Path | None = None) -> Path | None:
    """빌런 전용 곡 → 공통 곡 → 아무 곡. 없으면 None."""
    rng = rng or random.Random()
    candidates = _files("bgm", AUDIO_EXTENSIONS, root)
    if not candidates:
        return None
    slug = BGM_VILLAIN_SLUG.get(villain or "")
    if slug:
        matched = [p for p in candidates if p.stem.startswith(f"bgm_{slug}")]
        if matched:
            return rng.choice(matched)
    common = [p for p in candidates if p.stem.startswith(BGM_COMMON_PREFIX)]
    if common:
        return rng.choice(common)
    return rng.choice(candidates)


def find_sfx(name: str | None, *, root: Path | None = None) -> Path | None:
    """훅 효과음. 이름이 정확히 맞는 파일만 쓴다. 없으면 None(무음 훅)."""
    if not name:
        return None
    for path in _files("sfx", AUDIO_EXTENSIONS, root):
        if path.stem == name:
            return path
    return None


def find_logo(*, root: Path | None = None) -> Path | None:
    logos = [p for p in _files("brand", IMAGE_EXTENSIONS, root) if p.stem.lower() == LOGO_STEM]
    return logos[0] if logos else None


def find_cover(*, rng: random.Random | None = None, root: Path | None = None,
               character: str | None = None) -> Path | None:
    """아웃트로 표지. character 를 주면 그 캐릭터 영상에 맞는 표지만 고른다(v1.8.0 운영 베타 발견).

    - GOC: 파일 이름에 'goc' 가 들어간 표지만(Facebook 은 GOC 만 등장 — 마스터 결정 2026-10-04).
      없으면 None → 렌더러가 마지막 장면(GOC 생성 이미지)을 쓴다.
    - 그 밖(EDT): 'goc' 가 들어가지 않은 표지.
    - None: 기존 동작(로고가 아닌 모든 이미지).
    """
    rng = rng or random.Random()
    covers = [p for p in _files("brand", IMAGE_EXTENSIONS, root) if p.stem.lower() != LOGO_STEM]
    if character is not None:
        is_goc = character.upper() == "GOC"
        covers = [p for p in covers if ("goc" in p.stem.lower()) == is_goc]
    return rng.choice(covers) if covers else None


def reference_images(character: str = "EDT", *, root: Path | None = None) -> list[Path]:
    """캐릭터별 참조 이미지(reference/<캐릭터 소문자>/). 다른 캐릭터 참조가 섞이지 않게 폴더를 나눈다."""
    return _files(f"reference/{character.lower()}", IMAGE_EXTENSIONS, root)

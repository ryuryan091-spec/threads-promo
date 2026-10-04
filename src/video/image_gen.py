"""장면 이미지 생성 — Gemini(GEMINI_API_SUB_PAY_KEY) generate_content + 이미지 모델.

investment_comic_tube image_generator 와 같은 방식이다.
  - Imagen(generate_images)이 아니라 generate_content 응답의 inline_data(바이트)를 받는다.
  - assets/video/reference 이미지가 있으면 요청에 함께 넣어 EDT 외형을 고정한다
    (참조 이미지는 입력 토큰 추가 과금 — YouTube 코드 주석 기준 장당 약 1,120 토큰).
  - 이미지 안에 글자·숫자·로고를 넣지 않게 강제한다(자막은 렌더러가 따로 그린다).
세로 9:16 은 프롬프트로 요구하고, 비율이 달라도 렌더러가 확대 후 가운데를 잘라 맞춘다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import config
from . import assets

VERSION = "1.0.0"

log = logging.getLogger(__name__)

# 외형 묘사는 investment_comic_tube 테스트 픽스처(episode_sample.json)의 EDT 프롬프트에서 옮겼다.
HERO_APPEARANCE = (
    "The hero EDT is an anthropomorphic tiger warrior with vibrant orange and black striped fur, "
    "wearing battle-worn dark steel and aged bronze Roman plate armor with a dark red cape, "
    "eyes glowing platinum-white, holding a massive industrial metal chainsaw, gold-white energy "
    "flowing from the armor seams and a 'D' chest emblem. Armor is dark steel and bronze only."
)
VILLAIN_APPEARANCE = {
    "Debt Titan": "Villain: Debt Titan, a colossal faceless horned obsidian rock construct bound in heavy chains.",
    "Chaos Reaper": "Villain: Chaos Reaper, a shadowy scythe-wielding storm entity made of jagged dark energy.",
    "Bull Brute": "Villain: Bull Brute, a hulking overheated bull-like brute radiating red-hot steam.",
}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class ImageGenError(RuntimeError):
    """이미지를 한 장도 만들지 못했다."""


def build_prompt(scene: str, villain: str, has_reference: bool) -> str:
    return (
        "Vertical 9:16 comic-style illustration for a financial-market story called 'EDT Universe'. "
        f"{HERO_APPEARANCE} {VILLAIN_APPEARANCE.get(villain, '')} "
        f"Scene to depict: {scene} "
        "High contrast manhwa shading, cinematic lighting, dynamic camera angle. "
        + ("Use the provided reference image(s) as the definitive look of the hero EDT. "
           if has_reference else "")
        + "CRITICAL: absolutely NO text, NO letters, NO words, NO numbers, NO captions, NO speech "
        "bubbles, NO signage, NO watermarks and NO logos of any kind. Exactly one continuous scene "
        "from one camera viewpoint, no split panels, no blank bands or empty margins."
    )


def _extract_image(response) -> bytes | None:
    for cand in getattr(response, "candidates", None) or []:
        parts = getattr(getattr(cand, "content", None), "parts", None) or []
        for part in parts:
            data = getattr(getattr(part, "inline_data", None), "data", None)
            if data:
                return data
    return None


def generate_scenes(
    api_key: str, prompts: list[str], villain: str, out_dir: Path, *, client=None
) -> list[Path | None]:
    """장면마다 1장. 실패한 장면은 None(렌더러가 다른 장면 이미지로 대체). 전부 실패면 ImageGenError."""
    from google import genai
    from google.genai import types

    client = client or genai.Client(api_key=api_key)
    refs = assets.reference_images()
    ref_parts = [
        types.Part.from_bytes(data=p.read_bytes(), mime_type=MIME.get(p.suffix.lower(), "image/png"))
        for p in refs
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path | None] = []
    for idx, scene in enumerate(prompts):
        prompt = build_prompt(scene, villain, bool(ref_parts))
        data = None
        for attempt in (1, 2):
            try:
                resp = client.models.generate_content(
                    model=config.SHORTS_IMAGE_MODEL, contents=[*ref_parts, prompt]
                )
                data = _extract_image(resp)
            except Exception as exc:  # noqa: BLE001 — 외부 API 실패는 장면 단위로 격리
                log.warning("이미지 생성 실패 scene=%d attempt=%d: %s", idx, attempt, exc)
                continue
            if data:
                break
            log.warning("이미지 응답에 이미지 없음 scene=%d attempt=%d", idx, attempt)
        if not data:
            paths.append(None)
            continue
        path = out_dir / f"scene_{idx}.png"
        path.write_bytes(data)
        paths.append(path)
    ok = sum(1 for p in paths if p)
    log.info("이미지 생성 %d/%d 모델=%s 참조=%d", ok, len(prompts), config.SHORTS_IMAGE_MODEL, len(refs))
    if ok == 0:
        raise ImageGenError("장면 이미지를 한 장도 만들지 못했습니다")
    return paths

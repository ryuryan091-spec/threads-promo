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

VERSION = "1.2.0"   # v1.8.4: 구도 7종 · v1.8.3: 장면별 구도·자세 지시, 참조는 외형만

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
# GOC 외형·지시문은 investment_comic_tube image_generator.py 의 GOC 트랙 문구와 같다.
GOC_APPEARANCE = (
    "Guardian of Capital (GOC), the capital-protection heroine. "
    "Use the supplied reference as the exact design: human face and human ears, long blonde hair, "
    "blue eyes, ornate white-and-gold armor, large white feathered wings, and dark red cape. "
    "Preserve the reference's equipment and left/right arrangement; do not invent a weapon."
)
# v1.8.3 운영 베타 C3: 5장이 모두 참조 이미지와 같은 정면 전신 자세로 나왔다.
#   장면마다 다른 카메라·자세를 지시하고, 참조 이미지는 외형(얼굴·머리·갑옷·색)에만 쓰게 한다.
SHOT_DIRECTIVES: tuple[str, ...] = (
    "extreme close-up of the face and shoulders, intense eyes, shallow depth of field",
    "very wide establishing shot, the character small in the frame against a vast sky and city",
    "dynamic mid-action pose flying or leaping diagonally across the frame, motion blur, wind",
    "view from behind over the shoulder, looking out over the scene, cape and hair blowing",
    "dramatic low-angle shot from below, three-quarter view, strong rim light",
    "side profile medium shot, walking or gliding forward with determination, background in soft focus",
    "top-down bird's-eye view from high above, the character seen from overhead against the landscape",
)
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class ImageGenError(RuntimeError):
    """이미지를 한 장도 만들지 못했다."""


def shot_directive(index: int | None) -> str:
    if index is None:
        return ""
    return (f"Camera and pose for this image: {SHOT_DIRECTIVES[index % len(SHOT_DIRECTIVES)]}. "
            "Do NOT use a static front-facing standing pose. ")


def build_prompt(scene: str, villain: str | None, has_reference: bool,
                 character: str = config.CHARACTER_EDT, shot_index: int | None = None) -> str:
    shot = shot_directive(shot_index)
    if character == config.CHARACTER_GOC:
        # Facebook 영상: GOC 단독. EDT·빌런·다른 인물을 그리지 않게 명시한다.
        return (
            "Vertical 9:16 comic-style illustration for a financial-market story called 'EDT Universe'. "
            f"{GOC_APPEARANCE} "
            f"Scene to depict from GOC's protective perspective: {scene or 'GOC assesses market risk.'} "
            + shot
            + "GOC is the only character in the image: no other heroes, no villains, no animals, no crowds. "
            "High contrast manhwa shading, cinematic lighting, dynamic camera angle. "
            + ("Use the provided reference ONLY to keep GOC's identity (face, hair, armor, wings, colors); "
               "do NOT copy the reference's pose, framing, camera angle or background. " if has_reference else "")
            + "CRITICAL: absolutely NO text, NO letters, NO words, NO numbers, NO captions, NO speech "
            "bubbles, NO signage, NO watermarks and NO logos of any kind. Exactly one continuous scene "
            "from one camera viewpoint, no split panels, no blank bands or empty margins."
        )
    return (
        "Vertical 9:16 comic-style illustration for a financial-market story called 'EDT Universe'. "
        f"{HERO_APPEARANCE} {VILLAIN_APPEARANCE.get(villain, '')} "
        f"Scene to depict: {scene} "
        + shot
        + "High contrast manhwa shading, cinematic lighting, dynamic camera angle. "
        + ("Use the provided reference image(s) ONLY to keep the hero EDT's identity (face, fur, armor, "
           "colors); do NOT copy the reference's pose, framing, camera angle or background. "
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
    api_key: str, prompts: list[str], villain: str | None, out_dir: Path, *, client=None,
    character: str = config.CHARACTER_EDT,
) -> list[Path | None]:
    """장면마다 1장. 실패한 장면은 None(렌더러가 다른 장면 이미지로 대체). 전부 실패면 ImageGenError."""
    from google import genai
    from google.genai import types

    client = client or genai.Client(api_key=api_key)
    refs = assets.reference_images(character)
    ref_parts = [
        types.Part.from_bytes(data=p.read_bytes(), mime_type=MIME.get(p.suffix.lower(), "image/png"))
        for p in refs
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path | None] = []
    for idx, scene in enumerate(prompts):
        prompt = build_prompt(scene, villain, bool(ref_parts), character, shot_index=idx)
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
    log.info("이미지 생성 %d/%d 캐릭터=%s 모델=%s 참조=%d", ok, len(prompts), character,
             config.SHORTS_IMAGE_MODEL, len(refs))
    if ok == 0:
        raise ImageGenError("장면 이미지를 한 장도 만들지 못했습니다")
    return paths

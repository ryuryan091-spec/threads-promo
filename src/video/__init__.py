"""v1.7.0 60초 숏폼 영상 파이프라인 (DESIGN_V17_SHORTS.md).

대본(Claude) → 이미지(Gemini) → 음성(Gemini TTS) → 렌더(ffmpeg) → 규격 검사(ffprobe).
연출 파라미터는 investment_comic_tube(YouTube) 운영 렌더러에서 확인한 값을 따른다.
"""

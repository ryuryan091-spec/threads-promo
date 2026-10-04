"""숏폼 build job (v1.7.0) — 오늘 영상 만들기 + 텔레그램 미리보기. Meta 에는 아무것도 쓰지 않는다.

흐름
  1) 오늘 계획(shorts_plan.daily_plan): SHORTS_BUILD_ENABLED · 휴식일 · 램프 · 채널 스위치
  2) 시장 분위기 근거(mood_source — CHAT 과 같은 소스, 수치·기업명 없는 테마만)
  2-1) (v1.8.0, FACE_STORY_ENABLED) Facebook 회차 원장 읽기 — 게시완료 회차의 지난 이야기·열린 떡밥.
        원장 쓰기는 하지 않는다(publish 가 게시 결과로 쓴다). 조회 실패면 원장 없이 진행 + 알림.
  3) 편마다: 대본(Claude) → 이미지(Gemini) → 음성(Gemini TTS) → 렌더 → 규격 검사
  4) manifest.json + 영상 → artifact(워크플로가 업로드) · 텔레그램 미리보기
편 하나가 실패해도 다른 편은 계속한다. 정적 폴백 영상은 만들지 않는다.

종료코드: 0 정상(만들 것 없음 포함) · 2 전부 실패 · 3 설정 누락
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import chat_plan, config, face_story, mood_source, notifier, shorts_plan
from .video import image_gen, renderer, script_writer, tts, validator

VERSION = "1.2.0"   # v1.8.5: 미리보기에 대본 문체 경고 표시 · v1.8.0: Facebook 회차 원장 읽기 · 연속성 계약

KST = ZoneInfo("Asia/Seoul")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("shorts-build")
for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore", "google_genai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

MANIFEST = "manifest.json"


def out_dir() -> Path:
    return Path(os.environ.get("SHORTS_OUT_DIR", "out/shorts"))


def _notify(message: str) -> None:
    notifier.send(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_ALERT_CHAT_ID", ""),
                  message)


def build_one(item: shorts_plan.PlannedVideo, mood: mood_source.Mood, *, claude_key: str,
              gemini_key: str, used_types: set[str], used_hooks: list[str], base: Path,
              used_captions: set[str] | None = None,
              story: face_story.StoryContext | None = None) -> dict:
    """영상 1편. 성공하면 manifest 항목을 돌려주고, 실패하면 예외를 올린다.

    story: v1.8.0 Facebook 회차 원장 맥락. Facebook 편에만 연속성 계약을 건다(None 이면 v1.7.0 동작).
    """
    req = face_story.request_for(story, item.fmt) if shorts_plan.CHANNEL_FACE in item.channels else None
    work = base / item.content_id
    work.mkdir(parents=True, exist_ok=True)
    script = script_writer.write_script(
        claude_key, content_id=item.content_id, fmt=item.fmt, mood=mood,
        used_hook_types=used_types, used_hooks=used_hooks, used_captions=used_captions,
        character=item.character, continuity=req,
    )
    (work / "script.json").write_text(json.dumps(script.to_dict(), ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    images = image_gen.generate_scenes(gemini_key, list(script.image_prompts), script.villain,
                                       work / "images", character=item.character)
    usable = [p for p in images if p]
    video = work / "video.mp4"
    try:
        timing = _voice_and_render(script, images, usable, gemini_key, work, video)
    except renderer.RenderLengthError as exc:
        # 점검 2026-10-04: 낭독이 길어 60초에 못 넣으면 대본만 1회 다시 만든다(이미지는 재사용 — 비용 절감).
        log.warning("길이 초과 — 대본을 짧게 1회 재생성합니다: %s", exc)
        script = script_writer.write_script(
            claude_key, content_id=item.content_id, fmt=item.fmt, mood=mood,
            used_hook_types=used_types, used_hooks=used_hooks, used_captions=used_captions,
            extra_instruction=(f"직전 대본은 낭독하면 60초를 넘었다({exc}). 내레이션 합계를 "
                               f"{script_writer.TOTAL_MIN_CHARS}자에 가깝게 줄이고 훅은 짧게 쓴다."),
            character=item.character, continuity=req,
        )
        (work / "script.json").write_text(json.dumps(script.to_dict(), ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        timing = _voice_and_render(script, images, usable, gemini_key, work, video)
    issues = validator.check(video)
    if issues:
        raise renderer.RenderError(f"규격 검사 실패: {issues}")
    used_types.add(script.hook_type)
    used_hooks.append(script.beats[0].narration)
    entry = {
        "content_id": item.content_id,
        "character": item.character,
        "fmt": item.fmt,
        "channels": list(item.channels),
        "caption": script.caption,
        "video": f"{item.content_id}/video.mp4",
        "villain": script.villain,
        "hook_type": script.hook_type,
        "duration": timing.total,
        "images_ok": len(usable),
    }
    if script.warnings:
        entry["script_warnings"] = list(script.warnings)
    if req is not None and story is not None and script.continuity is not None:
        entry["themes"] = list(script.themes)
        entry["continuity"] = face_story.build_continuity(script.continuity, req, story, item.content_id)
    return entry


PREVIEW_WARN_MAX = 3      # 텔레그램 1000자 안에 캡션·승인 안내가 남도록 경고 줄 수를 제한한다
PREVIEW_WARN_CHARS = 120


def preview_caption(entry: dict) -> str:
    """텔레그램 미리보기 캡션. 문체 경고는 캡션 앞에 둔다 — 텔레그램 캡션은 1000자에서 잘린다(notifier.send_video)."""
    head = (f"[Shorts 미리보기] {entry['content_id']} {entry.get('character', '')} {entry['fmt']} → "
            f"{'/'.join(entry['channels'])}\n")
    warns = entry.get("script_warnings") or []
    warn = ""
    if warns:
        shown = [w[:PREVIEW_WARN_CHARS] for w in warns[:PREVIEW_WARN_MAX]]
        more = f"\n- 외 {len(warns) - PREVIEW_WARN_MAX}건" if len(warns) > PREVIEW_WARN_MAX else ""
        warn = ("⚠ 문체 검토 필요(1인칭·전언형 자동 검사 미통과 — 승인 전 확인):\n- "
                + "\n- ".join(shown) + more + "\n\n")
    return f"{head}{warn}{entry['caption']}\n\n승인: Actions › 📘🧵 Meta Shorts › Review deployments"

def _voice_and_render(script, images, usable, gemini_key: str, work: Path, video: Path):
    """TTS → 장면 조립 → 렌더. 길이 초과면 RenderLengthError 를 그대로 올린다."""
    lines = [b.narration for b in script.beats]
    audios = tts.synthesize(gemini_key, lines, [b.tone for b in script.beats], work / "audio")
    scenes = []
    for idx, beat in enumerate(script.beats):
        slot = script_writer.BEAT_IMAGE_SLOT[idx]
        image = images[slot] if slot < len(images) and images[slot] else usable[slot % len(usable)]
        scenes.append(renderer.SceneInput(
            image=image, audio=audios[idx], caption=beat.narration, is_hook=beat.is_hook,
            sfx=renderer.assets.find_sfx(beat.sfx) if beat.is_hook else None,
        ))
    return renderer.render(scenes, script.villain, video, character=script.character)


def _load_story(plan: list[shorts_plan.PlannedVideo], today: dt.date) -> face_story.StoryContext | None:
    """v1.8.0 Facebook 회차 원장 맥락. 꺼져 있거나 Facebook 편이 없으면 None(v1.7.0 동작)."""
    if not config.FACE_STORY_ENABLED:
        return None
    if not any(shorts_plan.CHANNEL_FACE in p.channels for p in plan):
        return None
    ctx = face_story.load_context(os.environ.get("NOTION_TOKEN", "").strip(), config.FACE_NOTION_DB_ID,
                                  today=today)
    if not ctx.available:
        _notify(f"[Shorts] Facebook 회차 원장을 읽지 못해 지난 이야기 없이 만듭니다 — {ctx.error[:200]}")
    return ctx


def run() -> int:
    log.info("[ShortsBuild] v%s 시작 (config v%s)", VERSION, config.VERSION)
    now = dt.datetime.now(KST)
    today = now.date()
    base = out_dir()
    base.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"date": today.isoformat(), "items": [], "failed": []}

    plan = shorts_plan.daily_plan(today)
    if not plan:
        reasons = []
        if not config.SHORTS_BUILD_ENABLED:
            reasons.append("SHORTS_BUILD_ENABLED=false")
        elif shorts_plan.is_rest_day(today):
            reasons.append("주간 휴식일")
        else:
            reasons.append("오늘 게시할 채널 없음(FACE 램프 0편 · Threads 숏폼 꺼짐/차단)")
        log.info("오늘 만들 영상 없음 — %s", ", ".join(reasons))
        (base / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    claude_key = os.environ.get("CLAUDE_AI_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_SUB_PAY_KEY", "").strip()
    missing = [n for n, v in (("CLAUDE_AI_KEY", claude_key), ("GEMINI_API_SUB_PAY_KEY", gemini_key)) if not v]
    if missing:
        log.error("필수 Secret 누락: %s", missing)
        _notify(f"[Shorts] 영상 생성 중단 — Secret 누락 {missing}")
        return 3

    log.info("오늘 계획 %d편: %s", len(plan),
             ", ".join(f"{p.content_id}({p.character} {p.fmt}→{'/'.join(p.channels)})" for p in plan))
    first = chat_plan.source_for(today, None, config.CHAT_SOURCE_MODE)
    mood = mood_source.collect(first, api_key=claude_key, today=today, now=now.astimezone(dt.UTC))
    log.info("근거 소스=%s 테마=%s 분위기=%s", mood.source, ",".join(mood.themes) or "-", mood.mood_word or "-")

    story = _load_story(plan, today)
    used_types: set[str] = set()
    used_hooks: list[str] = []
    used_captions: set[str] = set()
    for item in plan:
        try:
            entry = build_one(item, mood, claude_key=claude_key, gemini_key=gemini_key,
                              used_types=used_types, used_hooks=used_hooks, base=base,
                              used_captions=used_captions, story=story)
        except Exception as exc:  # noqa: BLE001 — 편 단위 격리
            log.exception("영상 생성 실패 %s", item.content_id)
            manifest["failed"].append({"content_id": item.content_id, "reason": str(exc)[:300]})
            continue
        manifest["items"].append(entry)
        log.info("영상 완료 %s %.1f초", entry["content_id"], entry["duration"])

    (base / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    bot, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
    for entry in manifest["items"]:
        notifier.send_video(bot, chat, base / entry["video"], preview_caption(entry))
    if manifest["failed"]:
        _notify("[Shorts] 일부 영상 생성 실패\n" + "\n".join(
            f"- {f['content_id']}: {f['reason'][:150]}" for f in manifest["failed"]))
    if not manifest["items"]:
        log.error("오늘 영상을 한 편도 만들지 못했습니다")
        return 2
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 — 최상위 방어
        log.exception("예기치 못한 오류")
        _notify(f"[Shorts] 영상 생성 예기치 못한 오류\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

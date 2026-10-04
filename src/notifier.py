"""운영 알림. 실패해도 본 파이프라인을 중단시키지 않는다."""

from __future__ import annotations

import logging

import requests

from . import config
from .redact import redact

VERSION = "1.2.0"   # v1.2.0: 영상 미리보기 전송(send_video) — v1.7.0 숏폼
# v1.1.0: 발송 전 자격증명 마스킹

log = logging.getLogger(__name__)


def send(bot_token: str, chat_id: str, message: str) -> None:
    # 알림 본문에는 예외 문자열이 그대로 실린다. 텔레그램은 Actions 마스킹 대상이 아니다.
    message = redact(message)
    if not bot_token or not chat_id:
        log.warning("텔레그램 설정 없음 — 알림 생략: %s", message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=config.HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        # 예외 문자열에 봇 토큰이 든 URL 이 포함된다.
        log.warning("알림 전송 실패: %s", redact(exc))


# Telegram Bot API 업로드 상한(50MB). 넘으면 영상 대신 안내 문구만 보낸다.
TELEGRAM_VIDEO_MAX_BYTES = 50 * 1024 * 1024


def send_video(bot_token: str, chat_id: str, video_path, caption: str) -> bool:
    """영상 미리보기 전송(v1.7.0). 실패해도 본 파이프라인을 막지 않는다. 성공 여부를 돌려준다."""
    caption = redact(caption)[:1000]
    if not bot_token or not chat_id:
        log.warning("텔레그램 설정 없음 — 영상 미리보기 생략")
        return False
    try:
        size = video_path.stat().st_size
    except OSError as exc:
        log.warning("미리보기 파일 확인 실패: %s", exc)
        return False
    if size > TELEGRAM_VIDEO_MAX_BYTES:
        send(bot_token, chat_id, f"{caption}\n(영상 {size:,}바이트 — 텔레그램 상한 초과, Actions artifact 확인)")
        return False
    try:
        with video_path.open("rb") as fh:
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendVideo",
                data={"chat_id": chat_id, "caption": caption, "supports_streaming": "true"},
                files={"video": (video_path.name, fh, "video/mp4")},
                timeout=config.HTTP_TIMEOUT_SEC * 6,
            )
        if resp.status_code != 200:
            log.warning("영상 미리보기 전송 실패 %s: %s", resp.status_code, redact(resp.text[:200]))
            return False
        return True
    except requests.RequestException as exc:
        log.warning("영상 미리보기 전송 실패: %s", redact(exc))
        return False

"""운영 알림. 실패해도 본 파이프라인을 중단시키지 않는다."""

from __future__ import annotations

import logging

import requests

from . import config

log = logging.getLogger(__name__)


def send(bot_token: str, chat_id: str, message: str) -> None:
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
        log.warning("알림 전송 실패: %s", exc)

"""운영 알림. 실패해도 본 파이프라인을 중단시키지 않는다."""

from __future__ import annotations

import logging

import requests

from . import config
from .redact import redact

VERSION = "1.1.0"   # v1.1.0: 발송 전 자격증명 마스킹

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

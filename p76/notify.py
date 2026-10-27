"""
p76/notify.py
=============
알림. 회장 전용 Telegram 대화방만 사용한다 (STD-OBS-06 — 투자 채널 사용 금지).
미설정이면 로그만 남긴다. 알림 실패는 실행 결과에 영향을 주지 않는다.
본문·댓글·토큰·ID 는 넣지 않는다 (날짜·회차·상태·코드만).

사용: python -m p76.notify --message "내용"
"""
from __future__ import annotations

import argparse
import logging

import requests

from p76 import settings
from p76.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
PREFIX = "[사건파일76]"


def send(message: str, urgent: bool = False) -> bool:
    text = redact(f"{PREFIX}{'[최우선]' if urgent else ''} {message}")[:1000]
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.info(f"[Notify] 알림 대상 미설정 — 로그만 기록: {text}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"[Notify] 전송 실패: {type(e).__name__}")
        return False
    if resp.status_code != 200:
        logger.warning(f"[Notify] 전송 실패 http_{resp.status_code}")
        return False
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", required=True)
    send(parser.parse_args().message)

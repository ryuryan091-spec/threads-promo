"""
p76/novel_tg.py
===============
사건 소설 텔레그램 알림 — 보내기만 한다 (DN2026_0072, D-N9 대체).

  send()          알림 (dry_run 은 로그만). 자격증명은 가리고, 노션 링크는 회장 운영 방이라 남긴다.

threads-promo 운영 알림 봇·방을 함께 쓴다(워크플로가 TELEGRAM_BOT_TOKEN · TELEGRAM_ALERT_CHAT_ID 를 넣는다).
getUpdates·웹훅은 쓰지 않는다 — 같은 봇을 쓰는 다른 파이프라인(investment-os 승인·보류 콜백)의 update 를
건드리지 않기 위해서다. 중단은 노션 DB-12 중단 체크와 GitHub Variable 로만 한다.
공개 Actions 로그에는 템플릿 ID·결과만 남긴다.
"""
from __future__ import annotations

import logging

import requests

from p76 import settings
from p76.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
PREFIX = "[사건파일76·소설] "
API = "https://api.telegram.org/bot{token}/{method}"


def configured() -> bool:
    return bool(settings.NOVEL_TG_BOT_TOKEN and settings.NOVEL_TG_CHAT_ID)


def page_link(page_id: str) -> str:
    pid = (page_id or "").replace("-", "")
    return f"https://www.notion.so/{pid}" if pid and not pid.startswith("dry") else ""


def send(template: str, text: str, link: str = "") -> bool:
    """link 는 마스킹 뒤에 붙인다(노션 ID 형식이라 redact 가 가리기 때문)."""
    if settings.MODE == "dry_run" or not configured():
        logger.info(f"[NovelTG] {template} 발송 생략 (mode={settings.MODE}, configured={configured()})")
        return False
    body = PREFIX + redact(text, include_ids=False)[:3500] + (f"\n{link}" if link else "")
    try:
        resp = requests.post(
            API.format(token=settings.NOVEL_TG_BOT_TOKEN, method="sendMessage"),
            json={"chat_id": settings.NOVEL_TG_CHAT_ID, "text": body, "disable_web_page_preview": True},
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"[NovelTG] {template} 전송 실패: {type(e).__name__}")
        return False
    if resp.status_code != 200:
        logger.warning(f"[NovelTG] {template} 전송 실패 http_{resp.status_code}")
        return False
    logger.info(f"[NovelTG] {template} 전송")
    return True


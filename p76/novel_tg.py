"""
p76/novel_tg.py
===============
사건 소설 전용 텔레그램 봇·비공개 채널 (D-N9, DSN-P76-NOVEL-002 §7).

  send()          채널에 알림 (dry_run 은 로그만). 자격증명은 가리고, 노션 링크는 비공개 채널이라 남긴다.
  poll_stop()     getUpdates 로 채널 글을 읽어 중단 명령이 있었는지 확인. 새 offset 을 돌려준다.

채널 관리자는 회장과 봇 둘뿐이다 → 지정 채널의 channel_post = 회장 글로 본다.
재개 명령은 두지 않는다(재개는 노션에서만). 웹훅은 쓰지 않는다(getUpdates 와 충돌).
공개 Actions 로그에는 템플릿 ID·결과만 남긴다.
"""
from __future__ import annotations

import logging

import requests

from p76 import settings
from p76.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
STOP_WORDS = ("/p76_stop", "중단")
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
    body = redact(text, include_ids=False)[:3500] + (f"\n{link}" if link else "")
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


def is_stop(text: str) -> bool:
    word = (text or "").strip().split("@")[0].strip().lower()
    return word in STOP_WORDS


def poll_stop(offset: int) -> tuple[bool, int, bool]:
    """(중단 명령 있음, 새 offset, 조회 성공). offset = 마지막으로 처리한 update_id."""
    if not configured():
        return False, offset, False
    try:
        resp = requests.get(
            API.format(token=settings.NOVEL_TG_BOT_TOKEN, method="getUpdates"),
            params={"offset": offset + 1, "timeout": 0, "allowed_updates": '["channel_post","message"]'},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"[NovelTG] 중단 명령 조회 실패: {type(e).__name__}")
        return False, offset, False
    if resp.status_code != 200:
        logger.warning(f"[NovelTG] 중단 명령 조회 실패 http_{resp.status_code}")
        return False, offset, False
    try:
        updates = resp.json().get("result") or []
    except ValueError:
        return False, offset, False
    stop, newest = False, offset
    for update in updates:
        newest = max(newest, int(update.get("update_id", 0)))
        post = update.get("channel_post") or update.get("message") or {}
        chat_id = str((post.get("chat") or {}).get("id", ""))
        if chat_id != str(settings.NOVEL_TG_CHAT_ID):
            continue
        if (post.get("from") or {}).get("is_bot"):
            continue
        if is_stop(post.get("text", "")):
            stop = True
    if stop:
        logger.warning("[NovelTG] 중단 명령 수신")
    return stop, newest, True

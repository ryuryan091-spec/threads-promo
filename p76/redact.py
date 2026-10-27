"""
p76/redact.py
=============
로그·알림·결과 문자열에서 토큰·식별자를 가린다 (SEC C-07).
  - access_token= · Authorization · EAA 토큰 · 노션 토큰 · 텔레그램 봇 토큰 패턴
  - 실행 설정의 토큰·페이지 ID·DB ID·chat_id 값 자체
  - UUID 형식(노션 페이지·DB ID)
Actions 에서는 시작 시 register_masks() 가 ::add-mask:: 를 내보내 러너 로그에서도 가린다.
"""
from __future__ import annotations

import os
import re
from typing import Any

from p76 import settings

VERSION = "1.0.0"

MASK = "***"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(access_token|input_token)=[^&\s'\"<>]+"), rf"\1={MASK}"),
    (re.compile(r"(?i)(\"?access_token\"?\s*:\s*\")[^\"]+"), rf"\1{MASK}"),
    (re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?(?:oauth|bearer)\s+)[^\s\"']+"), rf"\1{MASK}"),
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), f"EAA{MASK}"),
    (re.compile(r"\b(secret|ntn)_[A-Za-z0-9]{20,}"), rf"\1_{MASK}"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), f"sk-ant-{MASK}"),
    (re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]{20,}"), f"bot{MASK}"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"), MASK),
    (re.compile(r"\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b"), MASK),
)


def _secrets(include_ids: bool) -> list[str]:
    values = [settings.PAGE_TOKEN, settings.APP_SECRET, settings.NOTION_TOKEN, settings.TELEGRAM_BOT_TOKEN,
              settings.CLAUDE_KEY, settings.NOVEL_TG_BOT_TOKEN]
    if include_ids:
        values += [
            settings.PAGE_ID, settings.APP_ID, settings.TELEGRAM_CHAT_ID,
            settings.DB05_ID, settings.DB08_ID, settings.DB09_ID, settings.DB11_ID,
            settings.DB12_ID, settings.NOVEL_TG_CHAT_ID, settings.NOVEL_PROMPT_PAGE_ID, settings.NOVEL_MEMO_PAGE_ID,
        ]
        values += list(settings.BANNED_TERMS)  # 금칙어는 짧아도 가린다
        return sorted((v for v in values if v and (len(v) >= 6 or v in settings.BANNED_TERMS)),
                      key=len, reverse=True)
    return sorted((v for v in values if v and len(v) >= 6), key=len, reverse=True)


def redact(text: Any, include_ids: bool = True) -> str:
    """include_ids=False: 자격증명만 가린다 (비공개 노션 원장에 게시물 ID 를 온전히 남길 때)."""
    s = str(text)
    for value in _secrets(include_ids):
        s = s.replace(value, MASK)
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


def register_masks() -> None:
    """GitHub Actions 러너 로그 마스킹. Actions 밖에서는 아무것도 출력하지 않는다."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    for value in _secrets(include_ids=True):
        print(f"::add-mask::{value}", flush=True)

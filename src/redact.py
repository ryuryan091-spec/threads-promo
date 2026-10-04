"""자격증명 마스킹.

배경 (2026-09-20 확인)
  requests 네트워크 예외 문자열에는 요청 URL 전체가 들어간다.
    "Max retries exceeded with url: /v1.0/me/threads?...&access_token=THAA..."
  Threads Graph API 는 토큰을 쿼리 파라미터로 받으므로, 네트워크 오류가 나면
  토큰이 예외 메시지 → 로그 → 텔레그램 알림으로 그대로 흘러간다.
  GitHub Actions 는 등록된 Secret 문자열만 로그에서 가리고, 텔레그램은 가리지 않는다.

대책
  1. redact()      : 문자열에서 자격증명을 지운다 (Secret 실값 + 형식 패턴).
  2. install()     : 로그 레코드 생성 시점에 메시지·예외 트레이스를 마스킹한다.
                     src 패키지 import 시 1회 설치된다(src/__init__.py).
  3. 호출 지점     : notifier.send, ThreadsApiError 생성자에서도 한 번 더 적용한다.

v1.1.0
  4. mask_username / mask_mentions : 답글 감사 요약(scripts/reply_audit.py)처럼 타인 댓글을
     Actions 요약에 옮길 때 계정명을 가린다. 자격증명 마스킹과는 별개다.
"""

from __future__ import annotations

import logging
import os
import re
import traceback

VERSION = "1.2.0"   # v1.2.0: Facebook 페이지 토큰·Gemini 키 마스킹(v1.7.0 숏폼)
# v1.1.0: 계정명 가림(mask_username, mask_mentions)

MASK = "***"

# 실값을 그대로 찾아 지울 Secret 이름. 값이 짧으면 오탐이 커서 8자 이상만 쓴다.
SECRET_ENV_NAMES: tuple[str, ...] = (
    "THREADS_LONG_LIVED_TOKEN",
    "THREADS_APP_SECRET",
    "CLAUDE_AI_KEY",
    "GH_PAT_SECRETS_WRITE",
    "TELEGRAM_BOT_TOKEN",
    "NOTION_TOKEN",
    # v1.7.0 숏폼
    "FACE_PAGE_TOKEN",
    "GEMINI_API_SUB_PAY_KEY",
)
_MIN_SECRET_LEN = 8

# 형식 패턴. 실값을 모르는 경우(갱신 직후 새 토큰 등)를 덮는다.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(access_token|client_secret|th_refresh_token|fb_exchange_token|key)"
                r"=[^&\s'\"<>]+"), rf"\1={MASK}"),
    (re.compile(r"(?i)(\"?(?:access_token|client_secret)\"?\s*:\s*\")[^\"]+"), rf"\1{MASK}"),
    (re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]{20,}"), f"bot{MASK}"),
    (re.compile(r"\bTHAA[A-Za-z0-9_\-]{20,}"), f"THAA{MASK}"),
    # v1.7.0: Facebook 페이지 토큰(EAA…)과 Google API 키(AIza…) 형식
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), f"EAA{MASK}"),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"), f"AIza{MASK}"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), f"sk-ant-{MASK}"),
    (re.compile(r"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"), rf"\1_{MASK}"),
    (re.compile(r"\b(ntn|secret)_[A-Za-z0-9]{20,}"), rf"\1_{MASK}"),
)


def _secret_values() -> list[str]:
    values = []
    for name in SECRET_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if len(value) >= _MIN_SECRET_LEN:
            values.append(value)
    # 긴 값부터 지워야 부분 문자열 충돌이 없다.
    return sorted(values, key=len, reverse=True)


def redact(text: object) -> str:
    """문자열에서 자격증명을 가린다. 문자열이 아니면 str() 후 처리한다."""
    out = str(text)
    for value in _secret_values():
        if value in out:
            out = out.replace(value, MASK)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


_MENTION = re.compile(r"@[A-Za-z0-9_.]+")


def mask_username(name: object) -> str:
    """계정명을 앞 2글자만 남기고 가린다. 빈 값은 '-'."""
    value = str(name or "").strip().lstrip("@")
    if not value:
        return "-"
    return value[:2] + MASK


def mask_mentions(text: object) -> str:
    """본문 속 @계정 언급을 가린다."""
    return _MENTION.sub("@" + MASK, str(text or ""))


_INSTALLED = False


def install() -> None:
    """로그 레코드 팩토리에 마스킹을 건다. 여러 번 호출해도 1회만 설치된다."""
    global _INSTALLED
    if _INSTALLED:
        return

    original = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = original(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — 포맷 오류는 원래 경로에 맡긴다
            return record
        masked = redact(message)
        if masked != message:
            record.msg = masked
            record.args = None
        if record.exc_info and record.exc_info[0] is not None:
            text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_text = redact(text)
        return record

    logging.setLogRecordFactory(factory)
    _INSTALLED = True

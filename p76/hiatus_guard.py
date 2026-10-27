"""
p76/hiatus_guard.py
===================
휴재 콘텐츠 게시 직전 결정론 검사 (SEC C-13, DSN §6-3). 회장 검수의 보조이며 대체가 아니다.

  필수 : 픽션 고지 · #사건파일76
  투표 : 선택지 줄 정확히 2개 (A./B. · ①/② · 1./2.)
  금지 : 실제 날짜 형식 · 반응 강요 문구(fb-page-publisher content_guard 와 같은 목록) · 사전 고유명사
  면제 : 3요건 체크 줄 · D+n (본편 전용 규칙)
고유명사·금칙어 사전은 레포에 두지 않는다(외부 반출 금지) — 호출자가 비공개 사전에서 읽어 넘긴다.
"""
from __future__ import annotations

import re

from p76 import settings

VERSION = "1.0.0"

FICTION_NOTICE = "※ 여러 사례를 재구성한 픽션입니다."
HASHTAG = "#사건파일76"
REAL_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(19|20)\d{2}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}"),
    re.compile(r"(?<!\d)\d{1,2}\s*월\s*\d{1,2}\s*일"),
)
PUSHY_PHRASES = ("댓글 달아", "댓글로 남겨", "좋아요 눌러", "공유해 주세요", "팔로우 해 주세요")
VOTE_OPTION = re.compile(r"^\s*(?:[AB]\s*[.)]|[①②]|[12]\s*[.)])\s*\S", re.MULTILINE)
MAX_CHARS = 2000


def check(text: str, content: str, banned_terms: tuple[str, ...] = ()) -> list[str]:
    """위반 코드 목록 (빈 리스트 = 통과). 본문은 코드에 넣지 않는다."""
    body = text or ""
    if not body.strip():
        return ["hiatus:empty"]
    errors: list[str] = []
    if len(body) > MAX_CHARS:
        errors.append("hiatus:too_long")
    if FICTION_NOTICE not in body:
        errors.append("hiatus:fiction_notice_missing")
    if HASHTAG not in body:
        errors.append("hiatus:hashtag_missing")
    if content == settings.CONTENT_VOTE and len(VOTE_OPTION.findall(body)) != 2:
        errors.append("hiatus:vote_options")
    if content not in (settings.CONTENT_SUMMARY, settings.CONTENT_VOTE, settings.CONTENT_TEASER):
        errors.append("hiatus:content_type")
    if any(p.search(body) for p in REAL_DATE_PATTERNS):
        errors.append("hiatus:real_date_found")
    if any(p in body for p in PUSHY_PHRASES):
        errors.append("hiatus:pushy_phrase")
    if any(t and t in body for t in banned_terms):
        errors.append("hiatus:banned_term")
    return errors


def headline(text: str, limit: int = 60) -> str:
    """카드에 올릴 첫 줄 — 해시태그·고지 줄 제외."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("※"):
            continue
        return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""

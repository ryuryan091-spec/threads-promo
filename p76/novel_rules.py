"""
p76/novel_rules.py
==================
사건 소설 원고의 결정론 검사·조립 (DSN-P76-NOVEL-002 §6). AI 검수·회장 검수의 보조이며 대체가 아니다.

  check_episode()  R01~R12 위반 코드 목록 (빈 리스트 = 통과)
  check_bible()    바이블 JSON 검사
  fill_laws()      {{LAW_*}} 자리표시자 → 정본 법률 블록 (확인일 90일 이내만)
  footer()         고정 꼬리말 (픽션 고지·AI 표시·해시태그 — AI 는 쓰지 않는다)

금칙어(BANNED_TERMS)는 호출자가 넘긴다. 위반 코드에는 단어를 넣지 않는다(공개 로그).
"""
from __future__ import annotations

import json
import re
from datetime import date

from p76 import hiatus_guard, settings

VERSION = "1.0.0"

BODY_MIN, BODY_MAX = 3000, 4200
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
LAW_NAMES = ("LAW_76_2", "LAW_76_3", "LAW_116", "LAW_1350", "LAW_DISCLAIMER")
LAW_FINAL_ONLY = ("LAW_116",)
D_PLUS = re.compile(r"D\+\s?\d+")
QUOTE_MARK = re.compile(r"^📓\s*수사일지\s*#(\d{1,3})\s*발췌")
LEGAL_TEXT = (
    re.compile(r"제\s?\d+\s?조"),
    re.compile(r"과태료"),
    re.compile(r"\d[\d,]*\s?만\s?원"),
    re.compile(r"벌금"),
    re.compile(r"징역"),
    re.compile(r"(?<!\d)1350(?!\d)"),
)
INDUSTRY_TERMS = ("은행", "캐피탈", "카드사", "증권", "보험사", "저축은행", "여신", "대출", "심사역", "금융")
PLACE_TERMS = (
    "서울", "부산", "대구", "인천", "대전", "울산", "세종시", "경기도", "강원도", "충청", "전라", "경상",
    "제주", "강남", "여의도", "판교", "광화문", "을지로", "테헤란로",
)
HARM_TERMS = ("자살", "자해", "극단적 선택", "목숨을 끊", "사망", "숨졌", "숨을 거두", "폭행", "때렸", "주먹으로")
SUSPECT_CONVICTED = "가해자"
SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def _without_quotes(body: str) -> str:
    """인용 단락(📓 수사일지 #NNN 발췌 ~ 빈 줄)을 뺀 본문."""
    out, skipping = [], False
    for line in body.splitlines():
        if QUOTE_MARK.match(line.strip()):
            skipping = True
            continue
        if skipping and not line.strip():
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _quote_blocks(body: str) -> list[tuple[int, list[str]]]:
    blocks: list[tuple[int, list[str]]] = []
    current: tuple[int, list[str]] | None = None
    for line in body.splitlines():
        m = QUOTE_MARK.match(line.strip())
        if m:
            current = (int(m.group(1)), [])
            blocks.append(current)
            continue
        if current is not None:
            if not line.strip():
                current = None
            else:
                current[1].append(line.strip())
    return blocks


def body_chars(body: str) -> int:
    return len(_without_quotes(body).strip())


def _sentences(text: str) -> set[str]:
    return {s.strip() for s in SENTENCE_SPLIT.split(text or "") if len(s.strip()) >= 12}


def _cliche_hits(text: str, cliches: tuple[str, ...]) -> int:
    hits = 0
    for entry in cliches:
        for variant in (v.strip() for v in entry.split("/")):
            if variant:
                hits += text.count(variant)
    return hits


def common_checks(text: str, banned_terms: tuple[str, ...]) -> list[str]:
    """바이블·회차 공통 (R02 날짜 / R03 금칙어 / R04 업종·지명 / R06 반응 강요 / R07 위해 어휘)."""
    errors: list[str] = []
    if any(p.search(text) for p in hiatus_guard.REAL_DATE_PATTERNS):
        errors.append("R02:real_date")
    if any(t and t in text for t in banned_terms):
        errors.append("R03:banned_term")
    if any(t in text for t in INDUSTRY_TERMS):
        errors.append("R04:industry")
    if any(t in text for t in PLACE_TERMS):
        errors.append("R04:place")
    if any(p in text for p in hiatus_guard.PUSHY_PHRASES):
        errors.append("R06:pushy_phrase")
    if any(t in text for t in HARM_TERMS):
        errors.append("R07:harm_term")
    return errors


def check_episode(body: str, episode: int, *, banned_terms: tuple[str, ...] = (), cliches: tuple[str, ...] = (),
                  prev_bodies: tuple[str, ...] = (), journals: dict[int, str] | None = None,
                  d_plus_end: int | None = None, prev_d_plus_end: int | None = None) -> list[str]:
    """회차 본문(자리표시자 치환 전) 검사. journals: 인용 허용 일지 번호 → 원문."""
    text = body or ""
    if not text.strip():
        return ["R01:empty"]
    errors: list[str] = []
    chars = body_chars(text)
    if not BODY_MIN <= chars <= BODY_MAX:
        errors.append(f"R01:length:{chars}")
    if not D_PLUS.search(text):
        errors.append("R02:d_plus_missing")
    errors += common_checks(text, banned_terms)
    plain = PLACEHOLDER.sub("", text)
    if any(p.search(plain) for p in LEGAL_TEXT):
        errors.append("R05:legal_text")
    if episode < settings.NOVEL_EPISODES and SUSPECT_CONVICTED in text:
        errors.append("R08:convicted_early")
    sentences = _sentences(_without_quotes(text))
    previous = set().union(*(_sentences(b) for b in prev_bodies)) if prev_bodies else set()
    if len(sentences & previous) > 3:
        errors.append("R09:repeated_sentences")
    if _cliche_hits(text, cliches) > 2:
        errors.append("R09:cliche")
    quotes = _quote_blocks(text)
    if len(quotes) > 2:
        errors.append("R10:too_many_quotes")
    for no, lines in quotes:
        source = (journals or {}).get(no)
        if source is None or not lines or any(line not in source for line in lines):
            errors.append(f"R10:quote_mismatch:#{no:03d}")
    for line in text.splitlines():
        names = PLACEHOLDER.findall(line)
        if not names:
            continue
        if PLACEHOLDER.sub("", line).strip():
            errors.append("R11:placeholder_not_alone")
        for name in names:
            if name not in LAW_NAMES:
                errors.append("R11:unknown_placeholder")
            elif name in LAW_FINAL_ONLY and episode < settings.NOVEL_EPISODES:
                errors.append("R11:law_final_only")
    if d_plus_end is not None and prev_d_plus_end is not None and d_plus_end < prev_d_plus_end:
        errors.append("R12:d_plus_backwards")
    return sorted(set(errors))


def check_bible(bible: dict, *, banned_terms: tuple[str, ...] = (), journal_numbers: set[int] | None = None) -> list[str]:
    errors: list[str] = []
    text = json.dumps(bible, ensure_ascii=False)
    errors += common_checks(text, banned_terms)
    if any(p.search(text) for p in LEGAL_TEXT):
        errors.append("R05:legal_text")
    episodes = sorted(int(a.get("episode", 0)) for a in bible.get("arc") or [])
    if episodes != list(range(1, settings.NOVEL_EPISODES + 1)):
        errors.append("B01:arc_incomplete")
    slots = {c.get("slot") for c in bible.get("cast") or []}
    if not {"K1", "K2", "K3", "S4", "S5"} <= slots:
        errors.append("B02:cast_incomplete")
    promoted = set((bible.get("source") or {}).get("promoted_journals") or [])
    for a in bible.get("arc") or []:
        promoted |= set(a.get("quote_journals") or [])
        if len(a.get("quote_journals") or []) > 2:
            errors.append("B03:too_many_quotes")
    if journal_numbers is not None and not promoted <= journal_numbers:
        errors.append("B03:unknown_journal")
    return sorted(set(errors))


def law_block_ok(confirmed: date | None, today: date) -> bool:
    return confirmed is not None and 0 <= (today - confirmed).days <= settings.NOVEL_LAW_MAX_AGE_DAYS


def fill_laws(body: str, laws: dict[str, tuple[str, date | None]], today: date) -> tuple[str, list[str]]:
    """자리표시자 치환. (치환된 본문, 오류 코드). 오류가 있으면 게시하지 않는다."""
    errors: list[str] = []

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        text, confirmed = laws.get(name, ("", None))
        if not text:
            errors.append(f"LAW:missing:{name}")
            return m.group(0)
        if name != "LAW_DISCLAIMER" and not law_block_ok(confirmed, today):
            errors.append(f"LAW:unconfirmed:{name}")
        return text

    return PLACEHOLDER.sub(repl, body), sorted(set(errors))


def footer(season: int, episode: int, question: str) -> str:
    q = " ".join((question or "").split())
    lines = ["━━━━━━━━━━━━━━━"]
    if q:
        lines.append(f"💬 {q}")
    lines += [
        "(실명·회사명은 적지 말아 주세요. 확인 시 숨김 처리됩니다.)",
        "",
        f"📖 사건 소설 시즌 {season} · 제{episode}화 / 전 {settings.NOVEL_EPISODES}화 · 매주 일요일",
        "※ 여러 사례를 재구성한 픽션입니다. 법률 자문이 아닙니다.",
        "※ 이 글은 AI의 도움을 받아 작성했습니다.",
        "#사건파일76 #사건소설 #직장내괴롭힘",
    ]
    return "\n".join(lines)


def question_ok(question: str) -> list[str]:
    q = question or ""
    errors = []
    if any(p in q for p in hiatus_guard.PUSHY_PHRASES):
        errors.append("Q:pushy_phrase")
    if len(q) > 120:
        errors.append("Q:too_long")
    return errors

"""
p76/novel_prompts.py
====================
노션 「소설 프롬프트 정본」 페이지에서 프롬프트·스키마·법률 블록·클리셰 목록을 읽는다 (DSN-P76-NOVEL-002 §2.1).
프롬프트 본문은 공개 레포에 두지 않는다. 로그에는 버전 표식(sha256 앞 12자리)만 남긴다.

  ### NB system / NE system / NR system      → 다음 첫 코드 블록 = 시스템 프롬프트
  ### NB-S / NE-S / NR-S 출력 스키마           → 다음 첫 코드 블록 = JSON 스키마
  ## 클리셰 목록 …                             → 다음 첫 코드 블록(줄 단위)
  ## 법률 블록 …                               → 다음 첫 표(블록 · 문안 · 최종 확인일)

페이지 제목에 "초안" 이 있으면 쓰지 않는다(회장 승인 전).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date

from p76 import notion, settings

VERSION = "1.0.0"

ISO_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
SYSTEM_KEYS = {"NB system": "nb", "NE system": "ne", "NR system": "nr"}
SCHEMA_KEYS = {"NB-S": "nb", "NE-S": "ne", "NR-S": "nr"}


class PromptError(RuntimeError):
    """정본을 읽을 수 없거나 승인 전."""


@dataclass
class Prompts:
    systems: dict[str, str]
    schemas: dict[str, dict]
    cliches: tuple[str, ...]
    laws: dict[str, tuple[str, date | None]]
    style_samples: str = ""
    tags: dict[str, str] = field(default_factory=dict)

    def tag(self, which: str) -> str:
        return self.tags.get(which, "")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def parse_blocks(blocks: list[dict], table_rows: dict[str, list[dict]]) -> Prompts:
    systems: dict[str, str] = {}
    schemas: dict[str, dict] = {}
    cliches: tuple[str, ...] = ()
    laws: dict[str, tuple[str, date | None]] = {}
    pending = ""
    for block in blocks:
        btype = block.get("type", "")
        if btype in ("heading_2", "heading_3"):
            title = notion.block_text(block).strip()
            pending = ""
            for name in SYSTEM_KEYS:
                if title.startswith(name):
                    pending = f"system:{SYSTEM_KEYS[name]}"
            for name in SCHEMA_KEYS:
                if title.startswith(name):
                    pending = f"schema:{SCHEMA_KEYS[name]}"
            if title.startswith("클리셰 목록"):
                pending = "cliches"
            if title.startswith("법률 블록"):
                pending = "laws"
            continue
        if not pending:
            continue
        if btype == "code":
            text = notion.block_text(block)
            if pending.startswith("system:"):
                systems[pending[7:]] = text.strip()
            elif pending.startswith("schema:"):
                try:
                    schemas[pending[7:]] = json.loads(text)
                except ValueError:
                    raise PromptError(f"prompt:schema_invalid:{pending[7:]}") from None
            elif pending == "cliches":
                cliches = tuple(line.strip() for line in text.splitlines() if line.strip())
            pending = "" if pending != "laws" else pending
        elif btype == "table" and pending == "laws":
            for row in table_rows.get(block.get("id", ""), [])[1:]:  # 첫 행은 머리글
                cells = notion.block_text(row).split("\t")
                if len(cells) < 3:
                    continue
                name = cells[0].replace("\\", "").strip()
                found = ISO_DATE.search(cells[2])
                confirmed = date.fromisoformat(found.group(1)) if found else None
                laws[name] = (cells[1].strip(), confirmed)
            pending = ""
    missing = [k for k in ("nb", "ne", "nr") if k not in systems or k not in schemas]
    if missing:
        raise PromptError("prompt:missing:" + ",".join(missing))
    tags = {k: f"{k.upper()}-1.0/{_sha(systems[k] + json.dumps(schemas[k], sort_keys=True))}" for k in systems}
    return Prompts(systems=systems, schemas=schemas, cliches=cliches, laws=laws, tags=tags)


def load() -> Prompts:
    page_id = settings.NOVEL_PROMPT_PAGE_ID
    if not page_id:
        raise PromptError("prompt:not_configured")
    page = notion.retrieve_page(page_id)
    title = notion.plain(page, "title")
    if not title or "초안" in title:
        raise PromptError("prompt:not_approved")
    blocks = list(notion.children(page_id))
    tables = {b["id"]: list(notion.children(b["id"])) for b in blocks if b.get("type") == "table"}
    return parse_blocks(blocks, tables)


def style_samples(numbers: tuple[int, ...] = settings.NOVEL_STYLE_SAMPLES) -> str:
    """A형 수사일지 피드본문 (DB-05 읽기 전용)."""
    parts = []
    for no in numbers:
        body = {"filter": {"property": settings.D5_NO, "number": {"equals": no}}, "page_size": 1}
        for page in notion.query(settings.DB05_ID, body, max_pages=1):
            text = notion.full_text(page, "피드본문").strip()
            if text:
                parts.append(f"--- 수사일지 #{no:03d} ---\n{text}")
    return "\n\n".join(parts)


def published_journals(limit: int = settings.NOVEL_JOURNAL_CANDIDATES) -> dict[int, str]:
    """게시된(발행완료) A형 수사일지 중 본문이 있는 최근 것 → {번호: 본문}."""
    body = {"filter": {"property": settings.D5_STATE, "select": {"equals": settings.S_DONE}},
            "sorts": [{"property": settings.D5_NO, "direction": "descending"}], "page_size": 50}
    out: dict[int, str] = {}
    for page in notion.query(settings.DB05_ID, body, max_pages=2):
        no = notion.number(page, settings.D5_NO)
        if no is None:
            continue
        text = notion.full_text(page, "피드본문").strip()
        if text:
            out[no] = text
        if len(out) >= limit:
            break
    return out


def journal_text(numbers: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for no in numbers:
        body = {"filter": {"and": [
            {"property": settings.D5_NO, "number": {"equals": no}},
            {"property": settings.D5_STATE, "select": {"equals": settings.S_DONE}},
        ]}, "page_size": 1}
        for page in notion.query(settings.DB05_ID, body, max_pages=1):
            text = notion.full_text(page, "피드본문").strip()
            if text:
                out[no] = text
    return out


def memo_patterns() -> list[dict]:
    """소재 메모 패턴 표 → [{id, types, pattern, direction}] (상태 사용됨·폐기 제외, 추상 요약 칸만)."""
    page_id = settings.NOVEL_MEMO_PAGE_ID
    if not page_id:
        return []
    out: list[dict] = []
    for block in notion.children(page_id):
        if block.get("type") != "table":
            continue
        rows = [notion.block_text(r).split("\t") for r in notion.children(block["id"])]
        if not rows or not rows[0] or rows[0][0].strip() != "ID":
            continue
        head = [h.strip() for h in rows[0]]
        col = {name: i for i, name in enumerate(head)}
        need = ("ID", "패턴 (추상화)", "사건유형", "세계관 변환 방향", "상태")
        if not all(n in col for n in need):
            continue
        for cells in rows[1:]:
            if len(cells) < len(head):
                continue
            status = cells[col["상태"]].strip()
            if status in ("사용됨", "폐기"):
                continue
            out.append({
                "id": cells[col["ID"]].strip(),
                "types": cells[col["사건유형"]].strip(),
                "pattern": cells[col["패턴 (추상화)"]].strip(),
                "direction": cells[col["세계관 변환 방향"]].strip(),
            })
        break
    return out

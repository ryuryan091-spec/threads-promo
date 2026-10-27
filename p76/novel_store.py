"""
p76/novel_store.py
==================
DB-12 소설 원고 읽기·쓰기 (DSN-P76-NOVEL-002 §3).

  - 본문·바이블 JSON·메타는 속성이 아니라 행 페이지의 블록(제목 + 코드 블록)에 둔다.
  - dry_run 은 노션에 쓰지 않는다. 대신 메모리 안의 행을 고쳐 같은 실행의 다음 단계가 이어지게 한다.
  - 코드가 쓰는 상태 전이는 ALLOWED 목록만 허용한다. '승인' 으로의 전이는 코드에 없다(사람만).
  - CONTROL 행: 결과코드 칸에 JSON {tg_offset, month, spent, last_run, budget_alert_day}.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from p76 import notion, safety, settings

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

H_BODY, H_QUESTION, H_QUOTE, H_SUBTITLE, H_META = "본문 (게시용)", "열린 질문", "표지 인용", "부제", "메타 (JSON)"
H_SUMMARY, H_BIBLE, H_REVIEW = "요약", "바이블 (JSON)", "검수 (JSON)"
CONTROL_KEY = "CONTROL"

S = settings
ALLOWED: dict[str, set[str]] = {
    "": {S.NS_GENERATING},
    S.NS_GENERATING: {S.NS_CHECK_FAILED, S.NS_HELD, S.NS_REFUSED, S.NS_IMPLICIT, S.NS_EXPLICIT, S.NS_REVIEW,
                      S.NS_STOPPED},
    S.NS_CHECK_FAILED: {S.NS_GENERATING, S.NS_STOPPED},
    S.NS_IMPLICIT: {S.NS_CANON, S.NS_STOPPED},
    S.NS_EXPLICIT: {S.NS_STOPPED},
    S.NS_CANON: {S.NS_STOPPED},
    S.NS_REVIEW: {S.NS_STOPPED},
    S.NS_APPROVED: {S.NS_PUBLISHED, S.NS_CHECK, S.NS_STOPPED},
    S.NS_HELD: {S.NS_STOPPED},
    S.NS_REFUSED: {S.NS_STOPPED},
}


class TransitionError(RuntimeError):
    """허용되지 않은 상태 전이 (코드 결함)."""


def _as_read(props: dict) -> dict:
    """쓰기 값 → 읽기 형식 (dry_run 메모리 행·즉시 반영용)."""
    out = {}
    for name, value in props.items():
        if "title" in value or "rich_text" in value:
            kind = "title" if "title" in value else "rich_text"
            text = "".join(r["text"]["content"] for r in value[kind] or [])
            out[name] = {"type": kind, kind: [{"plain_text": text}] if text else []}
        else:
            kind = next(iter(value))
            out[name] = {"type": kind, **value}
    return out


def writes_allowed() -> bool:
    return safety.notion_write_allowed()


# ── 조회 ──────────────────────────────────────────────────
def all_rows() -> list[dict]:
    return list(notion.query(settings.DB12_ID, {"page_size": 100}, max_pages=10))


def key(page: dict) -> str:
    return notion.plain(page, settings.N_KEY)


def state(page: dict) -> str:
    return notion.select(page, settings.N_STATE)


def kind(page: dict) -> str:
    return notion.select(page, settings.N_KIND)


def season(page: dict) -> int:
    return notion.number(page, settings.N_SEASON) or 0


def episode(page: dict) -> int:
    return notion.number(page, settings.N_EPISODE) or 0


def stopped(page: dict) -> bool:
    return notion.checkbox(page, settings.N_STOP) or state(page) == settings.NS_STOPPED


def bible_key(n: int) -> str:
    return f"N{n}-BIBLE"


def episode_key(n: int, m: int) -> str:
    return f"N{n}-E{m:02d}"


def find(rows: list[dict], row_key: str) -> dict | None:
    return next((p for p in rows if key(p) == row_key), None)


def refresh(page: dict) -> dict:
    if not writes_allowed() or str(page.get("id", "")).startswith("dry-"):
        return page
    return notion.retrieve_page(page["id"])


# ── 쓰기 ──────────────────────────────────────────────────
def create(rows: list[dict], row_key: str, row_kind: str, n: int = 0, m: int = 0, due: date | None = None) -> dict:
    props = {
        settings.N_KEY: notion.v_title(row_key),
        settings.N_KIND: notion.v_select(row_kind),
        settings.N_STATE: notion.v_select(settings.NS_GENERATING if row_kind != settings.NK_CONTROL else None),
        settings.N_ATTEMPTS: notion.v_number(0),
        settings.N_COST: notion.v_number(0),
    }
    if n:
        props[settings.N_SEASON] = notion.v_number(n)
    if m:
        props[settings.N_EPISODE] = notion.v_number(m)
    if due:
        props[settings.N_DUE] = notion.v_date(due)
    if writes_allowed():
        page = notion.create_page(settings.DB12_ID, props)
    else:
        logger.info(f"[Novel][{settings.MODE}] {row_key} 행 생성 생략")
        page = {"id": f"dry-{row_key}", "properties": _as_read(props)}
    rows.append(page)
    return page


def update(page: dict, props: dict) -> None:
    if writes_allowed() and not str(page.get("id", "")).startswith("dry-"):
        notion.update_page(page["id"], props)
    page.setdefault("properties", {}).update(_as_read(props))


def transition(page: dict, new_state: str, extra: dict | None = None) -> None:
    current = state(page)
    if new_state not in ALLOWED.get(current, set()):
        raise TransitionError(f"{current or '-'}->{new_state}")
    update(page, {settings.N_STATE: notion.v_select(new_state), **(extra or {})})


def add_cost(page: dict, usd: float) -> None:
    total = round((notion.number_f(page, settings.N_COST) or 0.0) + usd, 4)
    update(page, {settings.N_COST: notion.v_number(total)})


def append(page: dict, blocks: list[dict]) -> None:
    if writes_allowed() and not str(page.get("id", "")).startswith("dry-"):
        for i in range(0, len(blocks), 100):
            notion.append_children(page["id"], blocks[i:i + 100])
    page["_blocks"] = page.get("_blocks", []) + blocks


def section_blocks(sections: list[tuple[str, str, str]]) -> list[dict]:
    """[(제목, 내용, 'code'|'json'|'text')] → 블록 목록."""
    blocks: list[dict] = []
    for title, content, form in sections:
        blocks.append(notion.b_heading(title))
        if form == "text":
            blocks.append(notion.b_paragraph(content))
        else:
            blocks.append(notion.b_code(content, "json" if form == "json" else "plain text"))
    return blocks


def sections(page: dict) -> dict[str, str]:
    """제목(heading_2) → 바로 뒤 코드·문단 블록 평문."""
    if "_blocks" in page and (str(page.get("id", "")).startswith("dry-") or not writes_allowed()):
        blocks = page["_blocks"]
    else:
        blocks = list(notion.children(page["id"]))
    out: dict[str, str] = {}
    current = ""
    for block in blocks:
        btype = block.get("type")
        if btype == "heading_2":
            current = notion.block_text(block).strip()
            continue
        if current and current not in out and btype in ("code", "paragraph"):
            out[current] = notion.block_text(block)
    return out


def json_section(page: dict, title: str) -> dict:
    raw = sections(page).get(title, "")
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ── CONTROL 행 ────────────────────────────────────────────
def control(rows: list[dict]) -> tuple[dict, dict]:
    page = find(rows, CONTROL_KEY)
    if page is None:
        page = create(rows, CONTROL_KEY, settings.NK_CONTROL)
    try:
        data = json.loads(notion.plain(page, settings.N_RESULT) or "{}")
    except ValueError:
        data = {}
    return page, data if isinstance(data, dict) else {}


def save_control(page: dict, data: dict) -> None:
    update(page, {settings.N_RESULT: notion.v_rich(json.dumps(data, ensure_ascii=False, sort_keys=True))})


def month_spent(data: dict, now: datetime) -> float:
    month = now.astimezone(notion.KST).strftime("%Y-%m")
    return float(data.get("spent", 0.0)) if data.get("month") == month else 0.0


def add_month_spent(data: dict, now: datetime, usd: float) -> None:
    month = now.astimezone(notion.KST).strftime("%Y-%m")
    data["spent"] = round(month_spent(data, now) + usd, 4)
    data["month"] = month

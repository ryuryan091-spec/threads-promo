"""
p76/notion.py
=============
노션 클라이언트 (Notion-Version 2022-06-28).

  - POST  /v1/databases/{id}/query        조회 (페이지네이션)
  - GET   /v1/databases/{id}              스키마 (health H5)
  - GET   /v1/pages/{id}                  재조회 (선점 확인)
  - GET   /v1/pages/{id}/properties/{pid} rich_text 전체 (페이지 객체는 25개 항목까지만)
  - PATCH /v1/pages/{id}                  갱신 (같은 값 재전송 = 멱등 → 재시도 허용)
  - POST  /v1/pages                       생성 (멱등 아님 → 재시도 없음)

DB-05 는 읽기 전용이다 (SEC C-06). DB-05 에서 읽은 페이지와 DB-05 부모로는 쓰기 호출을 만들지 않는다.
오류 메시지 본문에는 통합 이름·DB ID 가 들어 있어 코드만 남긴다.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from p76 import safety, settings
from p76.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
RETRY_MAX = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)
_RICH_CHUNK = 1900
_sleep = time.sleep  # 테스트에서 교체

_readonly_pages: set[str] = set()


class NotionError(RuntimeError):
    """노션 API 실패 (메시지는 마스킹됨)."""


class ReadOnlyViolation(RuntimeError):
    """DB-05 쓰기 시도 (코드 결함 — 테스트로 0건 고정)."""


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.NOTION_TOKEN}",
        "Notion-Version": settings.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _norm(object_id: str) -> str:
    return (object_id or "").replace("-", "").lower()


def _request(method: str, path: str, body: dict | None = None, retry: bool = True) -> dict:
    attempts = RETRY_MAX + 1 if retry else 1
    resp = None
    for attempt in range(attempts):
        try:
            resp = requests.request(
                method, f"{settings.NOTION_BASE}{path}", headers=_headers(), json=body,
                timeout=settings.HTTP_TIMEOUT_SEC,
            )
        except requests.exceptions.RequestException as e:
            if attempt + 1 < attempts:
                logger.warning(f"[Notion] {method} {type(e).__name__} — 재시도 {attempt + 1}/{RETRY_MAX}")
                _sleep(float(2 ** attempt))
                continue
            raise NotionError(f"{method} request_error:{type(e).__name__}:{redact(e)[:80]}") from None
        if resp.status_code in RETRY_STATUSES and attempt + 1 < attempts:
            logger.warning(f"[Notion] {method} http_{resp.status_code} — 재시도 {attempt + 1}/{RETRY_MAX}")
            try:
                wait = min(float(resp.headers.get("Retry-After", "")), 30.0)
            except (TypeError, ValueError):
                wait = float(2 ** attempt)
            _sleep(wait)
            continue
        break
    if resp.status_code != 200:
        try:
            code = str(resp.json().get("code", ""))
        except ValueError:
            code = ""
        raise NotionError(f"{method} http_{resp.status_code}:{code or 'unknown'}")
    try:
        return resp.json()
    except ValueError:
        raise NotionError(f"{method} invalid_json") from None


# ── 조회 ──────────────────────────────────────────────────
def query(db_id: str, body: dict, max_pages: int = 20) -> Iterator[dict]:
    if not db_id:
        raise NotionError("db_not_configured")
    cursor = None
    for _ in range(max_pages):
        payload = dict(body)
        if cursor:
            payload["start_cursor"] = cursor
        data = _request("POST", f"/databases/{db_id}/query", payload)
        for page in data.get("results", []):
            if _norm(db_id) == _norm(settings.DB05_ID):
                _readonly_pages.add(_norm(page.get("id", "")))
            yield page
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            return


def retrieve_database(db_id: str) -> dict:
    if not db_id:
        raise NotionError("db_not_configured")
    return _request("GET", f"/databases/{db_id}")


def retrieve_page(page_id: str) -> dict:
    return _request("GET", f"/pages/{page_id}")


def full_text(page: dict, prop_name: str) -> str:
    """rich_text 속성 전체. 속성 ID 는 응답 값을 그대로 쓴다(재인코딩 금지)."""
    prop = (page.get("properties") or {}).get(prop_name) or {}
    prop_id = str(prop.get("id", ""))
    if not prop_id:
        raise NotionError(f"property_missing:{prop_name}")
    parts: list[str] = []
    cursor = None
    while True:
        path = f"/pages/{page['id']}/properties/{prop_id}"
        if cursor:
            path += f"?start_cursor={quote(cursor, safe='')}"
        data = _request("GET", path)
        for item in data.get("results", []):
            parts.append(str((item.get("rich_text") or {}).get("plain_text", "")))
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return "".join(parts)


# ── 쓰기 ──────────────────────────────────────────────────
def update_page(page_id: str, props: dict) -> None:
    if _norm(page_id) in _readonly_pages:
        raise ReadOnlyViolation("db05_page")
    safety.guard_notion_write()
    _request("PATCH", f"/pages/{page_id}", {"properties": props})


def create_page(db_id: str, props: dict) -> dict:
    if not db_id or _norm(db_id) == _norm(settings.DB05_ID):
        raise ReadOnlyViolation("db05_parent")
    safety.guard_notion_write()
    return _request("POST", "/pages", {"parent": {"database_id": db_id}, "properties": props}, retry=False)


# ── 속성 읽기 ─────────────────────────────────────────────
def _prop(page: dict, name: str) -> dict:
    return (page.get("properties") or {}).get(name) or {}


def plain(page: dict, name: str) -> str:
    p = _prop(page, name)
    rich = p.get("rich_text") if "rich_text" in p else p.get("title")
    return "".join(str(r.get("plain_text", "")) for r in rich or []).strip()


def number(page: dict, name: str) -> int | None:
    value = _prop(page, name).get("number")
    return int(value) if isinstance(value, (int, float)) else None


def select(page: dict, name: str) -> str:
    return str((_prop(page, name).get("select") or {}).get("name", ""))


def checkbox(page: dict, name: str) -> bool:
    return bool(_prop(page, name).get("checkbox"))


def files(page: dict, name: str) -> list[dict]:
    return list(_prop(page, name).get("files") or [])


def date_value(page: dict, name: str) -> datetime | None:
    value = _prop(page, name).get("date") or {}
    raw = value.get("start")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def kst_date(page: dict, name: str) -> date | None:
    dt = date_value(page, name)
    return dt.astimezone(KST).date() if dt else None


def file_url(item: dict) -> str:
    kind = item.get("type")
    if kind == "file":
        return str((item.get("file") or {}).get("url", ""))
    if kind == "external":
        return str((item.get("external") or {}).get("url", ""))
    return ""


# ── 속성 쓰기 값 ──────────────────────────────────────────
def v_title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text}}]}


def v_rich(text: str) -> dict:
    text = text or ""
    return {"rich_text": [{"type": "text", "text": {"content": text[i:i + _RICH_CHUNK]}}
                          for i in range(0, len(text), _RICH_CHUNK)]}


def v_number(value: float | int | None) -> dict:
    return {"number": value}


def v_select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def v_date(value: date | datetime | None) -> dict:
    if value is None:
        return {"date": None}
    if isinstance(value, datetime):
        return {"date": {"start": value.astimezone(KST).isoformat(timespec="seconds")}}
    return {"date": {"start": value.isoformat()}}


def v_checkbox(value: bool) -> dict:
    return {"checkbox": bool(value)}


def schema_mismatches(db: dict, required: dict[str, str]) -> list[str]:
    """필수 속성 이름·타입 대조 (health H5)."""
    props = db.get("properties") or {}
    out = []
    for name, kind in required.items():
        actual = (props.get(name) or {}).get("type")
        if actual is None:
            out.append(f"missing:{name}")
        elif actual != kind:
            out.append(f"type:{name}:{actual}")
    return out


def _reset_readonly() -> None:
    """테스트용."""
    _readonly_pages.clear()

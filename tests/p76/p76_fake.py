"""
테스트용 가짜 노션·Graph. 합성 데이터만 쓴다 (실제 일지·댓글 금지, STD-TEST-14).
노션은 p76.notion._request 한 곳, Graph 는 p76.graph.requests 를 바꿔 끼운다.
"""
from __future__ import annotations

import itertools
import json
import struct
import zlib
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

import requests

from p76 import notion, settings

_ids = itertools.count(1)


def new_id() -> str:
    return f"{next(_ids):032x}"


# ── 노션 ──────────────────────────────────────────────────
def _to_read(kind: str, value: Any) -> dict:
    """테스트 입력 값 → 노션 응답 형식."""
    if kind in ("title", "rich_text"):
        return {kind: [{"plain_text": str(value)}] if value not in (None, "") else []}
    if kind == "number":
        return {"number": value}
    if kind == "select":
        return {"select": {"name": value} if value else None}
    if kind == "checkbox":
        return {"checkbox": bool(value)}
    if kind == "date":
        if value is None:
            return {"date": None}
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        return {"date": {"start": value}}
    if kind == "files":
        return {"files": list(value or [])}
    raise ValueError(kind)


def _write_to_read(kind: str, value: dict) -> dict:
    """PATCH/POST 속성 값 → 응답 형식."""
    if kind in ("title", "rich_text"):
        text = "".join(r["text"]["content"] for r in value.get(kind) or [])
        return _to_read(kind, text)
    return value


class FakeNotion:
    def __init__(self) -> None:
        self.schemas: dict[str, dict[str, str]] = {}
        self.pages: dict[str, dict] = {}
        self.page_db: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail: Callable[[str, str], Exception | None] = lambda m, p: None
        self.blocks: dict[str, list[dict]] = {}

    @staticmethod
    def _readable_block(block: dict) -> dict:
        """쓰기 형식 블록 → 응답 형식 (rich_text 에 plain_text 추가, id 부여)."""
        b = json.loads(json.dumps(block))
        b.setdefault("id", new_id())
        value = b.get(b.get("type", ""), {})
        for r in value.get("rich_text") or []:
            r.setdefault("plain_text", (r.get("text") or {}).get("content", ""))
        return b

    def set_blocks(self, block_id: str, blocks: list[dict]) -> list[dict]:
        self.blocks[block_id] = [self._readable_block(b) for b in blocks]
        return self.blocks[block_id]

    def add_plain_page(self, title: str) -> str:
        pid = new_id()
        self.pages[pid] = {"id": pid, "properties": {"title": {"id": "title", "type": "title",
                                                               "title": [{"plain_text": title}]}}}
        self.page_db[pid] = ""
        return pid

    def add_db(self, db_id: str, schema: dict[str, str]) -> None:
        self.schemas[db_id] = dict(schema)

    def add(self, db_id: str, **values: Any) -> dict:
        schema = self.schemas[db_id]
        props = {}
        for name, kind in schema.items():
            props[name] = {"id": name, "type": kind, **_to_read(kind, values.get(name))}
        pid = new_id()
        page = {"id": pid, "properties": props}
        self.pages[pid] = page
        self.page_db[pid] = db_id
        return page

    def rows(self, db_id: str) -> list[dict]:
        return [p for pid, p in self.pages.items() if self.page_db[pid] == db_id]

    # 값 읽기 도우미
    @staticmethod
    def val(page: dict, name: str) -> Any:
        p = page["properties"][name]
        kind = p["type"]
        if kind in ("title", "rich_text"):
            return "".join(r["plain_text"] for r in p[kind])
        if kind == "select":
            return (p["select"] or {}).get("name")
        if kind == "date":
            return (p["date"] or {}).get("start")
        return p[kind]

    # 필터 평가
    def _match(self, page: dict, flt: dict | None) -> bool:
        if not flt:
            return True
        if "and" in flt:
            return all(self._match(page, f) for f in flt["and"])
        if "or" in flt:
            return any(self._match(page, f) for f in flt["or"])
        name = flt["property"]
        value = self.val(page, name)
        for kind in ("select", "checkbox", "number", "title", "rich_text", "date"):
            if kind not in flt:
                continue
            cond = flt[kind]
            if "equals" in cond:
                return value == cond["equals"]
            if "is_not_empty" in cond:
                return bool(value)
            if "on_or_after" in cond:
                return bool(value) and value[:10] >= cond["on_or_after"]
            if "on_or_before" in cond:
                return bool(value) and value[:10] <= cond["on_or_before"]
        raise ValueError(flt)

    def request(self, method: str, path: str, body: dict | None = None, retry: bool = True) -> dict:
        self.calls.append((method, path, json.loads(json.dumps(body)) if body else None))
        err = self.fail(method, path)
        if err:
            raise err
        parts = path.strip("/").split("/")
        if method == "POST" and parts[0] == "databases" and parts[2] == "query":
            db_id = parts[1]
            rows = [p for p in self.rows(db_id) if self._match(p, (body or {}).get("filter"))]
            for s in reversed((body or {}).get("sorts") or []):
                rows.sort(key=lambda p, n=s["property"]: (self.val(p, n) is None, self.val(p, n) or 0),
                          reverse=s["direction"] == "descending")
            return {"results": rows[: (body or {}).get("page_size", 100)], "has_more": False}
        if method == "GET" and parts[0] == "databases":
            return {"properties": {n: {"type": k} for n, k in self.schemas[parts[1]].items()}}
        if method == "GET" and parts[0] == "pages" and len(parts) == 2 and parts[1] in self.pages:
            return json.loads(json.dumps(self.pages[parts[1]]))
        if method == "GET" and parts[0] == "pages" and parts[2] == "properties":
            text = self.val(self.pages[parts[1]], parts[3].split("?")[0])
            return {"results": [{"rich_text": {"plain_text": text}}], "has_more": False}
        if method == "PATCH" and parts[0] == "pages":
            page = self.pages[parts[1]]
            for name, value in body["properties"].items():
                kind = page["properties"][name]["type"]
                page["properties"][name] = {"id": name, "type": kind, **_write_to_read(kind, value)}
            return page
        if method == "POST" and parts == ["pages"]:
            db_id = body["parent"]["database_id"]
            page = self.add(db_id)
            for name, value in body["properties"].items():
                kind = self.schemas[db_id][name]
                page["properties"][name] = {"id": name, "type": kind, **_write_to_read(kind, value)}
            if body.get("children"):
                self.set_blocks(page["id"], body["children"])
            return page
        if parts[0] == "blocks" and method == "GET":
            return {"results": json.loads(json.dumps(self.blocks.get(parts[1], []))), "has_more": False}
        if parts[0] == "blocks" and method == "PATCH":
            self.blocks.setdefault(parts[1], []).extend(self._readable_block(b) for b in body["children"])
            return {"results": []}
        raise AssertionError(f"unexpected notion call {method} {path}")

    def writes(self) -> list[tuple[str, str, dict | None]]:
        return [c for c in self.calls if c[0] == "PATCH" or (c[0] == "POST" and not c[1].endswith("/query"))]

    def page_text(self, page_id: str) -> str:
        return "\n".join("".join(r.get("plain_text", "") for r in (b.get(b["type"]) or {}).get("rich_text") or [])
                         for b in self.blocks.get(page_id, []))


# ── Graph ─────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.headers: dict = {}

    def json(self) -> dict:
        return self._body


def err(code: int, http: int = 400) -> FakeResp:
    return FakeResp(http, {"error": {"code": code, "message": "synthetic error"}})


class FakeGraph:
    """requests 모듈 대역. handlers: (method, path) → FakeResp | Exception | callable."""

    exceptions = requests.exceptions

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.handlers: dict[tuple[str, str], Any] = {}

    def _path(self, url: str) -> str:
        return url.split(f"/{settings.GRAPH_VERSION}/", 1)[-1]

    def _dispatch(self, method: str, url: str, kwargs: dict) -> FakeResp:
        path = self._path(url)
        params = dict(kwargs.get("params") or kwargs.get("data") or {})
        self.calls.append((method, path, params))
        handler = self.handlers.get((method, path))
        if handler is None:
            fields = params.get("fields", "")
            handler = self.handlers.get((method, f"{path}?{fields}"))
        if handler is None:
            raise AssertionError(f"unexpected graph call {method} {path} {params.get('fields', '')}")
        if callable(handler) and not isinstance(handler, FakeResp):
            handler = handler(params)
        if isinstance(handler, BaseException):
            raise handler
        return handler

    def get(self, url: str, **kwargs: Any) -> FakeResp:
        return self._dispatch("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResp:
        return self._dispatch("POST", url, kwargs)

    def delete(self, url: str, **kwargs: Any) -> FakeResp:
        return self._dispatch("DELETE", url, kwargs)

    def writes(self) -> list:
        """Graph 쓰기 = POST · DELETE (DN2026_0119 예약 취소 포함)."""
        return [c for c in self.calls if c[0] in ("POST", "DELETE")]


# ── 공통 상수·도우미 ─────────────────────────────────────
# 가짜 값 — 실제 형식과 비슷하게 만들되 파일에는 리터럴로 두지 않는다 (scan_secrets 대상)
FAKE_TOKEN = "EAAB" + "Zq9" * 20
FAKE_NOTION_TOKEN = "ntn_" + "Fk7" * 15
FAKE_BOT = "8123456789:" + "AAf" * 12
FAKE_PAGE_ID = "61" + "0" * 13
DB05, DB08, DB09, DB11 = ("d5" * 16, "d8" * 16, "d9" * 16, "db" * 16)
DB12 = "dc" * 16

BASE_ENV = {
    "PAGE76_ENABLED": "true",
    "PAGE76_MODE": "live",
    "PAGE76_HIATUS_PUBLISH_ENABLED": "true",
    "PAGE76_INSIGHTS_ENABLED": "true",
    "FACE_PAGE76_ID": FAKE_PAGE_ID,
    "FACE_PAGE76_TOKEN": FAKE_TOKEN,
    "PAGE76_NOTION_TOKEN": FAKE_NOTION_TOKEN,
    "PAGE76_DB05_ID": DB05,
    "PAGE76_DB08_ID": DB08,
    "PAGE76_DB09_ID": DB09,
    "PAGE76_DB11_ID": DB11,
    "PAGE76_TELEGRAM_BOT_TOKEN": "",
    "PAGE76_TELEGRAM_CHAT_ID": "",
    "GITHUB_RUN_ID": "4242",
    # 기본은 수동 실행(예약 cron 없음). 예약 실행 테스트는 env(PAGE76_SCHEDULE_CRON=...) 로 덮어쓴다
    "PAGE76_SCHEDULE_CRON": "",
    "PAGE76_MAX_LAG_MIN": "",
}


def kst(d: date, hh: int = 20, mm: int = 30) -> str:
    return datetime.combine(d, time(hh, mm), tzinfo=notion.KST).isoformat()


def add_episode(fake: FakeNotion, number: int, published: date | None, state: str = "발행완료",
                feed_id: str = "", reel_id: str = "") -> dict:
    return fake.add(DB05, **{
        "회차키": f"#{number:03d}", "일지번호": number, "게시상태": state, "검수완료": True,
        "발행일시": kst(published) if published else None, "FB피드ID": feed_id, "FB릴스ID": reel_id,
    })


def png_bytes(w: int, h: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + chunk


def jpeg_bytes(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def schedule_handlers(g: FakeGraph, post_id: str = "61_9", photo_id: str = "ph9") -> dict:
    """FB 예약 게시 2단계 + 예약 목록 + 게시 확인 + 취소 + 비공개 전환 + 공개 게시물 목록 가짜 응답 (DN2026_0119).
    state["scheduled"] 는 예약 목록, state["published"] 는 is_published 값, state["delete_fail"] 이 있으면
    DELETE 가 그 응답을 돌려준다(fail_deletes), state["unpublish"] 는 비공개 전환 응답,
    state["page_posts"] 는 GET /{page}/posts 목록(합성 message 포함). 테스트가 바꿔 쓴다."""
    state: dict = {"scheduled": [], "published": False, "created": "2026-11-29T03:00:05+0000",
                   "delete_fail": None, "unpublish": FakeResp(200, {"success": True}), "page_posts": []}

    def feed(params: dict) -> FakeResp:
        state["scheduled"].append({"id": post_id, "scheduled_publish_time": int(params["scheduled_publish_time"])})
        return FakeResp(200, {"id": post_id})

    def listing(params: dict) -> FakeResp:
        return FakeResp(200, {"data": list(state["scheduled"])})

    def delete(params: dict) -> Any:
        if state["delete_fail"] is not None:
            return state["delete_fail"]
        state["scheduled"] = [p for p in state["scheduled"] if p["id"] != post_id]
        return FakeResp(200, {"success": True})

    def unpublish(params: dict) -> Any:
        assert params.get("is_published") == "false"
        return state["unpublish"]

    def posts(params: dict) -> FakeResp:
        since, until = int(params["since"]), int(params["until"])
        rows = [p for p in state["page_posts"]
                if since <= int(datetime.fromisoformat(p["created_time"].replace("+0000", "+00:00")).timestamp()) <= until]
        if "message" not in params.get("fields", ""):
            rows = [{k: v for k, v in p.items() if k != "message"} for p in rows]
        return FakeResp(200, {"data": rows})

    page = settings.PAGE_ID
    g.handlers[("POST", f"{page}/photos")] = FakeResp(200, {"id": photo_id})
    g.handlers[("POST", f"{page}/feed")] = feed
    g.handlers[("GET", f"{page}/scheduled_posts")] = listing
    g.handlers[("GET", f"{page}/posts")] = posts
    g.handlers[("GET", f"{post_id}?is_published,created_time,scheduled_publish_time")] = (
        lambda params: FakeResp(200, {"id": post_id, "is_published": state["published"],
                                      "created_time": state["created"]}))
    g.handlers[("DELETE", post_id)] = delete
    g.handlers[("POST", post_id)] = unpublish
    return state


def fail_deletes(state: dict, response: Any = None) -> None:
    """DELETE(예약 취소) 실패 주입 — 기본 4xx(code 100). None 을 넘기려면 state["delete_fail"]=None 으로 되돌린다."""
    state["delete_fail"] = response if response is not None else err(100)

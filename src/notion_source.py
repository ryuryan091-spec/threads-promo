"""Notion Tracker DB 에서 회차 기록을 가져온다.

설계 원칙 — 안전 기본값
  DB 스키마를 모르는 상태에서 컬럼명을 추측하면 운영 중 깨진다.
  따라서 타입 기준으로 안전한 것만 뽑고, 나머지는 명시적 허용목록으로만 연다.

  기본 채택 : title / select / status / multi_select  (짧은 범주값)
  기본 배제 : number / rich_text / formula / rollup / people / url / email
              -> 시장 수치·벤더명·실존명이 섞일 수 있는 필드

  EDT SECURITY v1.4 는 실존인물·브랜드·데이터벤더명 노출을 금지한다.
  또한 구체적 시장 수치를 Threads 본문에 실으면 투자조언 해석 여지가 생긴다.
  그래서 추출 후에도 한 번 더 살균한다.
"""

from __future__ import annotations

import logging
import re

import requests

from . import config

VERSION = "1.0.0"

log = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 타입 기준 기본 채택 목록. 짧은 범주값이라 수치 유출 위험이 낮다.
SAFE_PROPERTY_TYPES = ("title", "select", "status", "multi_select")

# 타입은 안전하지만 내용상 차단해야 하는 컬럼.
#   EDT 에피소드 트래커의 '메인 히어로' / '활성 빌런' 은 select 타입이라
#   타입 게이트를 통과하지만, 값이 캐릭터 고유명(Debt Titan 등)이다.
#   STORY 프롬프트는 "캐릭터 고유명 대신 빌런/히어로로 지칭"을 지시하므로
#   데이터에 고유명이 들어가면 지시와 데이터가 모순된다.
#   캐논 오염을 막기 위해 코드에서 차단한다.
#   허용목록(NOTION_FIELD_ALLOWLIST)에 명시하면 이 차단을 해제할 수 있다.
DENY_PROPERTY_NAMES = ("메인 히어로", "활성 빌런", "소스 패키지")

# 살균 대상 — 수치·통화·퍼센트
_NUMERIC = re.compile(
    r"("
    r"[-+]?\d[\d,]*\.?\d*\s*(%|퍼센트|bp|bps|달러|원|엔|위안)"  # 단위 붙은 수치
    r"|\$\s?[-+]?\d[\d,]*\.?\d*"                                # 통화 기호
    r"|[-+]?\d[\d,]*\.\d+"                                      # 소수
    r")"
)

# 회차 번호처럼 보존해야 하는 패턴은 살균에서 제외한다.
_KEEP = re.compile(r"^(Ep\d+|EX-\d+|Day\s?\d+|\d+일차|Arc\s?\d+)$", re.IGNORECASE)


class NotionSourceError(RuntimeError):
    """조회 실패. 호출자는 근거 없이 진행해야 한다."""


def _allowlist() -> set[str]:
    raw = config.NOTION_FIELD_ALLOWLIST
    return {name.strip() for name in raw.split(",") if name.strip()}


def sanitize(value: str) -> str:
    """수치·통화를 제거한다. 회차 번호 같은 식별자는 남긴다."""
    text = value.strip()
    if not text:
        return ""
    if _KEEP.match(text):
        return text
    cleaned = _NUMERIC.sub("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,·-")


def _extract_value(prop: dict) -> str:
    """Notion 속성 하나에서 표시 문자열을 뽑는다."""
    ptype = prop.get("type", "")

    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if ptype == "select":
        sel = prop.get("select") or {}
        return str(sel.get("name", ""))
    if ptype == "status":
        st = prop.get("status") or {}
        return str(st.get("name", ""))
    if ptype == "multi_select":
        return ", ".join(
            str(item.get("name", "")) for item in prop.get("multi_select", [])
        )
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if ptype == "number":
        value = prop.get("number")
        return "" if value is None else str(value)
    return ""


def _row_to_line(props: dict, allow: set[str]) -> str:
    """행 하나를 한 줄 요약으로 만든다."""
    parts: list[str] = []

    for name, prop in props.items():
        ptype = prop.get("type", "")

        # 허용목록에 없는 한, 이름 기반 차단이 타입 채택보다 우선한다.
        if name in DENY_PROPERTY_NAMES and name not in allow:
            continue

        permitted = ptype in SAFE_PROPERTY_TYPES or name in allow
        if not permitted:
            continue

        raw = _extract_value(prop)
        if not raw:
            continue

        clean = sanitize(raw)
        if not clean:
            continue

        # title 은 이름 없이, 나머지는 컬럼명과 함께
        parts.append(clean if ptype == "title" else f"{name}={clean}")

    return " / ".join(parts)


def fetch_episodes(token: str, database_id: str, limit: int) -> list[str]:
    """Tracker DB 최근 행을 한 줄 요약 목록으로 돌려준다.

    실패해도 예외를 올리지 않는다. 근거가 없으면 없는 대로 진행하는 것이
    발행을 멈추는 것보다 낫다.
    """
    if not token or not database_id:
        log.info("Notion 설정 없음 — 회차 근거 없이 진행합니다.")
        return []

    try:
        resp = requests.post(
            f"{NOTION_API}/databases/{database_id}/query",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            json={
                "page_size": max(1, min(limit, 20)),
                "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            },
            timeout=config.HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        log.warning("Notion 조회 네트워크 오류 — 근거 없이 진행: %s", exc)
        return []

    if resp.status_code != 200:
        hint = ""
        if resp.status_code == 401:
            hint = " — NOTION_TOKEN 이 잘못되었거나 만료되었습니다."
        elif resp.status_code == 404:
            hint = (
                " — NOTION_DB_ID 가 잘못되었거나, 통합이 이 DB 에 "
                "연결되지 않았습니다(Notion 페이지 > 연결 추가)."
            )
        log.warning(
            "Notion 조회 실패 %s%s: %s",
            resp.status_code, hint, resp.text[:200],
        )
        return []

    allow = _allowlist()
    lines: list[str] = []
    for row in resp.json().get("results", []):
        line = _row_to_line(row.get("properties", {}) or {}, allow)
        if line:
            lines.append(line[:120])

    log.info("Notion 회차 기록 %d건 수집 (허용목록 %d개)", len(lines), len(allow))
    return lines

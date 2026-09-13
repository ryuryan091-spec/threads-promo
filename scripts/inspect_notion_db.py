"""Notion Tracker DB 스키마와 추출 결과를 미리 확인한다.

실제 발행에 쓰기 전에 무엇이 프롬프트로 들어가는지 눈으로 봐야 한다.
컬럼명을 추측해서 코드를 쓰지 않기 위한 도구다.

사용
  NOTION_TOKEN=... NOTION_DB_ID=... python scripts/inspect_notion_db.py
  NOTION_FIELD_ALLOWLIST="비고,메모" python scripts/inspect_notion_db.py

출력
  1. DB 이름과 전체 컬럼 목록 (타입, 기본 채택 여부)
  2. 최근 행에서 실제로 추출되는 문자열
  3. 살균으로 제거된 내용
"""

from __future__ import annotations

import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import notion_source  # noqa: E402

VERSION = "1.0.0"


def main() -> int:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DB_ID", "").strip()

    if not token or not db_id:
        print("[FAIL] NOTION_TOKEN / NOTION_DB_ID 환경변수를 설정하십시오.")
        return 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": notion_source.NOTION_VERSION,
    }

    print(f"[InspectNotionDB] v{VERSION}\n")

    # 1) 스키마
    meta = requests.get(
        f"{notion_source.NOTION_API}/databases/{db_id}", headers=headers, timeout=20
    )
    if meta.status_code != 200:
        hint = ""
        if meta.status_code == 401:
            hint = " — NOTION_TOKEN 확인"
        elif meta.status_code == 404:
            hint = " — DB ID 오류이거나 통합이 DB에 연결되지 않았습니다"
        print(f"[FAIL] 스키마 조회 실패 {meta.status_code}{hint}")
        print(meta.text[:300])
        return 1

    body = meta.json()
    title = "".join(t.get("plain_text", "") for t in body.get("title", []))
    props = body.get("properties", {})

    print(f"DB 이름 : {title}")
    print(f"컬럼 수 : {len(props)}\n")
    print(f"{'컬럼명':24s} {'타입':16s} {'기본채택':8s}")
    print("-" * 52)
    for name, prop in props.items():
        ptype = prop.get("type", "")
        default = "O" if ptype in notion_source.SAFE_PROPERTY_TYPES else "-"
        print(f"{name[:24]:24s} {ptype:16s} {default:8s}")

    excluded = [
        n for n, p in props.items()
        if p.get("type") not in notion_source.SAFE_PROPERTY_TYPES
    ]
    if excluded:
        print(
            "\n기본 배제된 컬럼입니다. 필요하면 NOTION_FIELD_ALLOWLIST 에 "
            "쉼표로 나열하십시오:\n  " + ", ".join(excluded)
        )

    # 2) 실제 추출 결과
    print("\n" + "=" * 52)
    print("프롬프트에 실제로 들어갈 문자열")
    print("=" * 52)
    lines = notion_source.fetch_episodes(token, db_id, 6)
    if not lines:
        print("  (추출된 행 없음)")
    for line in lines:
        print(f"  - {line}")

    # 3) 살균 확인
    print("\n" + "=" * 52)
    print("살균 동작 확인 (수치·통화 제거)")
    print("=" * 52)
    for sample in (
        "10Y 4.57%",
        "WTI $81.77",
        "VIX 18.77 / HY OAS 271bp",
        "Ep61",
        "BATTLE",
        "Arc Day 16",
    ):
        print(f"  {sample:28s} -> {notion_source.sanitize(sample)!r}")

    print(
        "\n확인 사항\n"
        "  1. 위 문자열에 시장 수치가 남아 있지 않은지\n"
        "  2. 실존 인물·브랜드·데이터벤더명이 없는지\n"
        "  3. 캐릭터 고유명이 있다면 프롬프트가 '빌런/히어로'로 바꿔 쓰는지\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

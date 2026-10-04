"""Facebook 숏폼 사전 점검 — 읽기 전용 (v1.8.6, DESIGN_V18_FACE_STORY.md §10.5).

실행
  Actions → 📘 Facebook Preflight → Run workflow
  로컬: python scripts/face_preflight.py

원칙
  - Facebook·Notion 에 아무것도 쓰지 않는다. GET 과 Notion 질의(읽기)만 한다.
  - 토큰 값은 출력하지 않는다(redact).
  - FAIL 이 하나라도 있으면 종료코드 1. WARN 은 게시는 가능하나 확인이 필요한 항목.

검사 항목
  P1 필수 Secret 존재 (FACE_PAGE_ID · FACE_PAGE_TOKEN · NOTION_TOKEN · FACE_NOTION_DB_ID)
  P2 운영 Variables — 정기 실행이 Facebook 게시까지 가는 값인지
  P3 앞으로 7일 계획 (숏폼 휴식일 · Facebook 하루 편수)
  P4 페이지 접근            GET /{page_id}?fields=id,name
  P5 릴스 목록 description  GET /{page_id}/video_reels?fields=id,description,updated_time&limit=3
  P6 원장 DB 스키마         Notion GET databases/{id} ↔ face_story.SCHEMA
  P7 원장 DB 질의           Notion POST databases/{id}/query (page_size=1)
  P8 게시 권한              읽기 요청으로 확인하는 공식 방법이 없어 표시만(첫 실제 게시에서 확인)
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import config, face_story, safety, shorts_plan  # noqa: E402
from src.face_client import FaceApiError, FaceClient  # noqa: E402
from src.redact import redact  # noqa: E402

VERSION = "1.0.0"
KST = ZoneInfo("Asia/Seoul")
OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
REQUIRED_SECRETS = ("FACE_PAGE_ID", "FACE_PAGE_TOKEN", "NOTION_TOKEN", "FACE_NOTION_DB_ID")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, cid: str, name: str, status: str, detail: str) -> None:
        self.rows.append((cid, name, status, redact(detail)))
        print(f"[{status:<4}] {cid:<3} {name} — {redact(detail)}")

    @property
    def failed(self) -> bool:
        return any(r[2] == FAIL for r in self.rows)

    def markdown(self) -> str:
        lines = ["| # | 항목 | 결과 | 내용 |", "|---|---|---|---|"]
        lines += [f"| {c} | {n} | {s} | {d.replace('|', '/')} |" for c, n, s, d in self.rows]
        return "\n".join(lines)


def check_secrets(r: Report) -> bool:
    missing = [n for n in REQUIRED_SECRETS if not _env(n)]
    if missing:
        r.add("P1", "필수 Secret", FAIL, f"없음: {', '.join(missing)}")
        return False
    r.add("P1", "필수 Secret", OK, f"{len(REQUIRED_SECRETS)}개 존재(값은 출력하지 않음)")
    return True


def variable_warnings(today: dt.date) -> list[str]:
    """정기 실행(KST 08:19)이 Facebook 게시까지 가지 못하게 하는 설정값."""
    out = []
    if _env("DRY_RUN").lower() not in ("false", "0", "no"):
        out.append(f"DRY_RUN={_env('DRY_RUN') or '(미설정→true)'} — 정기 실행이 dry_run(게시 안 함)")
    if not safety.automation_enabled():
        out.append("AUTOMATION_ENABLED=false — 킬 스위치(게시 안 함)")
    if not config.FACE_ENABLED:
        out.append("FACE_ENABLED=false — Facebook 게시 꺼짐")
    if not config.SHORTS_BUILD_ENABLED:
        out.append("SHORTS_BUILD_ENABLED=false — 영상을 만들지 않음")
    if not config.FACE_STORY_ENABLED:
        out.append("FACE_STORY_ENABLED=false — 원장 미사용(연속성·원장 재게시 방지 없음)")
    start = shorts_plan.ramp_start()
    if start is None:
        out.append(f"FACE_RAMP_START={config.FACE_RAMP_START or '(미설정)'} — Facebook 하루 0편")
    elif start > today:
        out.append(f"FACE_RAMP_START={start.isoformat()} — 시작 전(그때까지 Facebook 0편)")
    return out


def check_variables(r: Report, today: dt.date) -> None:
    warns = variable_warnings(today)
    if warns:
        r.add("P2", "운영 Variables", WARN, " · ".join(warns))
    else:
        r.add("P2", "운영 Variables", OK, "DRY_RUN=false · 자동화·Facebook·영상 생성·원장 켜짐 · 램프 시작됨")


def check_plan(r: Report, today: dt.date) -> None:
    days = []
    for i in range(1, 8):
        d = today + dt.timedelta(days=i)
        n = 0 if shorts_plan.is_rest_day(d) else shorts_plan.face_daily_target(d)
        tag = "휴식" if shorts_plan.is_rest_day(d) else f"{n}편"
        days.append(f"{d.strftime('%m/%d')}({'월화수목금토일'[d.weekday()]}) {tag}")
    r.add("P3", "앞으로 7일 Facebook 계획", INFO, " · ".join(days))


def check_facebook(r: Report) -> None:
    page_id = _env("FACE_PAGE_ID")
    client = FaceClient(page_id, _env("FACE_PAGE_TOKEN"))
    try:
        info = client.page_info()
    except FaceApiError as exc:
        r.add("P4", "페이지 접근", FAIL, f"{exc}")
        r.add("P5", "릴스 목록 description", FAIL, "P4 실패로 건너뜀")
        return
    got = str(info.get("id") or "")
    if got != page_id:
        r.add("P4", "페이지 접근", FAIL, f"응답 id 가 FACE_PAGE_ID 와 다름 (응답 {got or '-'})")
    else:
        r.add("P4", "페이지 접근", OK, f"페이지 '{info.get('name', '-')}' 접근됨")
    try:
        items = client.list_reels(limit=3)
    except FaceApiError as exc:
        r.add("P5", "릴스 목록 description", FAIL, f"{exc}")
        return
    if not items:
        r.add("P5", "릴스 목록 description", WARN, "릴스 0건 — description 반환 여부 판단 불가(첫 게시 뒤 다시 실행)")
        return
    with_desc = sum(1 for i in items if "description" in i)
    if with_desc == 0:
        r.add("P5", "릴스 목록 description", FAIL,
              f"{len(items)}건 모두 description 없음 — 중복 게시 방지(캡션 비교)가 동작하지 않음")
    else:
        r.add("P5", "릴스 목록 description", OK, f"{len(items)}건 중 {with_desc}건 description 반환")


def check_notion(r: Report) -> None:
    try:
        ledger = face_story.Ledger(_env("NOTION_TOKEN"), config.FACE_NOTION_DB_ID or _env("FACE_NOTION_DB_ID"))
        issues = ledger.schema_issues()
    except face_story.LedgerError as exc:
        r.add("P6", "원장 DB 스키마", FAIL, f"{exc}")
        r.add("P7", "원장 DB 질의", FAIL, "P6 실패로 건너뜀")
        return
    if issues:
        r.add("P6", "원장 DB 스키마", FAIL, " · ".join(issues))
    else:
        r.add("P6", "원장 DB 스키마", OK, f"속성 {len(face_story.SCHEMA)}개 이름·타입 일치")
    try:
        n = ledger.probe()
    except face_story.LedgerError as exc:
        r.add("P7", "원장 DB 질의", FAIL, f"{exc}")
        return
    r.add("P7", "원장 DB 질의", OK, f"질의 성공(Notion-Version {face_story.NOTION_VERSION}) · 반환 {n}행")


def main() -> int:
    print(f"[FacePreflight] v{VERSION} 시작 (config v{config.VERSION})")
    today = dt.datetime.now(KST).date()
    r = Report()
    secrets_ok = check_secrets(r)
    check_variables(r, today)
    check_plan(r, today)
    if secrets_ok:
        check_facebook(r)
        check_notion(r)
    r.add("P8", "게시 권한", INFO, "읽기 요청으로 확인하는 공식 방법 없음 — 첫 실제 게시(publish 결과 알림)로 확인")

    summary = r.markdown()
    print("\n" + summary)
    path = _env("GITHUB_STEP_SUMMARY")
    if path:
        with pathlib.Path(path).open("a", encoding="utf-8") as fh:
            fh.write("## Facebook Preflight\n\n" + summary + "\n")
    if r.failed:
        print("\nFAIL 항목이 있습니다. 첫 게시 전에 조치하십시오.")
        return 1
    print("\nFAIL 없음. WARN 항목을 확인하십시오.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

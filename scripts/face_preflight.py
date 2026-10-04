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
  P8 게시 권한              실제 게시 성공은 첫 게시(publish 결과 알림)로만 확인된다
  P9 토큰 정보·권한(v1.1.0) GET /debug_token?input_token=<페이지 토큰>
                            유효 여부 · 토큰 종류 · 만료 시각 · 페이지 일치 · 권한(scopes)
                            필요 권한 = Reels Publishing 문서: pages_show_list · pages_read_engagement · pages_manage_posts
                            문서가 호출 토큰 종류를 명시하지 않아 조회 실패는 WARN(판단 불가)으로 둔다.
  실행 정보(v1.1.0)         실행 시각(KST) · 커밋 · 브랜치 · 워크플로 · 요청 경로(토큰 제외)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import config, face_story, safety, shorts_plan  # noqa: E402
from src.face_client import FaceApiError, FaceClient  # noqa: E402
from src.redact import redact  # noqa: E402

VERSION = "1.1.0"   # v1.8.7: 토큰 정보·권한(P9) · 실행 정보 · 요청 경로 로그
KST = ZoneInfo("Asia/Seoul")
OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
REQUIRED_SECRETS = ("FACE_PAGE_ID", "FACE_PAGE_TOKEN", "NOTION_TOKEN", "FACE_NOTION_DB_ID")
# Reels Publishing 가이드 'Requirements' 의 권한(페이지 CREATE_CONTENT 작업 권한은 토큰 scopes 로 보이지 않는다)
REQUIRED_SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts")
EXPIRY_WARN_DAYS = 7


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


def run_info(now: dt.datetime) -> str:
    """실행 정보 한 줄(Actions 기본 환경변수 — 없으면 '-')."""
    sha = _env("GITHUB_SHA")[:7] or "-"
    return (f"실행 {now.strftime('%Y-%m-%d %H:%M:%S')} KST · config v{config.VERSION} · 점검 v{VERSION} · "
            f"커밋 {sha} · 브랜치 {_env('GITHUB_REF_NAME') or '-'} · 워크플로 {_env('GITHUB_WORKFLOW') or '로컬'} · "
            f"Graph {config.FACE_GRAPH_BASE.rsplit('/', 1)[-1]} · Notion-Version {face_story.NOTION_VERSION}")


def _req(path: str) -> None:
    """요청 경로 로그(토큰·시크릿 없음)."""
    print(f"       ↳ 요청 GET {path}")


def _ts(value) -> dt.datetime | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(n, dt.UTC).astimezone(KST) if n > 0 else None


def token_findings(data: dict, page_id: str, now: dt.datetime) -> tuple[str, str]:
    """debug_token data → (판정, 내용). 판정 근거는 응답 필드 값만 쓴다."""
    if not data:
        return WARN, "응답에 data 없음 — 판단 불가"
    parts, status = [], OK
    if data.get("is_valid") is not True:
        err = (data.get("error") or {}).get("message", "")
        return FAIL, f"is_valid={data.get('is_valid')} {err}".strip()
    parts.append(f"유효 · 종류 {data.get('type', '-')} · 앱 {data.get('application', '-')}")
    profile = str(data.get("profile_id") or "")
    if profile and profile != page_id:
        status = FAIL
        parts.append(f"profile_id({profile}) ≠ FACE_PAGE_ID")
    exp_raw = data.get("expires_at")
    exp = _ts(exp_raw)
    if exp is None:
        parts.append(f"expires_at={exp_raw!r}(만료 시각 값 없음)")
    else:
        days = (exp - now).total_seconds() / 86400
        parts.append(f"만료 {exp.strftime('%Y-%m-%d %H:%M')} KST(남은 {days:.1f}일)")
        if days <= 0:
            status = FAIL
        elif days <= EXPIRY_WARN_DAYS and status == OK:
            status = WARN
            parts.append("단기 토큰으로 보임 — 장기 페이지 토큰으로 교체 권장")
    scopes = [str(x) for x in (data.get("scopes") or [])]
    missing = [s for s in REQUIRED_SCOPES if s not in scopes]
    if missing:
        status = FAIL
        parts.append(f"필요 권한 없음: {', '.join(missing)}")
    else:
        parts.append(f"필요 권한 3개 있음({', '.join(REQUIRED_SCOPES)})")
    return status, " · ".join(parts)


def check_facebook(r: Report, now: dt.datetime) -> None:
    page_id = _env("FACE_PAGE_ID")
    client = FaceClient(page_id, _env("FACE_PAGE_TOKEN"))
    _req("/{page_id}?fields=id,name")
    try:
        info = client.page_info()
    except FaceApiError as exc:
        r.add("P4", "페이지 접근", FAIL, f"{exc}{_hint(exc)}")
        r.add("P5", "릴스 목록 description", FAIL, "P4 실패로 건너뜀")
        info = None
    if info is not None:
        got = str(info.get("id") or "")
        if got != page_id:
            r.add("P4", "페이지 접근", FAIL, f"응답 id 가 FACE_PAGE_ID 와 다름 (응답 {got or '-'})")
        else:
            r.add("P4", "페이지 접근", OK, f"페이지 '{info.get('name', '-')}' 접근됨")
        _req(f"/{{page_id}}/video_reels?fields={config.FACE_REELS_LIST_FIELDS}&limit=3")
        try:
            _check_reels(r, client.list_reels(limit=3))
        except FaceApiError as exc:
            r.add("P5", "릴스 목록 description", FAIL, f"{exc}{_hint(exc)}")
    # P4 가 실패해도 시도한다 — 원인이 권한이면 scopes 로 무엇이 빠졌는지 보인다.
    _req("/debug_token?input_token=<페이지 토큰>")
    try:
        data = client.token_debug()
    except FaceApiError as exc:
        r.add("P9", "토큰 정보·권한", WARN, f"debug_token 조회 실패 — 판단 불가(문서가 호출 토큰 종류를 명시하지 않음) {exc}")
        return
    status, detail = token_findings(data, page_id, now)
    r.add("P9", "토큰 정보·권한", status, detail)


def _subcode(exc: FaceApiError) -> int | None:
    """오류 응답 JSON 의 error.error_subcode (없거나 파싱 불가면 None)."""
    try:
        sub = (json.loads(exc.payload).get("error") or {}).get("error_subcode")
    except (ValueError, AttributeError):
        return None
    return sub if isinstance(sub, int) else None


def _hint(exc: FaceApiError) -> str:
    """흔한 오류의 조치 안내(오류 코드 기준). subcode 463 = 실환경 응답 메시지 'Session has expired'."""
    if exc.is_auth_error and _subcode(exc) == 463:
        return " → 토큰 만료: 장기 페이지 토큰 재발급 후 FACE_PAGE_TOKEN 교체(OPERATIONS.md v1.8.7)"
    if exc.is_auth_error:
        return " → 토큰 무효: 장기 페이지 토큰 재발급 후 FACE_PAGE_TOKEN 교체"
    if exc.code == 200:
        return " → 권한 문제: 앱 권한(pages_manage_posts 등)·페이지 역할 확인"
    return ""


def _check_reels(r: Report, items: list[dict]) -> None:
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
    now = dt.datetime.now(KST)
    today = now.date()
    print(f"[FacePreflight] v{VERSION} 시작")
    print(f"[FacePreflight] {run_info(now)}")
    r = Report()
    secrets_ok = check_secrets(r)
    check_variables(r, today)
    check_plan(r, today)
    if secrets_ok:
        check_facebook(r, now)
        if safety.tripped() is not None:
            print("       ↳ 참고: 위 '회로 차단' 로그는 공통 Facebook 모듈의 기본 메시지입니다. 이 점검은 쓰기를 하지 않습니다.")
        check_notion(r)
    r.add("P8", "게시 성공", INFO, "실제 업로드·처리·게시 성공은 첫 실제 게시(publish 결과 알림)로만 확인 — P9 는 권한 보유 여부만")

    summary = r.markdown()
    print("\n" + summary)
    path = _env("GITHUB_STEP_SUMMARY")
    if path:
        with pathlib.Path(path).open("a", encoding="utf-8") as fh:
            fh.write("## Facebook Preflight\n\n" + run_info(now) + "\n\n" + summary + "\n")
    if r.failed:
        print("\nFAIL 항목이 있습니다. 첫 게시 전에 조치하십시오.")
        return 1
    print("\nFAIL 없음. WARN 항목을 확인하십시오.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

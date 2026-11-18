"""
p76/run_health.py
=================
매일 점검 (FR-07, DSN §6-6). 읽기만 한다. PAGE76_ENABLED=false 여도 실행한다(토큰 무효를 24시간 안에 탐지).

  H1 페이지 접근        GET /{page-id}                      실패 → FAIL(최우선, 두 레포 토큰 교체 안내)
  H2 토큰 정보          debug_token: 유효·PAGE·만료 없음·권한  권한 누락·추가 모두 WARN (SEC C-01)
  H3 인사이트 지표      PAGE76_INSIGHTS_METRICS 가 있을 때만   read_insights 없으면 SKIP
  H4 댓글 권한          (모더레이션 미구현 — 권한 발급 후)     SKIP
  H5 노션 스키마        DB-05(읽기 속성)·DB-08·DB-09·DB-11      불일치 → FAIL
  H6 휴재 중 본편 승인  오늘 DB-09 ≠ 본편 AND DB-05 승인 행      WARN
  H7 예약 작업 누락     오늘 DB-09 = 본편 #N AND DB-05 에 #N 없음 WARN

  사건 소설 감시 (DN2026_0119, DSN-P76-NOVEL-003 §9) — PAGE76_DB12_ID 없으면 생략(정보 로그). 읽기만.
  H-N1 바이블 상태      DB-12 바이블 상태 ∉ {정본·생성중·검사실패·중단·폐기}   WARN(정보, 생성 측 자동 재평가)
  H-N2 내일 회차 미준비  KST 18시 이후 AND 내일 게시예정 회차 ∉ {승인·발행예고·게시완료} WARN(정보)
  H-N4 소설 산출 정체    DB-11 24시간 novel 실행 ≥1 전부 쓰기 0 AND 오늘~D+3 회차가 승인 이전  FAIL(경보, RC-3)
  H-N5 토큰 만료 임박    debug_token is_valid=false 또는 expires_at>0 이고 14일 이내          FAIL(경보, 사람 조치)
  일일 요약             DB-12 상태별 행 수·오늘/내일 회차 상태·이번 달 비용·DB-11 24시간 상태 건수 (정보)

사용: python -m p76.run_health
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from p76 import cli, graph, ledger, notion, novel_store, settings
from p76.notify import send
from p76.runlog import RunLog

VERSION = "1.1.0"  # 1.1.0: DN2026_0119 사건 소설 감시 H-N1·H-N2·H-N4·H-N5 + 일일 요약 (DSN-P76-NOVEL-003 §9)

logger = logging.getLogger(__name__)

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"

# ── 사건 소설 감시 상수 (DN2026_0119) — settings 가 아니라 이 모듈에 둔다 ──
S = settings
BIBLE_OK_STATES = frozenset({S.NS_CANON, S.NS_GENERATING, S.NS_CHECK_FAILED, S.NS_STOPPED, S.NS_DISCARDED})
EPISODE_READY_STATES = frozenset({S.NS_APPROVED, S.NS_SCHEDULED, S.NS_PUBLISHED})
# H-N4 '승인 이전'에서 뺀다: 준비 완료(+확인필요 = 발행예고 이후) · 의도적 중단·폐기
EPISODE_NOT_PENDING_STATES = EPISODE_READY_STATES | {S.NS_CHECK, S.NS_STOPPED, S.NS_DISCARDED}
NOVEL_PIPELINE = "novel"
HN2_FROM_HOUR = 18           # H-N2: 실행 시각 KST 18시 이후만
HN4_LOOKBACK_HOURS = 24      # H-N4·요약: DB-11 최근 24시간
HN4_TARGET_DAYS = 3          # H-N4: 게시예정일 오늘~D+3
HN5_EXPIRY_DAYS = 14         # H-N5: 만료 D-14
TOKEN_HINT_HIDS = ("H1", "H2", "H-N5")  # 토큰 교체 안내를 붙이는 항목(사람 조치 문구는 이것만)
URGENT_HIDS = TOKEN_HINT_HIDS + ("H-N4",)


@dataclass
class Check:
    hid: str
    level: str
    code: str = ""
    detail: str = ""  # 로그·알림에만 붙인다(키·상태·건수·날짜만 — 원고·키 값·노션 ID 금지, SEC RR-06)

    @property
    def text(self) -> str:
        return f"{self.code}({self.detail})" if self.detail else self.code


def today_kst() -> date:
    return datetime.now(notion.KST).date()


def h1_page() -> Check:
    if not settings.PAGE_ID or not settings.PAGE_TOKEN:
        return Check("H1", FAIL, "not_configured")
    ok, _, reason = graph.page_info()
    return Check("H1", OK) if ok else Check("H1", FAIL, reason)


def h2_token() -> list[Check]:
    return h2_token_data()[0]


def h2_token_data() -> tuple[list[Check], dict | None]:
    """(H2 결과, debug_token data — 조회 실패면 None). H-N5 가 같은 응답을 다시 쓴다(중복 호출 없음)."""
    ok, data, reason = graph.debug_token()
    if not ok:
        return [Check("H2", FAIL, f"debug_token:{reason}")], None
    out = []
    if not data.get("is_valid"):
        out.append(Check("H2", FAIL, "invalid"))
    if str(data.get("type", "")).upper() != "PAGE":
        out.append(Check("H2", WARN, f"type:{data.get('type')}"))
    if data.get("expires_at") not in (0, None):
        out.append(Check("H2", WARN, "expires"))
    scopes = set(data.get("scopes") or [])
    expected = set(settings.EXPECTED_SCOPES)
    missing, extra = sorted(expected - scopes), sorted(scopes - expected)
    if missing:
        out.append(Check("H2", WARN, "scope_missing:" + "+".join(missing)))
    if extra:
        out.append(Check("H2", WARN, "scope_extra:" + "+".join(extra)))
    return out or [Check("H2", OK)], data


def h3_insights(latest_feed_id: str) -> Check:
    if not settings.INSIGHTS_METRICS:
        return Check("H3", SKIP, "no_metrics(read_insights)")
    if not latest_feed_id:
        return Check("H3", SKIP, "no_post")
    _, codes = graph.post_insights(latest_feed_id, settings.INSIGHTS_METRICS)
    return Check("H3", WARN if codes else OK, ";".join(codes)[:200])


def h5_schema() -> list[Check]:
    out = []
    for name, db_id, required in (
        ("DB05", settings.DB05_ID, settings.DB05_REQUIRED),
        ("DB08", settings.DB08_ID, settings.DB08_REQUIRED),
        ("DB09", settings.DB09_ID, settings.DB09_REQUIRED),
        ("DB11", settings.DB11_ID, settings.DB11_REQUIRED),
    ):
        try:
            diff = notion.schema_mismatches(notion.retrieve_database(db_id), required)
        except notion.NotionError as e:
            out.append(Check("H5", FAIL, f"{name}:{e}"))
            continue
        if diff:
            out.append(Check("H5", FAIL, f"{name}:" + ",".join(diff)[:150]))
    return out or [Check("H5", OK)]


def h6_h7(today: date) -> list[Check]:
    try:
        row = ledger.calendar_rows(today, today).get(today.isoformat())
    except notion.NotionError as e:
        return [Check("H6", WARN, f"db09:{e}")]
    if row is None:
        return [Check("H6", WARN, "calendar_row_missing")]
    kind = notion.select(row, settings.C_KIND)
    out = []
    try:
        if kind != settings.KIND_EPISODE:
            approved = ledger.approved_episode_numbers()
            out.append(Check("H6", WARN, "approved_during_hiatus:" + ",".join(f"#{n:03d}" for n in approved))
                       if approved else Check("H6", OK))
        else:
            number = notion.number(row, settings.C_NO)
            if number is not None and not ledger.episode_exists(number):
                out.append(Check("H7", WARN, f"draft_missing:#{number:03d}"))
            else:
                out.append(Check("H7", OK))
    except notion.NotionError as e:
        out.append(Check("H6", WARN, f"db05:{e}"))
    return out


# ── 사건 소설 감시 (DN2026_0119, DSN-P76-NOVEL-003 §9) ──────────
def hn5_token(data: dict | None, now: datetime) -> Check:
    if data is None:
        return Check("H-N5", SKIP, "no_debug_token")
    if not data.get("is_valid"):
        return Check("H-N5", FAIL, "token_invalid")
    try:
        expires = int(data.get("expires_at") or 0)
    except (TypeError, ValueError):
        return Check("H-N5", WARN, "expires_at_unreadable")
    if expires > 0:
        left = datetime.fromtimestamp(expires, UTC) - now.astimezone(UTC)
        if left <= timedelta(days=HN5_EXPIRY_DAYS):
            return Check("H-N5", FAIL, "token_expiring", f"D-{max(left.days, 0)}")
    return Check("H-N5", OK)


def _episodes(rows: list[dict]) -> list[dict]:
    return [p for p in rows if novel_store.kind(p) == S.NK_EPISODE]


def hn1_bible(rows: list[dict]) -> Check:
    bad = [f"{novel_store.key(p)}={novel_store.state(p) or '-'}" for p in rows
           if novel_store.kind(p) == S.NK_BIBLE and novel_store.state(p) not in BIBLE_OK_STATES]
    if bad:
        return Check("H-N1", WARN, "bible_state", ",".join(bad)[:150])
    return Check("H-N1", OK)


def hn2_tomorrow(rows: list[dict], now_kst: datetime) -> Check:
    if now_kst.hour < HN2_FROM_HOUR:
        return Check("H-N2", SKIP, f"before_{HN2_FROM_HOUR}h")
    tomorrow = now_kst.date() + timedelta(days=1)
    bad = [f"{novel_store.key(p)}={novel_store.state(p) or '-'}" for p in _episodes(rows)
           if notion.kst_date(p, S.N_DUE) == tomorrow and novel_store.state(p) not in EPISODE_READY_STATES]
    if bad:
        return Check("H-N2", WARN, "tomorrow_unready", ",".join(bad)[:150])
    return Check("H-N2", OK)


def recent_runs(now: datetime) -> list[dict]:
    """DB-11 최근 24시간 실행 행 (읽기만)."""
    since = now.astimezone(UTC) - timedelta(hours=HN4_LOOKBACK_HOURS)
    body = {"filter": {"property": S.R_START, "date": {"on_or_after": since.astimezone(notion.KST).date().isoformat()}},
            "page_size": 100}
    out = []
    for page in notion.query(S.DB11_ID, body, max_pages=5):
        started = notion.date_value(page, S.R_START)
        if started is not None and started >= since:
            out.append(page)
    return out


def hn4_stalled(rows: list[dict], runs: list[dict], today: date) -> Check:
    novel = [r for r in runs if notion.select(r, S.R_PIPELINE) == NOVEL_PIPELINE]
    if not novel:
        return Check("H-N4", SKIP, "no_novel_run_24h")
    if any((notion.number(r, S.R_WRITES) or 0) > 0 for r in novel):
        return Check("H-N4", OK)
    until = today + timedelta(days=HN4_TARGET_DAYS)
    pending = []
    for p in _episodes(rows):
        due = notion.kst_date(p, S.N_DUE)
        if due is not None and today <= due <= until and novel_store.state(p) not in EPISODE_NOT_PENDING_STATES \
                and not notion.checkbox(p, S.N_STOP):
            pending.append(f"{novel_store.key(p)}={novel_store.state(p) or '-'}")
    if not pending:
        return Check("H-N4", OK)
    causes = sorted({(notion.plain(r, S.R_ERROR) or "-")[:40] for r in novel})
    detail = f"runs={len(novel)} writes=0 targets={','.join(pending)} causes={'|'.join(causes)}"
    return Check("H-N4", FAIL, "novel_stalled", detail[:300])


def _counts(counter: Counter) -> str:
    return " ".join(f"{k}{v}" for k, v in sorted(counter.items())) or "없음"


def novel_summary(rows: list[dict], runs: list[dict] | None, now_kst: datetime) -> str:
    """일일 요약(정보, 조치 불필요). 키·상태·건수·금액만."""
    today, tomorrow = now_kst.date(), now_kst.date() + timedelta(days=1)
    states = Counter(novel_store.state(p) or "-" for p in rows if novel_store.kind(p) != S.NK_CONTROL)

    def day_line(d: date) -> str:
        eps = [f"{novel_store.key(p)} {novel_store.state(p) or '-'}" for p in _episodes(rows)
               if notion.kst_date(p, S.N_DUE) == d]
        return ",".join(eps) or "예정 없음"

    month = now_kst.strftime("%Y-%m")
    ctrl = novel_store.find(rows, novel_store.CONTROL_KEY)  # control() 은 없으면 행을 만든다 — 쓰지 않는다
    if ctrl is None:
        cost = "미확인(CONTROL 행 없음)"
    else:
        try:
            data = json.loads(notion.plain(ctrl, S.N_RESULT) or "{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            cost = "미확인(CONTROL 결과코드 형식)"
        elif data.get("month") != month:
            cost = f"{month} 기록 없음"
        else:
            try:
                cost = f"{month} ${float(data.get('spent', 0.0)):.2f}/${S.NOVEL_BUDGET_USD:.0f}"
            except (TypeError, ValueError):
                cost = "미확인(spent 형식)"
    if runs is None:
        run_line = "미확인(DB-11 조회 실패)"
    else:
        per: dict[str, Counter] = {}
        for r in runs:
            per.setdefault(notion.select(r, S.R_PIPELINE) or "-", Counter())[notion.select(r, S.R_STATUS) or "-"] += 1
        run_line = " · ".join(f"{name} {_counts(c)}" for name, c in sorted(per.items())) or "없음"
    return (f"소설 요약 {today.isoformat()} (정보): 상태 {_counts(states)} / 오늘 {day_line(today)} / "
            f"내일 {day_line(tomorrow)} / 비용 {cost} / 실행24h {run_line}")


def novel_checks(now: datetime) -> tuple[list[Check], str]:
    """(H-N1·H-N2·H-N4 결과, 일일 요약). DB12_ID 없으면 생략."""
    if not S.DB12_ID:
        logger.info("[Health] 사건 소설 감시 생략 — PAGE76_DB12_ID 미설정")
        return [Check("H-N", SKIP, "db12_not_configured")], ""
    now_kst = now.astimezone(notion.KST)
    try:
        rows = novel_store.all_rows()
    except notion.NotionError as e:
        return [Check("H-N1", WARN, f"db12:{e}")], ""
    checks = [hn1_bible(rows), hn2_tomorrow(rows, now_kst)]
    runs: list[dict] | None
    try:
        runs = recent_runs(now)
    except notion.NotionError as e:
        runs = None
        checks.append(Check("H-N4", WARN, f"db11:{e}"))
    if runs is not None:
        checks.append(hn4_stalled(rows, runs, now_kst.date()))
    return checks, novel_summary(rows, runs, now_kst)


def body(log: RunLog, today: date | None = None, now: datetime | None = None) -> None:
    now = now or datetime.now(notion.KST)
    day0 = today or now.astimezone(notion.KST).date()
    logger.info(f"[Health] v{VERSION} 시작 — 소설 감시 {'on' if S.DB12_ID else 'off(DB12 미설정)'} (DN2026_0119)")
    checks: list[Check] = [h1_page()]
    token_data: dict | None = None
    if checks[0].level == OK:
        h2, token_data = h2_token_data()
        checks += h2
    latest_feed = ""
    try:
        last = ledger.last_published()
        latest_feed = last.feed_id if last else ""
    except notion.NotionError:
        pass
    checks.append(h3_insights(latest_feed) if checks[0].level == OK else Check("H3", SKIP, "page_unreachable"))
    checks.append(Check("H4", SKIP, "moderation_not_enabled"))
    checks += h5_schema()
    checks += h6_h7(day0)
    checks.append(hn5_token(token_data, now) if checks[0].level == OK else Check("H-N5", SKIP, "page_unreachable"))
    novel, summary = novel_checks(now)
    checks += novel

    for c in checks:
        logger.info(f"[Health] {c.hid} {c.level} {c.text}".rstrip())
    problems = [c for c in checks if c.level in (WARN, FAIL)]
    for c in problems:
        (log.fail if c.level == FAIL else log.degrade)(f"{c.hid}:{c.code}")
    if problems:
        lines = " / ".join(f"{c.hid} {c.level} {c.text}" for c in problems)
        token = any(c.hid in TOKEN_HINT_HIDS and c.level == FAIL for c in problems)
        urgent = any(c.hid in URGENT_HIDS and c.level == FAIL for c in problems)
        hint = " — 토큰 재발급 시 fb-page-publisher FBDET_PAGE_TOKEN 과 함께 교체" if token else ""
        if any(c.hid in ("H-N1", "H-N2") and c.level == WARN for c in problems):
            hint += " · H-N1/H-N2 는 정보 — 다음 생성 실행에서 자동 재평가"
        send(f"health {day0.isoformat()}: {lines}{hint}", urgent=urgent)
    if summary:
        logger.info(f"[Health] {summary}")
        send(summary)


def main() -> int:
    cli.setup_logging()
    return cli.execute("health", VERSION, body, require_enabled=False)


if __name__ == "__main__":
    sys.exit(main())

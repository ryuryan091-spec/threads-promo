"""
p76/run_moderate.py
===================
댓글 자동 숨김 (DN2026_0119 A3, DSN-P76-NOVEL-003 §7). 답글·삭제·차단·DM 은 하지 않는다(DN2026_0068).

  0 가드   PAGE76_ENABLED · PAGE76_MODERATE_ENABLED(토큰 권한 확인 전까지 false)
  1 대상   DB-12 '게시완료' 행 · DB-09 휴재콘텐츠 '발행완료' 행 중 게시 후 MODERATE_LOOKBACK_HOURS(72) 이내 FB ID
           조회: 게시일시(DB-12)·발행일시(DB-09) ≥ now−MODERATE_LOOKBACK_HOURS 의 KST 날짜 필터 + 최근순 정렬
           (리뷰 W7 — 정확한 72시간 경계는 코드가 다시 거른다). DB-12 CONTROL 행은 대상에서 뺀다
  2 판정   금칙어 Secret(PAGE76_BANNED_TERMS) 포함 → 숨김. 이미 숨김이면 건너뜀
           ※ 회사명·실명 패턴은 범위 밖(리뷰 W7 결정) — 생성 측 꼬리말 문구가 '숨김 처리될 수 있습니다'로 바뀜
  3 숨김   live 에서만 POST /{comment-id} is_hidden=true. 회로 차단(권한 10·200대 등)이면 즉시 멈춘다

  모드     | 노션 기록    | Graph 쓰기
  dry_run | 없음         | 없음 (판정 건수만 로그)
  shadow  | 실행 기록만   | 없음
  live    | 실행 기록만   | 숨김

로그·DB-11 에는 건수만 남긴다 — 댓글 본문·작성자·ID 는 남기지 않는다(SEC RR-06).
사용: python -m p76.run_moderate
"""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta

from p76 import cli, graph, notion, safety, settings
from p76.runlog import RunLog

VERSION = "1.1.0"  # 1.1.0: 리뷰 W7 조회 날짜 필터·정렬
# 1.0.0: DN2026_0119 신규

logger = logging.getLogger(__name__)
S = settings


def matches(message: str) -> bool:
    """숨김 판정 — 금칙어 포함. 판정 근거(단어)는 돌려주지 않는다."""
    text = message or ""
    return any(t and t in text for t in S.BANNED_TERMS)


def _recent(at: datetime | None, now: datetime) -> bool:
    return at is not None and timedelta(0) <= now - at <= timedelta(hours=S.MODERATE_LOOKBACK_HOURS)


def _since_filter(prop: str, now: datetime) -> dict:
    """노션 날짜 필터는 KST 날짜 단위로 넉넉히(on_or_after) — 72시간 정확한 경계는 _recent 가 거른다."""
    floor = (now - timedelta(hours=S.MODERATE_LOOKBACK_HOURS)).astimezone(notion.KST).date()
    return {"property": prop, "date": {"on_or_after": floor.isoformat()}}


def targets(now: datetime) -> list[str]:
    ids: list[str] = []
    if S.DB12_ID:
        body12 = {"filter": {"and": [{"property": S.N_STATE, "select": {"equals": S.NS_PUBLISHED}},
                                     _since_filter(S.N_PUBLISHED_AT, now)]},
                  "sorts": [{"property": S.N_PUBLISHED_AT, "direction": "descending"}], "page_size": 100}
        for p in notion.query(S.DB12_ID, body12, max_pages=3):
            if notion.select(p, S.N_KIND) not in (S.NK_EPISODE, S.NK_NOTICE):
                continue
            fb_id = notion.plain(p, S.N_FB_ID)
            if fb_id and _recent(notion.date_value(p, S.N_PUBLISHED_AT), now):
                ids.append(fb_id)
    if S.DB09_ID:
        body = {"filter": {"and": [
            {"property": S.C_KIND, "select": {"equals": S.KIND_HIATUS_CONTENT}},
            {"property": S.C_STATE, "select": {"equals": S.S_DONE}},
            _since_filter(S.C_PUBLISHED_AT, now)]},
            "sorts": [{"property": S.C_PUBLISHED_AT, "direction": "descending"}], "page_size": 100}
        for p in notion.query(S.DB09_ID, body, max_pages=1):
            fb_id = notion.plain(p, S.C_FEED_ID)
            if fb_id and _recent(notion.date_value(p, S.C_PUBLISHED_AT), now):
                ids.append(fb_id)
    return list(dict.fromkeys(ids))


def body(log: RunLog, now: datetime | None = None) -> None:
    if not S.MODERATE_ENABLED:
        logger.info("[Moderate] PAGE76_MODERATE_ENABLED=false — 종료")
        return
    if not S.BANNED_TERMS:
        log.degrade("moderate:no_terms")
        logger.warning("[Moderate] 금칙어가 비어 있음 — 판정할 수 없어 종료")
        return
    now = now or datetime.now(UTC)
    live = S.MODE == "live"
    posts = targets(now)
    counts = {"comments": 0, "already": 0, "matched": 0, "hidden": 0, "failed": 0, "read_failed": 0}
    for post_id in posts:
        ok, comments, _ = graph.list_comments(post_id)
        if not ok:
            counts["read_failed"] += 1
        for c in comments:
            counts["comments"] += 1
            if c.get("is_hidden") is True:
                counts["already"] += 1
                continue
            if not matches(str(c.get("message") or "")):
                continue
            counts["matched"] += 1
            if not live:
                continue
            if not safety.graph_write_allowed():
                break
            res = graph.hide_comment(str(c["id"]))
            if res["status"] == "ok":
                counts["hidden"] += 1
                log.writes += 1
            else:
                counts["failed"] += 1
        if safety.tripped():
            break
    logger.info(f"[Moderate] mode={S.MODE} posts={len(posts)} " + " ".join(f"{k}={v}" for k, v in counts.items()))
    if counts["failed"]:
        log.degrade(f"hide_failed:{counts['failed']}")
    if counts["read_failed"]:
        log.degrade(f"comments_read_failed:{counts['read_failed']}")
    if safety.tripped():
        log.degrade("graph:tripped")


def main() -> int:
    cli.setup_logging()
    return cli.execute("moderate", VERSION, body)


if __name__ == "__main__":
    sys.exit(main())

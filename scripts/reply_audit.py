"""답글 감사 — 읽기 전용.

실행
  Actions → Threads Reply Audit → Run workflow (days 기본 3, 최대 14)
  로컬: python scripts/reply_audit.py [--days 3]

원칙
  - Threads 에 아무것도 쓰지 않는다. GET(/me, 내 글 목록, 대화)만 한다. Claude 도 호출하지 않는다.
  - 토큰은 Secret 값을 그대로 쓴다. 갱신·저장하지 않는다(golive_check 와 같은 방식).
  - 타인 계정명은 앞 2글자만 남기고, 본문 속 @언급도 가린다(redact.mask_username / mask_mentions).
    자격증명 패턴도 redact() 로 한 번 더 가린다.

출력
  최근 N일 내 글마다, 내가 댓글에 단 답글을 그 댓글과 함께 한 행으로 보여준다.
    원글 시각·앞부분 / 댓글 작성자(가림) / 댓글 원문 / 내 답글 / 현재 로직 재판정
  재판정은 지금의 reply_engine.decide 로 그 댓글을 다시 판정한 결과(방침·사유)와 답글 형식 범주다.
  일일·저자·스레드 캡과 '이미 답글함'은 재판정에서 제외한다(이미 답했으므로 항상 걸린다).
  링크 셀프 리플라이·셀프 이어쓰기(원글에 단 내 글)는 댓글 응답이 아니므로 제외한다.
  결과는 표준출력과 $GITHUB_STEP_SUMMARY 에 Markdown 표로 남긴다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys
from collections import Counter
from dataclasses import dataclass
from zoneinfo import ZoneInfo

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import config, reply_engine, style, watchdog  # noqa: E402
from src.redact import mask_mentions, mask_username, redact  # noqa: E402
from src.reply_engine import Comment, ReplyStrategy  # noqa: E402
from src.threads_client import (  # noqa: E402
    POSTS_MAX_PAGES,
    POSTS_PAGE_SIZE,
    ThreadsApiError,
    ThreadsClient,
    fetch_user_id,
)

VERSION = "1.0.0"
KST = ZoneInfo("Asia/Seoul")

DEFAULT_DAYS = 3
MAX_DAYS = 14
POSTS_PER_DAY = 20          # 하루 게시물(정기+CHAT+이벤트) 약 10건의 2배 여유
POST_HEAD_CHARS = 40
TEXT_CHARS = 150


def clamp_days(raw: object) -> int:
    """입력 일수를 1~MAX_DAYS 로 맞춘다. 해석 불가면 기본값."""
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(days, MAX_DAYS))


def _parse_comment(raw: dict) -> Comment:
    """run_reply._parse_comment 와 같은 변환. run_reply(발행 경로) import 를 피한다."""
    replied_to = raw.get("replied_to") or {}
    return Comment(
        id=str(raw.get("id", "")),
        text=(raw.get("text") or "").strip(),
        username=str(raw.get("username", "")),
        timestamp=str(raw.get("timestamp", "")),
        replied_to_id=str(replied_to.get("id", "")),
        owned_by_me=bool(raw.get("is_reply_owned_by_me", False)),
        hide_status=str(raw.get("hide_status", "")),
    )


@dataclass(frozen=True)
class AuditRow:
    post_time: str
    post_head: str
    author: str
    comment: str
    reply: str
    strategy: str
    reason: str
    kind: str


def _kst_label(timestamp: str) -> str:
    parsed = watchdog.parse_threads_timestamp(timestamp)
    return parsed.astimezone(KST).strftime("%m-%d %H:%M") if parsed else "-"


def _clean(text: str, limit: int) -> str:
    """표 셀용: 자격증명·@언급 가림, 줄바꿈·파이프 정리, 길이 제한."""
    out = mask_mentions(redact(text or ""))
    out = " ".join(out.split()).replace("|", "/")
    return out if len(out) <= limit else out[: limit - 1] + "…"


def audit_rows(posts: list[dict], conversations: dict[str, list[Comment]]) -> list[AuditRow]:
    """내 답글(댓글에 단 것) 한 건당 한 행. 원글 순서 → 대화 순서."""
    my_post_ids = {str(p.get("id", "")) for p in posts if p.get("id")}
    rows: list[AuditRow] = []
    for post in posts:
        post_id = str(post.get("id", ""))
        comments = conversations.get(post_id)
        if not comments:
            continue
        by_id = {c.id: c for c in comments}
        reply_targets = my_post_ids | {c.id for c in comments if c.owned_by_me}
        for mine in comments:
            if not mine.owned_by_me or not mine.replied_to_id:
                continue
            if mine.replied_to_id in my_post_ids:
                continue   # 링크 셀프 리플라이·셀프 이어쓰기
            target = by_id.get(mine.replied_to_id)
            if target is None:
                rows.append(AuditRow(
                    _kst_label(str(post.get("timestamp", ""))),
                    _clean(post.get("text") or "", POST_HEAD_CHARS),
                    "-", "(대화 조회 범위 밖)", _clean(mine.text, TEXT_CHARS), "-", "-", "-",
                ))
                continue
            decision = reply_engine.decide(
                target, already_replied=False, author_used=0, thread_author_count=0,
                reply_target_ids=reply_targets,
            )
            if decision.strategy in (ReplyStrategy.SKIP, ReplyStrategy.NON_KOREAN):
                kind = "-"
            else:
                kind = style.pick_reply_style(
                    target.id, target.text,
                    reaction=decision.strategy is ReplyStrategy.REACTION,
                ).kind
            rows.append(AuditRow(
                post_time=_kst_label(str(post.get("timestamp", ""))),
                post_head=_clean(post.get("text") or "", POST_HEAD_CHARS),
                author=mask_username(target.username),
                comment=_clean(target.text, TEXT_CHARS),
                reply=_clean(mine.text, TEXT_CHARS),
                strategy=decision.strategy.value,
                reason=_clean(decision.reason, 60),
                kind=kind,
            ))
    return rows


def render_markdown(rows: list[AuditRow], days: int, post_count: int) -> str:
    lines = [
        f"## Reply Audit (최근 {days}일, 원글 {post_count}건, 내 답글 {len(rows)}건)",
        "",
        "재판정 = 현재 reply_engine.decide 결과(캡·'이미 답글함' 제외) · 형식 = style 범주",
        "",
        "| 원글 시각(KST) | 원글 앞부분 | 작성자 | 댓글 원문 | 내 답글 | 재판정 | 사유 | 형식 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.post_time} | {r.post_head} | {r.author} | {r.comment} | {r.reply} "
            f"| {r.strategy} | {r.reason} | {r.kind} |"
        )
    if not rows:
        lines.append("| - | - | - | 해당 기간 내 답글 없음 | - | - | - | - |")
    counts = Counter(r.strategy for r in rows)
    lines.append("")
    lines.append("재판정 분포: " + (" · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                                   or "없음"))
    return "\n".join(lines)


def _within_days(post: dict, now: dt.datetime, days: int) -> bool:
    parsed = watchdog.parse_threads_timestamp(str(post.get("timestamp", "")))
    if parsed is None:
        return False
    return now - parsed <= dt.timedelta(days=days)


def collect(client: ThreadsClient, days: int, now: dt.datetime) -> tuple[list[dict], dict]:
    """GET 만 한다. (원글 목록, {원글 ID: 대화 Comment 목록})."""
    since = (now.astimezone(KST) - dt.timedelta(days=days + 1)).date()
    limit = min(days * POSTS_PER_DAY, POSTS_PAGE_SIZE * POSTS_MAX_PAGES)
    posts = [p for p in client.get_my_posts(limit, since=since) if _within_days(p, now, days)]
    conversations: dict[str, list[Comment]] = {}
    for post in posts:
        post_id = str(post.get("id", ""))
        if not post_id:
            continue
        try:
            raw = client.get_conversation(post_id, config.REPLY_SCAN_LIMIT)
        except ThreadsApiError as exc:
            if exc.is_blocked:
                raise
            print(f"[WARN] 대화 조회 실패 post={post_id}: {redact(str(exc))[:160]}")
            continue
        conversations[post_id] = [_parse_comment(r) for r in raw]
    return posts, conversations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="답글 감사 (읽기 전용)")
    parser.add_argument("--days", default=os.environ.get("AUDIT_DAYS", str(DEFAULT_DAYS)),
                        help=f"최근 며칠(1~{MAX_DAYS}, 기본 {DEFAULT_DAYS})")
    args = parser.parse_args(argv)
    days = clamp_days(args.days)

    print(f"[ReplyAudit] v{VERSION} 시작 — 최근 {days}일")
    token = os.environ.get("THREADS_LONG_LIVED_TOKEN", "").strip()
    if not token:
        print("THREADS_LONG_LIVED_TOKEN 이 없습니다.")
        return 2

    try:
        user_id = os.environ.get("THREADS_USER_ID", "").strip()
        if not user_id.isdigit():
            user_id, _ = fetch_user_id(token)
        client = ThreadsClient(user_id, token)
        now = dt.datetime.now(dt.UTC)
        posts, conversations = collect(client, days, now)
    except ThreadsApiError as exc:
        print(f"Threads API 오류: {redact(str(exc))[:300]}")
        return 4

    summary = render_markdown(audit_rows(posts, conversations), days, len(posts))
    print("\n" + summary)
    path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if path:
        with pathlib.Path(path).open("a", encoding="utf-8") as fh:
            fh.write(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

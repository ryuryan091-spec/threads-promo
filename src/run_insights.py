"""인사이트 수집 엔트리포인트.

실행: python -m src.run_insights

Threads 에 아무것도 쓰지 않는다. 모든 요청이 GET 이다.
발행·답글과 완전히 독립되어 있어, 인사이트 실패가 발행을 막지 않는다.

수집 결과는 저장하지 않는다. 인사이트는 언제든 재조회 가능한 데이터이고,
관찰 기간이 2주~2개월 규모라 텔레그램 리포트를 모아 보는 것으로 충분하다.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from zoneinfo import ZoneInfo

from . import config, insights, notifier
from .env import MissingEnvError, load_settings
from .threads_client import ThreadsApiError, ThreadsClient, fetch_user_id

VERSION = "1.1.1"
KST = ZoneInfo("Asia/Seoul")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-insights")

for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _user_metrics(followers_known: int | None) -> tuple[str, ...]:
    """요청할 사용자 메트릭.

    follower_demographics 는 팔로워 100명 이상이어야 조회된다.
    미달 상태에서 요청하면 오류이거나 빈 값이므로 아예 넣지 않는다.
    """
    base = list(insights.USER_METRICS)
    if (
        followers_known is not None
        and followers_known >= config.INSIGHTS_DEMOGRAPHICS_MIN_FOLLOWERS
    ):
        base.append("follower_demographics")
    return tuple(base)


def collect_post_stats(
    client: ThreadsClient, posts: list[dict], limit: int
) -> tuple[list[insights.PostStat], int]:
    """게시물별 인사이트를 모은다. 실패 건수도 함께 돌려준다."""
    stats: list[insights.PostStat] = []
    failed = 0

    for post in posts[:limit]:
        post_id = str(post.get("id", ""))
        posted_at = insights.parse_timestamp(str(post.get("timestamp", "")))
        if not post_id or posted_at is None:
            failed += 1
            continue

        try:
            body = client.get_media_insights(post_id, insights.MEDIA_METRICS)
        except ThreadsApiError as exc:
            if exc.is_blocked:
                raise
            log.warning("게시물 인사이트 실패 %s: %s", post_id, exc)
            failed += 1
            continue

        metrics = insights.parse_media_insights(body)
        stats.append(
            insights.PostStat(
                post_id=post_id,
                posted_at=posted_at,
                pillar=insights.restore_pillar(posted_at, str(post.get("media_type") or "")),
                views=metrics.get("views", 0),
                likes=metrics.get("likes", 0),
                replies=metrics.get("replies", 0),
                reposts=metrics.get("reposts", 0),
                quotes=metrics.get("quotes", 0),
            )
        )

    return stats, failed


def run() -> int:
    log.info("[Insights] v%s 시작", VERSION)

    if not config.INSIGHTS_ENABLED:
        log.info("INSIGHTS_ENABLED=false — 종료")
        return 0

    settings = load_settings()
    today = dt.datetime.now(KST).date()

    # 인사이트는 토큰을 갱신하지 않는다. 읽기만 한다.
    token = settings.threads_token
    user_id = settings.threads_user_id
    if user_id == "me":
        user_id, username = fetch_user_id(token)
        log.info("사용자 ID 조회 완료 — @%s", username)

    client = ThreadsClient(user_id, token)

    # 1) 사용자 인사이트 — 먼저 팔로워 수를 알아야 demographics 포함 여부가 정해진다
    user_body = client.get_user_insights(_user_metrics(None))
    user = insights.parse_user_insights(user_body)
    log.info(
        "사용자 인사이트 — 팔로워 %d, 프로필 조회 %d, 링크 %d종",
        user.followers, user.profile_views, len(user.clicks),
    )

    # 2) 최근 게시물
    since = today - dt.timedelta(days=config.INSIGHTS_LOOKBACK_DAYS)
    posts = client.get_my_posts(config.INSIGHTS_POST_LIMIT, since=since)
    stats, failed = collect_post_stats(client, posts, config.INSIGHTS_POST_LIMIT)
    log.info("게시물 인사이트 %d건 수집 (실패 %d건)", len(stats), failed)

    unknown = sum(1 for s in stats if s.pillar == insights.UNKNOWN)
    if unknown:
        log.warning("기둥 판정 불가 %d건 — 수동 실행이거나 슬롯 변경 이전 글", unknown)

    # 3) 집계 및 리포트
    rows = insights.aggregate(stats)
    report = insights.render_report(
        today, user, rows, config.INSIGHTS_LOOKBACK_DAYS, failed_posts=failed
    )

    log.info("\n%s", report)
    _write_summary(report)

    notifier.send(settings.telegram_bot_token, settings.telegram_chat_id, report)
    return 0


def _write_summary(report: str) -> None:
    """GitHub Actions 요약란에 출력한다. 실패해도 무시한다."""
    import os
    import pathlib

    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        with pathlib.Path(path).open("a", encoding="utf-8") as fh:
            fh.write(f"```\n{report}\n```\n")
    except OSError as exc:
        log.warning("Actions 요약 기록 실패: %s", exc)


def main() -> int:
    try:
        return run()
    except MissingEnvError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except ThreadsApiError as exc:
        if exc.is_blocked:
            log.error("API 접근 차단 (code=200)\n%s", exc)
            _alert(
                "[Threads][최우선] 인사이트 조회 중 API 접근 차단 (code=200)\n"
                "developers.facebook.com 에서 계정·앱 상태를 확인하십시오.\n"
                f"{exc}"
            )
            return 7
        if exc.code == 200 or "insights" in str(exc).lower():
            log.error("인사이트 권한이 없을 수 있습니다: %s", exc)
            _alert(
                "[Threads] 인사이트 조회 실패 — threads_manage_insights 권한을 "
                f"확인하십시오.\n{exc}"
            )
            return 4
        log.error("Threads API 오류: %s", exc)
        _alert(f"[Threads] 인사이트 조회 실패\n{exc}")
        return 4
    except Exception as exc:  # noqa: BLE001
        log.exception("예기치 못한 오류")
        _alert(f"[Threads] 인사이트 워크플로우 오류\n{exc}")
        return 1


def _alert(message: str) -> None:
    try:
        settings = load_settings()
        notifier.send(settings.telegram_bot_token, settings.telegram_chat_id, message)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())

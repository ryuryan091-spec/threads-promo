"""기둥 비중 자동 조절 엔트리포인트.

실행: python -m src.run_weighting

기본 비활성이다. 활성화해도 게이트를 전부 통과해야 실제로 바뀐다.
조정하지 않은 경우에도 사유와 함께 리포트를 보낸다.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from zoneinfo import ZoneInfo

from . import chat_plan, config, insights, notifier, weighting
from .env import MissingEnvError, load_settings
from .run_insights import collect_post_stats
from .threads_client import ThreadsApiError, ThreadsClient, fetch_user_id

VERSION = "1.1.1"
KST = ZoneInfo("Asia/Seoul")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-weighting")

for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


WEIGHT_POSTS_LIST_LIMIT = 300   # 25 x 12페이지
WEIGHT_POSTS_STATS_LIMIT = 60


def _is_chat_post(post: dict) -> bool:
    return chat_plan.is_chat_post_dict(post, insights.parse_timestamp)


def _scores(
    stats: list[insights.PostStat], clicks_total: int
) -> list[weighting.PillarScore]:
    """기둥별 점수를 만든다.

    클릭은 사용자 단위 집계라 게시물별로 쪼갤 수 없다.
    발행 비중에 비례해 배분한다. 근사치이며, 이 한계 때문에
    최소 표본(S1)과 유의 임계(S6)를 두었다.
    """
    from collections import defaultdict

    posts: dict[str, int] = defaultdict(int)
    replies: dict[str, int] = defaultdict(int)
    for stat in stats:
        # CHAT 은 로테이션 기둥이 아니고 링크도 없다. 클릭 배분에서 제외한다.
        if stat.pillar in (insights.UNKNOWN, insights.CHAT):
            continue
        posts[stat.pillar] += 1
        replies[stat.pillar] += stat.replies

    total_posts = sum(posts.values())
    out: list[weighting.PillarScore] = []
    for pillar, count in posts.items():
        share = count / total_posts if total_posts else 0.0
        out.append(
            weighting.PillarScore(
                pillar=pillar,
                posts=count,
                clicks=round(clicks_total * share),
                replies=replies[pillar],
            )
        )
    return out


def run() -> int:
    log.info("[Weighting] v%s 시작", VERSION)

    settings = load_settings()
    today = dt.datetime.now(KST).date()
    window = config.WEIGHT_ADJUST_INTERVAL_DAYS

    token = settings.threads_token
    user_id = settings.threads_user_id
    if user_id == "me":
        user_id, username = fetch_user_id(token)
        log.info("사용자 ID 조회 완료 — @%s", username)

    client = ThreadsClient(user_id, token)

    since = int(
        dt.datetime.combine(
            today - dt.timedelta(days=window), dt.time(0, 0), tzinfo=KST
        ).timestamp()
    )
    user_body = client.get_user_insights(("clicks", "replies"), since=since)
    user = insights.parse_user_insights(user_body)
    clicks_total = sum(user.clicks.values())
    log.info("%d일 클릭 합계 %d", window, clicks_total)

    # CHAT 도입 후 하루 게시물이 약 10건이다. 목록은 넉넉히 받고,
    # 인사이트 호출은 CHAT 을 뺀 정기·이벤트 글에만 한다(호출 수 절약).
    listed = client.get_my_posts(
        WEIGHT_POSTS_LIST_LIMIT, since=today - dt.timedelta(days=window)
    )
    posts = [p for p in listed if not _is_chat_post(p)]
    stats, failed = collect_post_stats(client, posts, WEIGHT_POSTS_STATS_LIMIT)
    log.info("게시물 %d건 수집 (실패 %d건)", len(stats), failed)

    result = weighting.decide(_scores(stats, clicks_total), today, config.LAST_WEIGHT_ADJUST)
    report = weighting.render(result, today, window)
    log.info("\n%s", report)

    if result.adjusted:
        log.warning(
            "로테이션 변경 필요 — Variables 에 아래 값을 반영하십시오.\n"
            "  PILLAR_ROTATION_OVERRIDE = %s\n"
            "  LAST_WEIGHT_ADJUST = %s",
            ",".join(result.after), today.isoformat(),
        )
        report += (
            "\n\nVariables 반영 필요\n"
            f"  PILLAR_ROTATION_OVERRIDE = {','.join(result.after)}\n"
            f"  LAST_WEIGHT_ADJUST = {today.isoformat()}"
        )

    notifier.send(settings.telegram_bot_token, settings.telegram_chat_id, report)
    return 0


def main() -> int:
    try:
        return run()
    except MissingEnvError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except ThreadsApiError as exc:
        if exc.is_blocked:
            log.error("API 접근 차단 (code=200)\n%s", exc)
            return 7
        log.error("Threads API 오류: %s", exc)
        return 4
    except Exception:  # noqa: BLE001
        log.exception("예기치 못한 오류")
        return 1


if __name__ == "__main__":
    sys.exit(main())

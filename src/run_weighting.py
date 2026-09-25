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

VERSION = "1.2.0"   # v1.2.0: 클릭 기둥 배분 제거, 자동 결과는 PILLAR_ROTATION_AUTO
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

    v1.2.0: 클릭은 사용자 단위 합계라 기둥으로 쪼갤 수 없다. 이전에는 발행 비중에
    비례해 배분했으나, 그러면 기둥별 클릭 평균이 모두 clicks_total / 전체 발행 수로
    같아져 판별력이 0이다. 기둥 점수에는 넣지 않고(clicks=0) 리포트에 합계만 표시한다.
    clicks_total 인자는 호출부 호환을 위해 유지한다.
    """
    del clicks_total
    from collections import defaultdict

    posts: dict[str, int] = defaultdict(int)
    replies: dict[str, int] = defaultdict(int)
    for stat in stats:
        # CHAT 은 로테이션 기둥이 아니고 링크도 없다. 클릭 배분에서 제외한다.
        if stat.pillar in (insights.UNKNOWN, insights.CHAT):
            continue
        posts[stat.pillar] += 1
        replies[stat.pillar] += stat.replies

    return [
        weighting.PillarScore(
            pillar=pillar, posts=count, clicks=0, replies=replies[pillar]
        )
        for pillar, count in posts.items()
    ]


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
    report += (
        f"\n\n계정 링크 클릭 합계({window}일): {clicks_total}"
        "\n  클릭은 기둥별 귀속이 불가능해 점수에서 제외합니다(답글 기준 판정)."
    )
    log.info("\n%s", report)

    if result.adjusted:
        log.warning(
            "로테이션 변경 필요 — Variables 에 아래 값을 반영하십시오.\n"
            "  PILLAR_ROTATION_AUTO = %s\n"
            "  LAST_WEIGHT_ADJUST = %s",
            ",".join(result.after), today.isoformat(),
        )
        report += (
            "\n\nVariables 반영 필요\n"
            f"  PILLAR_ROTATION_AUTO = {','.join(result.after)}\n"
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

"""워치독 엔트리포인트.

실행: python -m src.run_watchdog

정상이면 아무 알림도 보내지 않는다. 로그에만 요약을 남긴다.
워치독 자신의 실패로 파이프라인을 흔들지 않기 위해, 어떤 오류가 나도
종료코드 0 을 유지한다. 다만 오류 자체는 알린다.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from zoneinfo import ZoneInfo

from . import config, notifier, watchdog
from .env import load_settings
from .threads_client import ThreadsApiError, ThreadsClient, fetch_user_id

VERSION = "1.0.0"
KST = ZoneInfo("Asia/Seoul")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-watchdog")

for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _collect_owned_reply_stamps(
    client: ThreadsClient, posts: list[dict]
) -> list[dt.datetime]:
    """최근 글들의 대화에서 내 답글 시각을 모은다."""
    stamps: list[dt.datetime] = []
    for post in posts[: watchdog.RECENT_POSTS_TO_SCAN]:
        post_id = str(post.get("id", ""))
        if not post_id:
            continue
        try:
            items = client.get_conversation(post_id, 25)
        except ThreadsApiError as exc:
            log.warning("대화 조회 실패 post=%s: %s", post_id, exc)
            continue
        for item in items:
            if not item.get("is_reply_owned_by_me"):
                continue
            parsed = watchdog.parse_threads_timestamp(str(item.get("timestamp", "")))
            if parsed:
                stamps.append(parsed)
    return stamps


def run() -> int:
    log.info("[Watchdog] v%s 시작", VERSION)

    settings = load_settings()
    now = dt.datetime.now(dt.UTC)
    today = dt.datetime.now(KST).date()

    findings: list[watchdog.Finding] = []

    # 토큰 만료는 API 없이도 판정 가능하므로 먼저 본다.
    findings.append(watchdog.check_token_expiry(today, config.TOKEN_ISSUED_AT))

    # 워치독은 토큰을 갱신하지 않는다. 발행 워크플로우와 중복되면
    # 갱신이 이중으로 일어나 오히려 혼란스럽다. 현재 토큰 그대로 읽기만 한다.
    token = settings.threads_token

    try:
        user_id = settings.threads_user_id
        if user_id == "me":
            user_id, username = fetch_user_id(token)
            log.info("사용자 ID 조회 완료 — @%s", username)

        client = ThreadsClient(user_id, token)

        posts = client.get_my_posts(watchdog.RECENT_POSTS_TO_SCAN)
        findings.append(watchdog.check_publish_freshness(posts, now))

        quota = client.get_post_quota()
        findings.append(watchdog.check_quota(quota.used, quota.total))

        reply_stamps = _collect_owned_reply_stamps(client, posts)
        findings.append(
            watchdog.check_reply_activity(
                reply_stamps, now, enabled=config.REPLY_ENABLED
            )
        )

    except ThreadsApiError as exc:
        # API 자체가 안 되는 것도 감시 대상이다.
        severity = (
            watchdog.Severity.CRITICAL if exc.is_auth_error else watchdog.Severity.WARN
        )
        findings.append(
            watchdog.Finding(
                severity,
                "Threads API 접근 실패",
                f"{exc}\n토큰 무효 또는 API 장애 가능성이 있습니다.",
            )
        )

    report = watchdog.build_report(findings)

    for finding in report.findings:
        line = f"[{finding.severity}] {finding.title} — {finding.detail}"
        if finding.severity == watchdog.Severity.CRITICAL:
            log.error(line)
        elif finding.severity == watchdog.Severity.WARN:
            log.warning(line)
        else:
            log.info(line)

    if report.has_alert:
        notifier.send(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            report.to_message(),
        )
        log.warning("경보 발송 — 이상 %d건", sum(
            1 for f in report.findings if f.severity != watchdog.Severity.OK
        ))
    else:
        log.info("이상 없음 — 알림을 보내지 않습니다.")

    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 — 워치독은 죽어도 조용히
        log.exception("워치독 실행 오류")
        try:
            settings = load_settings()
            notifier.send(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                f"[Threads Watchdog] 워치독 자체 오류\n{exc}",
            )
        except Exception:  # noqa: BLE001
            pass
        # 워치독 실패로 Actions 를 빨갛게 만들지 않는다.
        return 0


if __name__ == "__main__":
    sys.exit(main())

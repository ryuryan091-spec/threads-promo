"""토큰 주간 갱신 전용 엔트리포인트.

실행: python -m src.run_refresh

발행·답글 실행에서 갱신을 분리한 이유
  60일짜리 토큰을 하루 수십 번 갱신하는 것은 Meta 문서의 갱신 조건
  (발급 후 24시간 경과 ~ 만료 전)에 어긋난다. 정상 앱은 만료 임박 시
  한 번 갱신한다. 매 실행 갱신은 자동 보안 시스템에 이상 패턴으로 보이며,
  실제로 개발자 계정 checkpoint 가 걸린 이력이 있다.

만료 안전성
  갱신 가능 구간이 24시간~60일이므로 주 1회로 충분하다.
  한 번 실패해도 다음 주 재시도 여유가 있다.
  발행 로그에 묻히지 않고 실패가 명확히 드러나는 이점도 있다.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from zoneinfo import ZoneInfo

from . import config, notifier, token_manager
from .env import MissingEnvError, load_settings
from .main import _alert_expiry, refresh_and_persist
from .threads_client import ThreadsApiError, fetch_user_id

VERSION = "1.0.0"
KST = ZoneInfo("Asia/Seoul")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-refresh")

for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _should_refresh(
    assessment: token_manager.ExpiryAssessment, forced: bool
) -> bool:
    """갱신이 필요한 시점인지 판정한다.

    잔여 일수를 모르면(발급일 미기록) 갱신한다.
    모른 채 방치하면 만료를 놓치는 쪽이 더 위험하다.
    """
    if forced:
        return True
    if assessment.days_left is None:
        log.warning("잔여 일수 미상 — 안전을 위해 갱신을 진행합니다.")
        return True
    return assessment.days_left <= config.TOKEN_REFRESH_THRESHOLD_DAYS


def run() -> int:
    log.info("[TokenRefresh] v%s 시작", VERSION)

    settings = load_settings()
    today = dt.datetime.now(KST).date()

    basis = token_manager.effective_issue_date(
        config.TOKEN_ISSUED_AT, config.TOKEN_REFRESHED_AT
    )
    before = token_manager.assess_expiry(today, basis)
    log.info("갱신 전 상태 — %s", before.message)

    # 임계 게이트 — 여기까지 API 호출 0회.
    # 잔여 일수는 날짜 계산으로 나오므로 매일 확인해도 비용이 없다.
    forced = os.environ.get("FORCE_REFRESH", "").strip().lower() in (
        "true", "1", "yes"
    )
    if not _should_refresh(before, forced):
        log.info(
            "갱신 불필요 — 잔여 %s일 (임계 %d일). 종료",
            before.days_left, config.TOKEN_REFRESH_THRESHOLD_DAYS,
        )
        return 0

    if forced:
        log.warning("FORCE_REFRESH=true — 임계와 무관하게 갱신합니다.")

    new_token = refresh_and_persist(settings)

    if new_token == settings.threads_token:
        # 갱신 실패 시 기존 토큰이 반환된다. 이 경우 만료 시계가 흐른다.
        log.warning("토큰이 갱신되지 않았습니다. 다음 주기에 재시도합니다.")
        _alert_expiry(settings, "주간 갱신 실패 — 다음 주기 재시도 예정.")
        return 0

    log.info("갱신 완료 — 새 토큰으로 교체되었습니다.")

    # 새 토큰이 실제로 동작하는지 확인한다.
    # 갱신은 됐는데 쓸 수 없는 상태면 다음 발행에서야 드러난다.
    try:
        user_id, username = fetch_user_id(new_token)
        log.info("검증 완료 — @%s (id=%s)", username, user_id)
    except ThreadsApiError as exc:
        if exc.is_blocked:
            log.error("갱신 후 검증에서 접근 차단 확인 (code=200)\n%s", exc)
            notifier.send(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                "[Threads][최우선] 토큰 갱신 후 검증에서 API 접근 차단 확인\n"
                "developers.facebook.com 에서 계정·앱 상태를 확인하십시오.\n"
                f"{exc}",
            )
            return 7
        log.error("갱신 후 검증 실패: %s", exc)
        notifier.send(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            f"[Threads] 토큰 갱신은 됐으나 검증 실패\n{exc}",
        )
        return 4

    return 0


def main() -> int:
    try:
        return run()
    except MissingEnvError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        log.exception("예기치 못한 오류")
        try:
            settings = load_settings()
            notifier.send(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                f"[Threads] 토큰 갱신 워크플로우 오류\n{exc}",
            )
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

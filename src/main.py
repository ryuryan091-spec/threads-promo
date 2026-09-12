"""엔트리포인트.

흐름:
  1) 토큰 갱신 -> 새 토큰을 변수로 수령 (지시사항)
  2) 새 토큰을 Secret에 영속화 (60일 만료 방지)
  3) 발행 쿼터 확인 (DB 대신 API 조회)
  4) 날짜 기반 콘텐츠 선택 + 정책 린트
  5) 이미지 게시물 발행
  6) 셀프 리플라이로 YouTube / X 링크 배치
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, content, notifier, token_manager
from .env import MissingEnvError, Settings, load_settings
from .threads_client import ThreadsApiError, ThreadsClient

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "assets"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("threads-promo")

# 토큰 문자열이 서드파티 라이브러리 로거를 통해 새는 사고를 막는다.
for noisy in ("urllib3", "requests", "hpack", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _preflight() -> None:
    if config.YOUTUBE_URL_PLACEHOLDER in config.YOUTUBE_URL:
        raise RuntimeError(
            "config.YOUTUBE_URL이 플레이스홀더 상태입니다. "
            "실제 채널 핸들로 교체한 뒤 실행하십시오."
        )


def _resolve_raw_base_url(settings: Settings) -> str:
    """레포 raw URL. 오버라이드가 있으면 그 값을 우선한다."""
    override = os.environ.get("ASSET_RAW_BASE_URL", "").strip()
    if override:
        return override
    ref = os.environ.get("GITHUB_REF_NAME", "main").strip() or "main"
    return f"https://raw.githubusercontent.com/{settings.gh_repo}/{ref}/assets"


def _acquire_token(settings: Settings) -> str:
    """갱신 실패는 치명적이지 않다. 기존 토큰이 아직 유효할 수 있으므로 폴백한다."""
    try:
        new_token = token_manager.refresh_long_lived_token(settings.threads_token)
    except token_manager.TokenRefreshError as exc:
        msg = f"[Threads] 토큰 갱신 실패 — 기존 토큰으로 진행합니다.\n{exc}"
        log.warning(msg)
        notifier.send(settings.telegram_bot_token, settings.telegram_chat_id, msg)
        return settings.threads_token

    if not settings.can_persist_token:
        notifier.send(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            "[Threads] 경고: GH_PAT_SECRETS_WRITE 미설정으로 갱신 토큰을 "
            "영속화하지 못했습니다. 이대로면 60일 후 재인가가 필요합니다.",
        )
        return new_token

    try:
        token_manager.persist_token_to_secret(
            settings.gh_repo, settings.gh_pat, new_token
        )
    except token_manager.SecretPersistError as exc:
        notifier.send(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            f"[Threads] 토큰 Secret 영속화 실패 — 60일 만료 위험.\n{exc}",
        )
    return new_token


def run() -> int:
    _preflight()
    settings = load_settings()
    today = dt.datetime.now(KST).date()

    token = _acquire_token(settings)
    client = ThreadsClient(settings.threads_user_id, token)

    quota = client.get_post_quota()
    log.info("발행 쿼터 %d/%d (잔여 %d)", quota.used, quota.total, quota.remaining)
    if quota.remaining < 2:  # 본문 1 + 셀프 리플라이 1
        raise RuntimeError(f"쿼터 부족 — 잔여 {quota.remaining}")

    plan = content.build_plan(today, ASSETS_DIR, _resolve_raw_base_url(settings))
    log.info("유형=%s 이미지=%s", plan.kind.value, plan.image_url)

    if settings.dry_run:
        log.info("DRY_RUN — 실제 발행하지 않습니다.\n--- 본문 ---\n%s\n--- 리플 ---\n%s",
                 plan.text, plan.reply_text)
        return 0

    post_id = client.publish_image_post(plan.image_url, plan.text)
    log.info("본문 발행 완료 post_id=%s", post_id)

    reply_id = client.publish_self_reply(post_id, plan.reply_text)
    log.info("셀프 리플라이 발행 완료 reply_id=%s", reply_id)
    return 0


def main() -> int:
    try:
        return run()
    except MissingEnvError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except content.ContentPolicyError as exc:
        log.error("콘텐츠 정책 위반으로 발행 중단: %s", exc)
        _notify_safe(f"[Threads] 콘텐츠 정책 위반 — 발행 중단\n{exc}")
        return 3
    except ThreadsApiError as exc:
        hint = " (재인가 필요: authorize -> 단기 -> 장수명)" if exc.is_auth_error else ""
        log.error("Threads API 오류%s: %s", hint, exc)
        _notify_safe(f"[Threads] 발행 실패{hint}\n{exc}")
        return 4
    except Exception as exc:  # noqa: BLE001 — 최상위 방어
        log.exception("예기치 못한 오류")
        _notify_safe(f"[Threads] 예기치 못한 오류\n{exc}")
        return 1


def _notify_safe(message: str) -> None:
    try:
        settings = load_settings()
        notifier.send(settings.telegram_bot_token, settings.telegram_chat_id, message)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())

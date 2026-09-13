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
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import antibot, config, content, facts, notifier, token_manager
from .env import MissingEnvError, Settings, load_settings
from .threads_client import (
    ContainerNotReadyError,
    ImageValidationError,
    ThreadsApiError,
    ThreadsClient,
    fetch_user_id,
    verify_image_url,
)

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
    """링크 미설정 상태로 발행되는 것을 막는다."""
    missing = [
        name
        for name, value in (("YOUTUBE_URL", config.YOUTUBE_URL),
                            ("X_URL", config.X_URL))
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"GitHub Variables 미설정: {', '.join(missing)}\n"
            "  Settings > Secrets and variables > Actions > Variables 탭에서 등록하십시오."
        )

    for name, value in (("YOUTUBE_URL", config.YOUTUBE_URL), ("X_URL", config.X_URL)):
        if not value.startswith("https://"):
            raise RuntimeError(f"{name} 이 https:// 로 시작하지 않습니다: {value!r}")


def _resolve_raw_base_url(settings: Settings) -> str:
    """이미지 자산의 base URL을 결정한다.

    ASSET_RAW_BASE_URL Variable 이 있으면 그것을 쓰되, 명백히 자산 호스트가
    아닌 값(YouTube, X 등)이면 무시하고 레포 raw URL 을 자동 조립한다.
    설정 실수로 발행이 계속 실패하는 것을 막기 위한 방어다.
    """
    from .threads_client import FORBIDDEN_ASSET_HOSTS

    override = os.environ.get("ASSET_RAW_BASE_URL", "").strip().rstrip("/")
    if override:
        host = urlparse(override).netloc.lower()
        if any(host.endswith(bad) for bad in FORBIDDEN_ASSET_HOSTS):
            log.error(
                "ASSET_RAW_BASE_URL 이 자산 호스트가 아닙니다: %s\n"
                "  이 값을 무시하고 레포 raw URL 을 사용합니다.\n"
                "  Variables 에서 ASSET_RAW_BASE_URL 을 삭제하십시오.",
                override,
            )
        elif not override.startswith("https://"):
            log.error(
                "ASSET_RAW_BASE_URL 이 https 로 시작하지 않습니다: %s — 무시합니다.",
                override,
            )
        else:
            return override

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        raise RuntimeError(
            "GITHUB_REPOSITORY 를 알 수 없어 이미지 base URL 을 만들 수 없습니다."
        )
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip() or "main"
    return f"https://raw.githubusercontent.com/{repo}/{branch}/assets"


LEVEL_PREFIX = {
    "critical": "[Threads][최우선]",
    "urgent": "[Threads][긴급]",
    "warn": "[Threads][경고]",
    "unknown": "[Threads][확인필요]",
    "ok": "[Threads]",
}


def _alert_expiry(settings: Settings, cause: str) -> None:
    """영속화가 안 된 상태에서 기존 토큰의 잔여 수명을 경보한다.

    저장이 실패하면 갱신값이 반영되지 않으므로 만료 시계가 멈추지 않는다.
    발급일(TOKEN_ISSUED_AT)을 알면 남은 일수를 계산해 단계별로 알린다.
    """
    assessment = token_manager.assess_expiry(
        dt.datetime.now(KST).date(), config.TOKEN_ISSUED_AT
    )
    prefix = LEVEL_PREFIX.get(assessment.level, "[Threads]")
    body = f"{prefix} 토큰 영속화 실패\n{cause}\n{assessment.message}"

    if assessment.level in ("critical", "urgent"):
        log.error(body)
    else:
        log.warning(body)

    if assessment.should_alert:
        notifier.send(settings.telegram_bot_token, settings.telegram_chat_id, body)


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
        _alert_expiry(
            settings,
            "GH_PAT_SECRETS_WRITE 미설정으로 갱신 토큰을 영속화하지 못했습니다.",
        )
        return new_token

    try:
        token_manager.persist_token_to_secret(
            settings.gh_repo, settings.gh_pat, new_token
        )
    except token_manager.SecretPersistError as exc:
        _alert_expiry(settings, f"토큰 Secret 영속화 실패.\n{exc}")
    return new_token


VERSION = "1.1.0"


def _slot_gate(today: dt.date) -> bool:
    """오늘 이 슬롯이 실행 대상인지. 아니면 즉시 종료해 Actions 분을 아낀다.

    수동 실행(workflow_dispatch)은 슬롯과 무관하게 항상 통과시킨다.
    슬롯 분산은 스케줄 실행에만 적용되는 안티봇 장치이며,
    사람이 직접 누른 실행까지 막으면 검증·긴급 발행이 불가능해진다.
    """
    event = os.environ.get("EVENT_NAME", "").strip()
    if event and event != "schedule":
        log.info("수동 실행(%s) — 슬롯 판정을 건너뜁니다.", event)
        return True

    slots = [s for s in os.environ.get("PUBLISH_SLOTS", "").split(",") if s.strip()]
    current = os.environ.get("SLOT", "").strip()

    if not slots or not current:
        return True

    if current not in slots:
        # cron 문자열과 Resolve slot 의 case 분기가 어긋난 상태.
        # 조용히 매번 발행되면 안 되므로 경고 후 중단한다.
        log.error(
            "슬롯 '%s' 이 등록 목록 %s 에 없습니다. "
            "cron 문자열과 Resolve slot case 분기가 일치하는지 확인하십시오.",
            current, slots,
        )
        return False

    return antibot.should_run_this_slot(
        today, current, slots, config.ANTIBOT_SLOT_SALT_PUBLISH
    )


def run() -> int:
    log.info("[Publish] v%s 시작", VERSION)
    _preflight()
    settings = load_settings()
    today = dt.datetime.now(KST).date()

    if antibot.is_rest_day(today, config.PUBLISH_WEEKLY_REST_DAYS):
        return 0

    if not _slot_gate(today):
        log.info("오늘 슬롯이 아님 — 종료")
        return 0

    token = _acquire_token(settings)

    user_id = settings.threads_user_id
    if user_id == "me":
        user_id, username = fetch_user_id(token)
        log.info("사용자 ID 조회 완료 — @%s (id=%s)", username, user_id)

    client = ThreadsClient(user_id, token)

    quota = client.get_post_quota()
    log.info("발행 쿼터 %d/%d (잔여 %d)", quota.used, quota.total, quota.remaining)
    if quota.remaining < 2:  # 본문 1 + 셀프 리플라이 1
        raise RuntimeError(f"쿼터 부족 — 잔여 {quota.remaining}")

    recent_texts: list[str] = []
    if settings.can_generate and config.AI_ENABLED:
        recent_texts = client.get_recent_texts(config.RECENT_POSTS_FOR_DEDUP)
        log.info("중복 회피용 최근 글 %d건 확보", len(recent_texts))
    else:
        log.info("AI 생성 비활성 — 정적 텍스트 풀 사용")

    collected = facts.collect(
        REPO_ROOT,
        ASSETS_DIR,
        quota_used=quota.used,
        recent_post_count=len(recent_texts),
    )

    plan = content.build_plan(
        today,
        ASSETS_DIR,
        _resolve_raw_base_url(settings),
        claude_api_key=settings.claude_api_key,
        recent_texts=recent_texts,
        facts_block=collected.to_prompt_block(),
    )
    log.info(
        "기둥=%s 소재=%s 생성=%s 근거=%s 이미지=%s",
        plan.pillar, plan.seed, plan.source,
        f"커밋{len(collected.commits)}건" if collected.has_evidence else "없음",
        plan.image_url,
    )

    if settings.dry_run:
        log.info("DRY_RUN — 실제 발행하지 않습니다.\n--- 본문 ---\n%s\n--- 리플 ---\n%s",
                 plan.text, plan.reply_text)
        return 0

    # ------------------------------------------------------------------
    # Tier 1~2 : 사용 가능한 이미지를 찾는다.
    #   지연 이전에 검증한다. 실패가 확정된 요청을 위해 수 분을 대기하면
    #   Actions 분만 낭비된다.
    # ------------------------------------------------------------------
    image_url, degrade_reasons = _select_usable_image(plan, settings)

    if image_url is None and not config.IMAGE_FALLBACK_TO_TEXT:
        raise ImageValidationError(
            "사용 가능한 이미지가 없고 텍스트 폴백이 비활성 상태입니다.\n"
            + "\n".join(degrade_reasons)
        )

    # 안티봇 — 매번 다른 시각에 발행되도록 랜덤 지연
    antibot.jitter_sleep(*config.ANTIBOT_PUBLISH_JITTER, label="발행 전")

    # ------------------------------------------------------------------
    # Tier 3 : 이미지가 없으면 텍스트 전용으로 발행한다.
    #   이미지 하나 때문에 그날 발행을 거르는 것이 더 큰 손해다.
    # ------------------------------------------------------------------
    post_id = None

    if image_url:
        try:
            post_id = client.publish_image_post(image_url, plan.text, dry_run=settings.dry_run)
            log.info("본문 발행 완료 (이미지) post_id=%s", post_id)
        except (ContainerNotReadyError, ThreadsApiError) as exc:
            # 컨테이너 처리 실패도 텍스트 폴백 대상이다.
            # 이미지 때문에 그날 발행을 통째로 잃는 것이 더 큰 손해다.
            log.error("이미지 발행 실패 — 텍스트 폴백으로 전환: %s", exc)
            degrade_reasons.append(f"이미지 발행 실패: {str(exc)[:200]}")
            if not config.IMAGE_FALLBACK_TO_TEXT:
                raise

    if post_id is None:
        post_id = client.publish_text_post(plan.text, dry_run=settings.dry_run)
        log.warning("본문 발행 완료 (텍스트 전용 폴백) post_id=%s", post_id)
        _notify_safe(
            "[Threads] 이미지 없이 텍스트만 발행했습니다.\n"
            + "\n".join(degrade_reasons[:3])
        )

    reply_id = client.publish_self_reply(post_id, plan.reply_text, dry_run=settings.dry_run)
    log.info("셀프 리플라이 발행 완료 reply_id=%s", reply_id)
    return 0


def _select_usable_image(plan, settings: Settings) -> tuple[str | None, list[str]]:
    """검증을 통과하는 이미지 URL 을 찾는다.

    Tier 1  오늘의 1순위 이미지
    Tier 2  같은 디렉토리의 다른 이미지 (최대 IMAGE_CANDIDATE_LIMIT 개)
    실패하면 (None, 사유목록) 을 돌려주고 호출자가 텍스트 폴백을 결정한다.

    반환하는 사유 목록은 알림에 그대로 실어 보낸다. 어떤 파일이 왜 안 됐는지
    남기지 않으면 조용한 품질 저하가 반복된다.
    """
    reasons: list[str] = []

    try:
        assets = content.list_asset_names(ASSETS_DIR)
    except FileNotFoundError as exc:
        reasons.append(f"자산 디렉토리 문제: {exc}")
        return None, reasons

    base_url = _resolve_raw_base_url(settings)
    day_index = content._day_index(dt.datetime.now(KST).date())
    candidates = content.order_asset_candidates(assets, day_index)[
        : config.IMAGE_CANDIDATE_LIMIT
    ]

    for rank, name in enumerate(candidates, start=1):
        url = content.build_image_url(base_url, name)
        try:
            verify_image_url(url)
        except ImageValidationError as exc:
            first_line = str(exc).split("\n")[0]
            log.warning("이미지 후보 %d/%d 실패 — %s: %s",
                        rank, len(candidates), name, first_line)
            reasons.append(f"{name}: {first_line}")
            continue

        if rank > 1:
            log.warning("1순위 이미지 실패 — 대체 이미지 %s 로 발행합니다.", name)
            _notify_safe(
                f"[Threads] 1순위 이미지 실패, 대체본 사용\n대체: {name}\n"
                + "\n".join(reasons)
            )
        return url, reasons

    log.error("사용 가능한 이미지가 없습니다. 후보 %d개 전부 실패.", len(candidates))
    return None, reasons


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
    except ContainerNotReadyError as exc:
        log.error("컨테이너 처리 실패: %s", exc)
        _notify_safe(f"[Threads] 컨테이너 처리 실패\n{exc}")
        return 6
    except ImageValidationError as exc:
        log.error("이미지 검증 실패 — 발행하지 않습니다.\n%s", exc)
        _notify_safe(f"[Threads] 이미지 검증 실패\n{exc}")
        return 5
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

"""Threads Graph API 클라이언트.

발행은 2단계 컨테이너 모델을 따른다.
  1) POST /{user-id}/threads          -> creation_id
  2) POST /{user-id}/threads_publish  -> post_id
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger(__name__)


class ThreadsApiError(RuntimeError):
    """Threads API 호출 실패."""

    def __init__(self, status: int, payload: str, *, code: int | None = None):
        self.status = status
        self.payload = payload
        self.code = code
        super().__init__(f"Threads API {status} (code={code}): {payload[:300]}")

    @property
    def is_auth_error(self) -> bool:
        """OAuthException 190 계열 여부. 재인가가 필요한 상황."""
        return self.code == 190 or self.status == 401

    @property
    def is_blocked(self) -> bool:
        """code 200 — 접근 차단. 사람 조치 없이는 풀리지 않는다.

        개발자 계정 checkpoint, 앱 제한, 권한 박탈 등이 여기 해당한다.
        일시 오류가 아니므로 재시도하면 안 되고, 이후 호출도 멈춰야 한다.
        차단 상태에서 계속 호출하면 판정이 강화된다.
        """
        return self.code == 200 or "access blocked" in self.payload.lower()


@dataclass(frozen=True)
class Quota:
    used: int
    total: int

    @property
    def remaining(self) -> int:
        return max(self.total - self.used, 0)


def _request(method: str, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(1, config.HTTP_RETRY_COUNT + 1):
        try:
            resp = requests.request(
                method, url, params=params, timeout=config.HTTP_TIMEOUT_SEC
            )
        except requests.RequestException as exc:
            last_error = exc
            log.warning("네트워크 오류 (%d/%d): %s", attempt, config.HTTP_RETRY_COUNT, exc)
            time.sleep(config.HTTP_RETRY_BACKOFF_SEC * attempt)
            continue

        if resp.status_code == 200:
            return resp.json()

        code = None
        try:
            code = resp.json().get("error", {}).get("code")
        except ValueError:
            pass

        error = ThreadsApiError(resp.status_code, resp.text, code=code)

        # 인증 오류와 4xx는 재시도해도 동일하므로 즉시 중단한다.
        if error.is_auth_error or 400 <= resp.status_code < 500:
            raise error

        last_error = error
        log.warning("서버 오류 (%d/%d): %s", attempt, config.HTTP_RETRY_COUNT, error)
        time.sleep(config.HTTP_RETRY_BACKOFF_SEC * attempt)

    raise ThreadsApiError(0, f"재시도 소진: {last_error}")


ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/jpg", "image/png")

# 자산 호스트로 쓰일 수 없는 도메인. 설정 오류를 조기에 잡는다.
FORBIDDEN_ASSET_HOSTS = (
    "youtube.com", "youtu.be", "x.com", "twitter.com",
    "threads.com", "threads.net", "instagram.com",
)


class ContainerNotReadyError(RuntimeError):
    """컨테이너가 발행 가능한 상태가 되지 못했다.

    호출자는 이 예외를 잡아 폴백(텍스트 전용 발행)으로 넘어갈 수 있다.
    """


class ImageValidationError(RuntimeError):
    """이미지 URL이 Threads 요구사항을 만족하지 않는다."""


def _diagnose_payload(url: str, content_type: str) -> str:
    """실제 응답 앞부분을 읽어 원인을 구체적으로 짚는다.

    Content-Type 만으로는 "왜 이미지가 아닌지"를 알 수 없다.
    파일 시그니처를 보면 Git LFS 포인터인지, 텍스트 자리표시자인지,
    다른 이미지 포맷인지 바로 구분된다.
    """
    try:
        resp = requests.get(
            url,
            headers={"Range": "bytes=0-511"},
            timeout=config.HTTP_TIMEOUT_SEC,
            allow_redirects=True,
        )
        head = resp.content[:512]
    except requests.RequestException as exc:
        return f"  (원인 진단 실패: {exc})"

    signatures = (
        (b"\x89PNG\r\n\x1a\n", "실제로는 PNG 입니다. 서버가 타입을 잘못 보냈을 수 있습니다."),
        (b"\xff\xd8\xff", "실제로는 JPEG 입니다. 서버가 타입을 잘못 보냈을 수 있습니다."),
        (b"GIF8", "GIF 파일입니다. Threads 는 JPEG/PNG 만 지원합니다."),
        (b"RIFF", "WebP 파일입니다. PNG 로 변환하십시오."),
        (b"\x00\x00\x00 ftypheic", "HEIC 파일입니다. PNG 로 변환하십시오."),
        (b"<!DOCTYPE", "HTML 페이지입니다. raw URL 이 아닙니다."),
        (b"<html", "HTML 페이지입니다. raw URL 이 아닙니다."),
    )
    for magic, message in signatures:
        if head.startswith(magic) or magic in head[:32]:
            return f"  진단: {message}"

    if head.startswith(b"version https://git-lfs"):
        return (
            "  진단: Git LFS 포인터 파일입니다. 실제 이미지가 아닌 텍스트 메타데이터입니다.\n"
            "        .gitattributes 에서 이미지 확장자의 LFS 설정을 제거하고\n"
            "        파일을 일반 바이너리로 다시 커밋하십시오."
        )

    if not head:
        return "  진단: 파일이 비어 있습니다(0바이트)."

    try:
        preview = head.decode("utf-8", errors="replace")[:120].replace("\n", " ")
        return (
            f"  진단: 이미지가 아닌 텍스트 파일입니다. 앞부분: {preview!r}\n"
            "        assets 에 실제 PNG/JPEG 바이너리를 커밋했는지 확인하십시오."
        )
    except Exception:  # noqa: BLE001
        return "  진단: 알 수 없는 바이너리입니다."


def verify_image_url(url: str) -> None:
    """발행 전에 이미지 URL을 검증한다.

    Threads 는 이 URL 에서 직접 이미지를 내려받는다. 접근이 안 되거나
    이미지가 아니면 code=36001(Unknown Image Format) 이 발생한다.
    지연·발행 이전에 잡아야 시간과 쿼터를 낭비하지 않는다.
    """
    if not url.startswith("https://"):
        raise ImageValidationError(f"https 로 시작하지 않습니다: {url}")

    host = urlparse(url).netloc.lower()
    for bad in FORBIDDEN_ASSET_HOSTS:
        if host.endswith(bad):
            raise ImageValidationError(
                f"자산 호스트가 될 수 없는 도메인입니다: {host}\n"
                "  ASSET_RAW_BASE_URL Variable 에 잘못된 값이 들어 있습니다.\n"
                "  해당 Variable 을 삭제하면 레포 raw URL 이 자동 조립됩니다."
            )

    try:
        resp = requests.head(url, timeout=config.HTTP_TIMEOUT_SEC, allow_redirects=True)
        if resp.status_code == 405:  # HEAD 미지원 서버 대비
            resp = requests.get(
                url, timeout=config.HTTP_TIMEOUT_SEC, stream=True, allow_redirects=True
            )
            resp.close()
    except requests.RequestException as exc:
        raise ImageValidationError(f"URL 접근 실패: {exc}") from exc

    if resp.status_code != 200:
        raise ImageValidationError(
            f"HTTP {resp.status_code} — 파일이 없거나 접근할 수 없습니다: {url}"
        )

    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ImageValidationError(
            f"Content-Type 이 {content_type or '없음'} 입니다. "
            f"허용: {', '.join(ALLOWED_IMAGE_TYPES)}\n"
            f"{_diagnose_payload(url, content_type)}"
        )

    length = resp.headers.get("Content-Length")
    if length and int(length) > config.IMAGE_MAX_BYTES:
        raise ImageValidationError(
            f"파일 크기 {int(length):,}바이트 — 상한 {config.IMAGE_MAX_BYTES:,}바이트 초과"
        )

    log.info(
        "이미지 검증 통과 — %s (%s바이트)",
        content_type, f"{int(length):,}" if length else "크기 미상",
    )


def fetch_user_id(access_token: str) -> tuple[str, str]:
    """토큰 소유자의 숫자 사용자 ID와 계정명을 조회한다.

    /me 는 프로필 조회에서만 동작하고, 발행·쿼터 엔드포인트는 숫자 ID를
    요구한다. 따라서 실행 시점에 한 번 조회해서 쓰는 편이 확실하다.
    Secret 에 ID 를 보관할 필요가 없어지고, 잘못된 값이 들어갈 여지도 없다.
    """
    data = _request(
        "GET",
        f"{config.THREADS_API_BASE}/me",
        params={"fields": "id,username", "access_token": access_token},
    )
    user_id = str(data.get("id", ""))
    if not user_id.isdigit():
        raise ThreadsApiError(200, f"사용자 ID 조회 실패: {data}")
    return user_id, str(data.get("username", ""))


class ThreadsClient:
    def __init__(self, user_id: str, access_token: str):
        self._user_id = user_id
        self._token = access_token

    # -- 조회 -------------------------------------------------------------
    def get_media_insights(self, media_id: str, metrics: tuple[str, ...]) -> dict:
        """게시물 인사이트. 읽기 전용."""
        return _request(
            "GET",
            f"{config.THREADS_API_BASE}/{media_id}/insights",
            params={"metric": ",".join(metrics), "access_token": self._token},
        )

    def get_user_insights(
        self,
        metrics: tuple[str, ...],
        since: int | None = None,
        until: int | None = None,
    ) -> dict:
        """사용자 인사이트. 읽기 전용.

        since/until 은 Unix 타임스탬프이며 2024-04-13 이전은 거부된다.
        생략하면 어제~오늘 2일 범위가 기본이다.
        """
        params: dict[str, object] = {
            "metric": ",".join(metrics),
            "access_token": self._token,
        }
        if since is not None:
            params["since"] = max(since, config.INSIGHTS_EARLIEST_TIMESTAMP)
        if until is not None:
            params["until"] = until

        return _request(
            "GET",
            f"{config.THREADS_API_BASE}/{self._user_id}/threads_insights",
            params=params,
        )

    def get_post_quota(self) -> Quota:
        """DB 없이 발행 쿼터를 확인한다. 상태를 API 쪽에 위임하는 것이 핵심."""
        data = _request(
            "GET",
            f"{config.THREADS_API_BASE}/{self._user_id}/threads_publishing_limit",
            params={"fields": "quota_usage,config", "access_token": self._token},
        )
        entry = (data.get("data") or [{}])[0]
        cfg = entry.get("config") or {}
        return Quota(
            used=int(entry.get("quota_usage", 0)),
            total=int(cfg.get("quota_total", config.DAILY_POST_QUOTA)),
        )

    def get_recent_texts(self, limit: int = 8) -> list[str]:
        """최근 발행한 본문을 가져온다. 중복 회피 프롬프트에 넣는다.

        DB 없이 중복을 피하기 위한 방법이다. 실패해도 발행을 막지 않는다.
        """
        try:
            data = _request(
                "GET",
                f"{config.THREADS_API_BASE}/{self._user_id}/threads",
                params={
                    "fields": "text",
                    "limit": limit,
                    "access_token": self._token,
                },
            )
        except ThreadsApiError as exc:
            log.warning("최근 글 조회 실패 — 중복 회피 없이 진행: %s", exc)
            return []

        texts = []
        for item in data.get("data", []):
            value = (item.get("text") or "").strip()
            if value:
                texts.append(value)
        return texts

    def get_reply_quota(self) -> Quota:
        """답글 발행 쿼터. 24시간 이동구간 1,000건이 API 한도."""
        data = _request(
            "GET",
            f"{config.THREADS_API_BASE}/{self._user_id}/threads_publishing_limit",
            params={
                "fields": "reply_quota_usage,reply_config",
                "access_token": self._token,
            },
        )
        entry = (data.get("data") or [{}])[0]
        cfg = entry.get("reply_config") or {}
        return Quota(
            used=int(entry.get("reply_quota_usage", 0)),
            total=int(cfg.get("quota_total", config.DAILY_REPLY_QUOTA)),
        )

    def get_my_posts(self, limit: int = 5) -> list[dict]:
        """최근 내 글 목록. id 와 text 를 함께 받는다."""
        data = _request(
            "GET",
            f"{config.THREADS_API_BASE}/{self._user_id}/threads",
            params={
                "fields": "id,text,timestamp",
                "limit": limit,
                "access_token": self._token,
            },
        )
        return list(data.get("data") or [])

    def get_conversation(self, post_id: str, limit: int = 25) -> list[dict]:
        """글 하나의 전체 대화(최상위+중첩 평탄화)를 가져온다.

        공식 필드 목록에서 필요한 것만 요청한다:
          id, text, username, timestamp, replied_to, is_reply,
          is_reply_owned_by_me, hide_status
        """
        data = _request(
            "GET",
            f"{config.THREADS_API_BASE}/{post_id}/conversation",
            params={
                "fields": (
                    "id,text,username,timestamp,replied_to,"
                    "is_reply,is_reply_owned_by_me,hide_status"
                ),
                "limit": limit,
                "reverse": "false",
                "access_token": self._token,
            },
        )
        return list(data.get("data") or [])

    # -- 발행 -------------------------------------------------------------
    def create_image_container(self, image_url: str, text: str) -> str:
        return self._create_container(
            {
                "media_type": config.MEDIA_TYPE_IMAGE,
                "image_url": image_url,
                "text": text,
            }
        )

    def create_text_container(self, text: str) -> str:
        """텍스트 전용 게시물 컨테이너. 이미지 폴백 시 사용한다."""
        return self._create_container(
            {
                "media_type": config.MEDIA_TYPE_TEXT,
                "text": text,
            }
        )

    def create_reply_container(self, parent_post_id: str, text: str) -> str:
        return self._create_container(
            {
                "media_type": config.MEDIA_TYPE_TEXT,
                "text": text,
                "reply_to_id": parent_post_id,
            }
        )

    def _create_container(self, fields: dict[str, Any]) -> str:
        payload = dict(fields)
        payload["access_token"] = self._token
        data = _request(
            "POST",
            f"{config.THREADS_API_BASE}/{self._user_id}/threads",
            params=payload,
        )
        creation_id = data.get("id")
        if not creation_id:
            raise ThreadsApiError(200, f"creation_id 없음: {data}")
        return str(creation_id)

    def get_container_status(self, container_id: str) -> tuple[str, str]:
        """컨테이너 처리 상태를 조회한다.

        반환: (status, error_message)
        status 는 FINISHED / IN_PROGRESS / ERROR / EXPIRED / PUBLISHED.
        """
        data = _request(
            "GET",
            f"{config.THREADS_API_BASE}/{container_id}",
            params={"fields": "status,error_message", "access_token": self._token},
        )
        return (
            str(data.get("status", "")).upper(),
            str(data.get("error_message", "")),
        )

    def wait_until_ready(
        self, container_id: str, initial_wait_sec: int, *, dry_run: bool = False
    ) -> None:
        """컨테이너가 FINISHED 가 될 때까지 대기한다.

        Meta 는 생성 직후 평균 30초 대기를 권장한다. 즉시 발행하면
        code=24 (Media Not Found) 가 발생한다.
        상태 조회는 1분 간격, 총 5분을 넘기지 않는다.
        """
        if dry_run:
            log.info("DRY_RUN — 컨테이너 대기를 건너뜁니다.")
            return

        log.info("컨테이너 처리 대기 %d초 (id=%s)", initial_wait_sec, container_id)
        time.sleep(initial_wait_sec)

        waited = initial_wait_sec
        while True:
            status, error_message = self.get_container_status(container_id)
            log.info("컨테이너 상태=%s (누적 대기 %d초)", status, waited)

            if status in (
                config.CONTAINER_STATUS_FINISHED,
                config.CONTAINER_STATUS_PUBLISHED,
            ):
                return

            if status == config.CONTAINER_STATUS_ERROR:
                raise ContainerNotReadyError(
                    f"컨테이너 처리 실패 (id={container_id}): "
                    f"{error_message or '사유 미상'}"
                )

            if status == config.CONTAINER_STATUS_EXPIRED:
                raise ContainerNotReadyError(
                    f"컨테이너가 만료되었습니다 (id={container_id}). "
                    "생성 후 24시간이 지났습니다."
                )

            if waited >= config.CONTAINER_POLL_MAX_SEC:
                raise ContainerNotReadyError(
                    f"컨테이너가 {waited}초 안에 준비되지 않았습니다 "
                    f"(id={container_id}, 마지막 상태={status})."
                )

            remaining = config.CONTAINER_POLL_MAX_SEC - waited
            interval = min(config.CONTAINER_POLL_INTERVAL_SEC, remaining)
            time.sleep(interval)
            waited += interval

    def publish(self, creation_id: str) -> str:
        """컨테이너는 생성 후 24시간이면 만료되므로 즉시 발행한다."""
        data = _request(
            "POST",
            f"{config.THREADS_API_BASE}/{self._user_id}/threads_publish",
            params={"creation_id": creation_id, "access_token": self._token},
        )
        post_id = data.get("id")
        if not post_id:
            raise ThreadsApiError(200, f"post_id 없음: {data}")
        return str(post_id)

    def publish_image_post(
        self, image_url: str, text: str, *, dry_run: bool = False
    ) -> str:
        container_id = self.create_image_container(image_url, text)
        self.wait_until_ready(
            container_id, config.CONTAINER_WAIT_IMAGE_SEC, dry_run=dry_run
        )
        return self.publish(container_id)

    def publish_text_post(self, text: str, *, dry_run: bool = False) -> str:
        """텍스트 전용 발행. 이미지 폴백 경로."""
        container_id = self.create_text_container(text)
        self.wait_until_ready(
            container_id, config.CONTAINER_WAIT_TEXT_SEC, dry_run=dry_run
        )
        return self.publish(container_id)

    def publish_self_reply(
        self, parent_post_id: str, text: str, *, dry_run: bool = False
    ) -> str:
        container_id = self.create_reply_container(parent_post_id, text)
        self.wait_until_ready(
            container_id, config.CONTAINER_WAIT_TEXT_SEC, dry_run=dry_run
        )
        return self.publish(container_id)

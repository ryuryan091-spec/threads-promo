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

    def publish_image_post(self, image_url: str, text: str) -> str:
        return self.publish(self.create_image_container(image_url, text))

    def publish_self_reply(self, parent_post_id: str, text: str) -> str:
        return self.publish(self.create_reply_container(parent_post_id, text))

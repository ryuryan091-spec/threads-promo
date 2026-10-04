"""Facebook 페이지 릴스 클라이언트 (v1.7.0 숏폼).

공식 문서(Reels Publishing API) 3단계를 그대로 따른다.
  1) POST {graph}/{page_id}/video_reels  upload_phase=start           → video_id
  2) POST {rupload}/{video_id}  Authorization: OAuth · offset · file_size · 바이너리
  3) POST {graph}/{page_id}/video_reels  upload_phase=finish · video_state=PUBLISHED · description
  상태 확인: GET {graph}/{video_id}?fields=status
  목록: GET {graph}/{page_id}/video_reels (응답 예시 필드: id · description · updated_time)

오류 분류는 threads_client 와 같다. code 190 / HTTP 401 = 토큰 무효, code 200 = 접근 차단
→ 재시도 없이 safety 회로 차단기를 연다.
문서 한도: 릴스 API 게시 24시간 30건.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from . import config, safety
from .redact import redact

VERSION = "1.1.0"   # v1.8.0: recent_reels

log = logging.getLogger(__name__)


class FaceApiError(RuntimeError):
    """Facebook Graph API 호출 실패."""

    def __init__(self, status: int, payload: str, *, code: int | None = None):
        self.status = status
        self.payload = redact(payload)
        self.code = code
        self.video_id = ""          # v1.8.0: 업로드 세션을 연 뒤 실패하면 그 video_id(원장 확인필요 기록용)
        self.definitive = False     # v1.8.0: Facebook 이 처리 실패를 확정한 경우만 True
        super().__init__(f"Facebook API {status} (code={code}): {self.payload[:300]}")

    @property
    def is_auth_error(self) -> bool:
        return self.code == 190 or self.status == 401

    @property
    def is_blocked(self) -> bool:
        return self.code == 200 or "access blocked" in self.payload.lower()


@dataclass(frozen=True)
class ReelStatus:
    video_status: str
    uploading: str
    processing: str
    publishing: str
    error: str

    @property
    def failed(self) -> bool:
        return "error" in (self.video_status, self.uploading, self.processing, self.publishing)

    @property
    def published(self) -> bool:
        return self.publishing == "complete"


def _raise_for(resp: requests.Response) -> None:
    code = None
    try:
        code = resp.json().get("error", {}).get("code")
    except ValueError:
        pass
    error = FaceApiError(resp.status_code, resp.text, code=code)
    if safety.is_account_fatal(error):
        safety.trip(error)
    raise error


def _send(method: str, url: str, *, retry: bool, **kwargs: Any) -> dict[str, Any]:
    """retry=False 는 쓰기(중복 게시 위험) 경로. 네트워크 오류도 재시도하지 않는다."""
    attempts = config.HTTP_RETRY_COUNT if retry else 1
    timeout = kwargs.pop("timeout", config.HTTP_TIMEOUT_SEC * 3)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last = exc
            log.warning("Facebook 네트워크 오류 (%d/%d): %s", attempt, attempts, redact(str(exc)))
            time.sleep(config.HTTP_RETRY_BACKOFF_SEC * attempt)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code < 500 or not retry:
            _raise_for(resp)
        last = FaceApiError(resp.status_code, resp.text)
        log.warning("Facebook 서버 오류 (%d/%d): %s", attempt, attempts, last)
        time.sleep(config.HTTP_RETRY_BACKOFF_SEC * attempt)
    raise FaceApiError(0, f"재시도 소진: {redact(str(last))}")


class FaceClient:
    def __init__(self, page_id: str, page_token: str):
        if not page_id or not page_token:
            raise ValueError("FACE_PAGE_ID / FACE_PAGE_TOKEN 이 필요합니다")
        self._page_id = page_id
        self._token = page_token

    # -- 조회 -------------------------------------------------------------
    def recent_descriptions(self) -> list[str]:
        """최근 릴스 설명 목록(중복 게시 확인용)."""
        data = _send(
            "GET", f"{config.FACE_GRAPH_BASE}/{self._page_id}/video_reels", retry=True,
            params={"access_token": self._token, "limit": config.FACE_REELS_LIST_LIMIT},
        )
        return [str(item.get("description") or "") for item in data.get("data") or []]

    def recent_reels(self) -> list[tuple[str, str]]:
        """v1.8.0 최근 릴스 (id, description). 같은 설명으로 이미 게시된 회차의 video_id 를 원장에 기록할 때 쓴다.

        recent_descriptions 와 같은 요청이다(응답 예시 필드 id · description — 모듈 docstring).
        """
        data = _send(
            "GET", f"{config.FACE_GRAPH_BASE}/{self._page_id}/video_reels", retry=True,
            params={"access_token": self._token, "limit": config.FACE_REELS_LIST_LIMIT},
        )
        return [(str(item.get("id") or ""), str(item.get("description") or ""))
                for item in data.get("data") or []]

    def status(self, video_id: str) -> ReelStatus:
        data = _send("GET", f"{config.FACE_GRAPH_BASE}/{video_id}", retry=True,
                     params={"fields": "status", "access_token": self._token})
        st = data.get("status") or {}

        def phase(name: str) -> str:
            return str((st.get(name) or {}).get("status") or "").lower()

        err = (st.get("processing_phase") or {}).get("error") or {}
        return ReelStatus(
            video_status=str(st.get("video_status") or "").lower(),
            uploading=phase("uploading_phase"),
            processing=phase("processing_phase"),
            publishing=phase("publishing_phase"),
            error=str(err.get("message") or ""),
        )

    # -- 게시 -------------------------------------------------------------
    def publish_reel(self, video: Path, description: str,
                     on_session=None) -> tuple[str, ReelStatus | None]:
        """3단계 게시 후 상태를 확인한다. 반환 (video_id, 마지막 상태 또는 None=시간 초과).

        on_session: v1.8.0 업로드 세션 video_id 를 받는 즉시 호출(원장 선기록). 콜백 오류는 게시를 막지 않는다.
        """
        safety.guard_write()
        start = _send("POST", f"{config.FACE_GRAPH_BASE}/{self._page_id}/video_reels", retry=False,
                      json={"upload_phase": "start", "access_token": self._token})
        video_id = str(start.get("video_id") or "")
        if not video_id:
            raise FaceApiError(200, f"video_id 없음: {start}")
        log.info("릴스 업로드 세션 video_id=%s", video_id)
        if on_session is not None:
            try:
                on_session(video_id)
            except Exception as exc:  # noqa: BLE001 — 기록 실패가 게시를 막지 않는다
                log.warning("세션 콜백 실패(게시는 계속): %s", redact(str(exc)))
        try:
            return video_id, self._upload_and_finish(video, description, video_id)
        except FaceApiError as exc:
            exc.video_id = video_id     # 이미 게시됐을 수 있으므로 호출자가 확인필요로 남길 수 있게 한다
            raise

    def _upload_and_finish(self, video: Path, description: str, video_id: str) -> ReelStatus | None:
        safety.guard_write()
        size = video.stat().st_size
        with video.open("rb") as fh:
            up = _send("POST", f"{config.FACE_RUPLOAD_BASE}/{video_id}", retry=False,
                       headers={"Authorization": f"OAuth {self._token}", "offset": "0",
                                "file_size": str(size)},
                       data=fh, timeout=600)
        if not up.get("success"):
            raise FaceApiError(200, f"업로드 응답 success 아님: {up}")
        log.info("릴스 파일 전송 완료 %s바이트", f"{size:,}")

        safety.guard_write()
        _send("POST", f"{config.FACE_GRAPH_BASE}/{self._page_id}/video_reels", retry=False,
              params={"access_token": self._token, "video_id": video_id, "upload_phase": "finish",
                      "video_state": "PUBLISHED", "description": description})
        log.info("릴스 게시 요청 완료 video_id=%s — 처리 상태 확인", video_id)
        return self.wait_status(video_id)

    def wait_status(self, video_id: str) -> ReelStatus | None:
        """게시 완료·실패가 확정될 때까지 확인. 상한(FACE_STATUS_MAX_SEC)을 넘기면 None(확인 필요)."""
        waited = 0
        last: ReelStatus | None = None
        while waited <= config.FACE_STATUS_MAX_SEC:
            last = self.status(video_id)
            log.info("릴스 상태 video=%s 업로드=%s 처리=%s 게시=%s", last.video_status, last.uploading,
                     last.processing, last.publishing)
            if last.failed or last.published:
                return last
            time.sleep(config.FACE_STATUS_POLL_SEC)
            waited += config.FACE_STATUS_POLL_SEC
        return None

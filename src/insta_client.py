"""Instagram 릴스 클라이언트 (v1.9.0 숏폼).

Instagram API with Facebook Login 경로. Facebook 페이지 토큰(FACE_PAGE_TOKEN)을 그대로 쓴다.
공식 문서(Instagram Platform › Content Publishing, IG User Media 레퍼런스) 흐름을 따른다.
  0) IG 사용자 ID   GET {graph}/{page_id}?fields=instagram_business_account{id,username}
  1) 컨테이너 생성  POST {graph}/{ig_id}/media  media_type=REELS · upload_type=resumable · caption
                    · share_to_feed · is_ai_generated                                → 컨테이너 id
  2) 파일 전송      POST {rupload}/{컨테이너 id}  Authorization: OAuth · offset · file_size · 바이너리
  3) 상태 확인      GET {graph}/{컨테이너 id}?fields=status_code,status
                    IN_PROGRESS → FINISHED (ERROR · EXPIRED 는 실패 확정). 문서 권장: 1분 간격 · 5분 이내
  4) 게시           POST {graph}/{ig_id}/media_publish  creation_id                     → 미디어 id
  목록(중복 확인)   GET {graph}/{ig_id}/media?fields=id,caption,media_product_type,timestamp
  한도              GET {graph}/{ig_id}/content_publishing_limit  (24시간 이동 창)

게시물이 공개되는 쓰기는 4) 하나뿐이다. 1)~3)에서 멈추면 컨테이너는 비공개로 남고 24시간 뒤 만료된다.
그래서 Facebook 처럼 회차 원장으로 '확인필요'를 추적하지 않고, 4)의 응답을 잃은 경우만
다음 실행의 캡션 중복 확인(recent_captions)으로 막는다.

오류 분류는 face_client 와 같다(code 190 / HTTP 401 = 토큰 무효, code 200 = 접근 차단 → 회로 차단).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, safety
from .face_client import FaceApiError
from .face_client import _send as _face_send

VERSION = "1.0.0"

log = logging.getLogger(__name__)

STATUS_FINISHED = "FINISHED"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_FAILED = ("ERROR", "EXPIRED")


class InstaApiError(FaceApiError):
    """Instagram Graph API 호출 실패. 같은 페이지 토큰을 쓰므로 Facebook 오류 분류를 그대로 쓴다."""

    def __init__(self, status: int, payload: str, *, code: int | None = None):
        super().__init__(status, payload, code=code)
        self.args = (f"Instagram API {status} (code={code}): {self.payload[:300]}",)

    @classmethod
    def from_face(cls, exc: FaceApiError) -> InstaApiError:
        out = cls(exc.status, exc.payload, code=exc.code)
        out.video_id, out.definitive, out.before_session = exc.video_id, exc.definitive, exc.before_session
        return out


def _send(method: str, url: str, *, retry: bool, **kwargs: Any) -> dict[str, Any]:
    """face_client._send 와 같은 재시도·회로 차단 규칙. 오류만 InstaApiError 로 바꾼다."""
    try:
        return _face_send(method, url, retry=retry, **kwargs)
    except InstaApiError:
        raise
    except FaceApiError as exc:
        # 회로 차단기는 face_client 가 이미 열었다(같은 토큰 — Facebook 쓰기도 함께 멈춘다).
        raise InstaApiError.from_face(exc) from exc


@dataclass(frozen=True)
class InstaResult:
    """게시 1편 결과. media_id 가 있으면 공개됨. 없으면 status 가 이유(시간 초과 등)."""

    container_id: str
    media_id: str
    status: str


class InstaClient:
    def __init__(self, page_id: str, page_token: str, ig_user_id: str = ""):
        if not page_id or not page_token:
            raise ValueError("FACE_PAGE_ID / FACE_PAGE_TOKEN 이 필요합니다")
        self._page_id = page_id
        self._token = page_token
        self._ig_id = ig_user_id.strip()
        self._username = ""

    # -- 계정 -------------------------------------------------------------
    def account(self) -> tuple[str, str]:
        """(IG 사용자 ID, 계정 이름). 페이지에 IG 프로페셔널 계정이 연결되어 있지 않으면 InstaApiError."""
        if self._ig_id and self._username:
            return self._ig_id, self._username
        data = _send("GET", f"{config.INSTA_GRAPH_BASE}/{self._page_id}", retry=True,
                     params={"fields": "instagram_business_account{id,username}",
                             "access_token": self._token})
        acct = data.get("instagram_business_account") or {}
        ig_id = str(acct.get("id") or "")
        if not ig_id:
            raise InstaApiError(404, "페이지에 연결된 Instagram 프로페셔널 계정이 없습니다"
                                     "(instagram_business_account 없음 — 연결 또는 instagram_basic 권한 확인)")
        self._ig_id, self._username = ig_id, str(acct.get("username") or "")
        return self._ig_id, self._username

    @property
    def ig_id(self) -> str:
        """INSTA_USER_ID 를 주면 조회 없이 그 값을 쓴다(계정 이름이 필요한 사전 점검만 account() 호출)."""
        return self._ig_id or self.account()[0]

    # -- 조회 -------------------------------------------------------------
    def recent_media(self, limit: int | None = None) -> list[dict[str, Any]]:
        data = _send("GET", f"{config.INSTA_GRAPH_BASE}/{self.ig_id}/media", retry=True,
                     params={"fields": "id,caption,media_product_type,timestamp",
                             "limit": limit or config.INSTA_MEDIA_LIST_LIMIT,
                             "access_token": self._token})
        return [i for i in (data.get("data") or []) if isinstance(i, dict)]

    def recent_captions(self) -> list[tuple[str, str]]:
        """최근 게시물 (미디어 id, 캡션). 중복 게시 확인용."""
        return [(str(i.get("id") or ""), str(i.get("caption") or "")) for i in self.recent_media()]

    def publishing_limit(self) -> tuple[int | None, int | None]:
        """(24시간 사용량, 한도). 응답에 값이 없으면 None — 호출자는 판단 불가로 다룬다."""
        data = _send("GET", f"{config.INSTA_GRAPH_BASE}/{self.ig_id}/content_publishing_limit",
                     retry=True, params={"fields": "quota_usage,config", "access_token": self._token})
        rows = data.get("data") or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        usage = row.get("quota_usage")
        total = (row.get("config") or {}).get("quota_total")
        return (int(usage) if isinstance(usage, int) else None,
                int(total) if isinstance(total, int) else None)

    def container_status(self, container_id: str) -> tuple[str, str]:
        data = _send("GET", f"{config.INSTA_GRAPH_BASE}/{container_id}", retry=True,
                     params={"fields": "status_code,status", "access_token": self._token})
        return str(data.get("status_code") or "").upper(), str(data.get("status") or "")

    # -- 게시 -------------------------------------------------------------
    def publish_reel(self, video: Path, caption: str) -> InstaResult:
        """컨테이너 생성 → 파일 전송 → 상태 확인 → 게시. 처리 실패 확정은 InstaApiError(definitive=True).

        상태 확인이 시간 안에 끝나지 않으면 게시하지 않고 InstaResult(media_id="", status="TIMEOUT").
        """
        ig_id = self.ig_id
        safety.guard_write()
        try:
            created = _send("POST", f"{config.INSTA_GRAPH_BASE}/{ig_id}/media", retry=False,
                            json={"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                                  "share_to_feed": config.INSTA_SHARE_TO_FEED,
                                  "is_ai_generated": config.INSTA_IS_AI_GENERATED,
                                  "access_token": self._token})
        except InstaApiError as exc:
            exc.before_session = True
            raise
        container_id = str(created.get("id") or "")
        if not container_id:
            raise InstaApiError(200, f"컨테이너 id 없음: {created}")
        log.info("IG 릴스 컨테이너 id=%s", container_id)
        upload_url = str(created.get("uri") or f"{config.INSTA_RUPLOAD_BASE}/{container_id}")

        try:
            safety.guard_write()
            size = video.stat().st_size
            with video.open("rb") as fh:
                up = _send("POST", upload_url, retry=False,
                           headers={"Authorization": f"OAuth {self._token}", "offset": "0",
                                    "file_size": str(size)},
                           data=fh, timeout=600)
            if not up.get("success"):
                raise InstaApiError(200, f"업로드 응답 success 아님: {up}")
            log.info("IG 릴스 파일 전송 완료 %s바이트", f"{size:,}")

            code = self.wait_finished(container_id)
            if code != STATUS_FINISHED:
                return InstaResult(container_id, "", code or "TIMEOUT")

            safety.guard_write()
            published = _send("POST", f"{config.INSTA_GRAPH_BASE}/{ig_id}/media_publish", retry=False,
                              json={"creation_id": container_id, "access_token": self._token})
        except InstaApiError as exc:
            exc.video_id = container_id
            raise
        media_id = str(published.get("id") or "")
        log.info("IG 릴스 게시 완료 media_id=%s", media_id or "-")
        return InstaResult(container_id, media_id, STATUS_PUBLISHED)

    def wait_finished(self, container_id: str) -> str:
        """FINISHED 가 되면 그 값을, 시간 초과면 "" 을 돌려준다. ERROR·EXPIRED 는 실패 확정 예외."""
        waited = 0
        while waited <= config.INSTA_STATUS_MAX_SEC:
            code, detail = self.container_status(container_id)
            log.info("IG 컨테이너 상태 %s %s", code or "-", detail)
            if code in (STATUS_FINISHED, STATUS_PUBLISHED):
                return STATUS_FINISHED
            if code in STATUS_FAILED:
                exc = InstaApiError(200, f"릴스 처리 실패 container={container_id}: {code} {detail}")
                exc.video_id, exc.definitive = container_id, True
                raise exc
            time.sleep(config.INSTA_STATUS_POLL_SEC)
            waited += config.INSTA_STATUS_POLL_SEC
        return ""

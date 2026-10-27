"""
p76/graph.py
============
Facebook Graph API (v25.0) — 사건파일 76 페이지 전용.

  쓰기  POST /{page-id}/photos  source(multipart) + caption → id, post_id   (F-07, 10MB)
  읽기  GET  /{page-id}?fields=id,name,followers_count
        GET  /debug_token?input_token=…                                      (health H2)
        GET  /{post-id}?fields=reactions.summary(total_count).limit(0),shares
        GET  /{post-id}?fields=comments.summary(total_count).limit(0)
        GET  /{post-id}/insights?metric=…                                     (read_insights 필요)

운영 원칙 (fb-page-publisher graph_client 와 같은 계약)
  - 쓰기는 재시도하지 않는다. 결과 status: ok / failed / unknown
      failed  : 게시되지 않음이 확정 (4xx, 연결 수립 실패)
      unknown : 전송 후 결과 미확정 (읽기 타임아웃, 연결 끊김, 5xx) → 확인필요
  - 읽기만 재시도(2회). code 190·10·200대·368 은 회로 차단(safety.trip).
  - 토큰·ID 는 결과·로그에 남기지 않는다. 댓글 쓰기·답글·DM 엔드포인트는 두지 않는다(DSN §9).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

from p76 import safety, settings
from p76.redact import redact

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
READ_RETRY = 2
_sleep = time.sleep


def classify(code: int | None, http_status: int) -> str:
    if code == 190 or http_status == 401:
        return "auth_error"
    if code == 10 or (code is not None and 200 <= code <= 299):
        return "permission_error"
    if code in (4, 17, 32, 613, 80001):
        return "rate_limited"
    if code == 368:
        return "policy_blocked"
    if code == 100:
        return "invalid_param"
    return "api_error"


def _parse_error(resp: requests.Response) -> tuple[int | None, str]:
    try:
        err = resp.json().get("error", {}) or {}
        return err.get("code"), str(err.get("message", ""))[:200]
    except ValueError:
        return None, (resp.text or "")[:200]


def _check_trip(code: int | None, http_status: int, where: str) -> None:
    if safety.is_trip_code(code, http_status):
        safety.trip(f"{where}:{classify(code, http_status)}:code={code}")


# ── 읽기 ──────────────────────────────────────────────────
def get(path: str, params: dict | None = None) -> tuple[bool, dict, str]:
    """(성공, body, 오류코드문자열). 5xx·연결 오류만 재시도."""
    query = dict(params or {})
    query["access_token"] = settings.PAGE_TOKEN
    reason = ""
    for attempt in range(READ_RETRY + 1):
        try:
            resp = requests.get(f"{settings.GRAPH_BASE}/{path}", params=query, timeout=settings.HTTP_TIMEOUT_SEC)
        except requests.exceptions.RequestException as e:
            reason = f"request_error:{type(e).__name__}"
            if attempt < READ_RETRY:
                _sleep(float(2 ** attempt))
                continue
            logger.warning(f"[Graph] GET 실패 {reason}: {redact(e)[:80]}")
            return False, {}, reason
        if resp.status_code >= 500 and attempt < READ_RETRY:
            _sleep(float(2 ** attempt))
            continue
        break
    if resp.status_code == 200:
        try:
            return True, resp.json(), ""
        except ValueError:
            return False, {}, "invalid_json"
    code, message = _parse_error(resp)
    _check_trip(code, resp.status_code, "read")
    reason = f"http_{resp.status_code}:{classify(code, resp.status_code)}:code={code}"
    logger.warning(f"[Graph] GET 실패 {reason}: {redact(message)[:120]}")
    return False, {"error_code": code}, reason


def page_info() -> tuple[bool, dict, str]:
    return get(settings.PAGE_ID, {"fields": "id,name,followers_count"})


def debug_token() -> tuple[bool, dict, str]:
    """앱 토큰이 있으면 앱 토큰으로, 없으면 페이지 토큰 자신으로 조회."""
    app_token = f"{settings.APP_ID}|{settings.APP_SECRET}" if settings.APP_ID and settings.APP_SECRET else ""
    try:
        resp = requests.get(
            f"{settings.GRAPH_BASE}/debug_token",
            params={"input_token": settings.PAGE_TOKEN, "access_token": app_token or settings.PAGE_TOKEN},
            timeout=settings.HTTP_TIMEOUT_SEC,
        )
    except requests.exceptions.RequestException as e:
        return False, {}, f"request_error:{type(e).__name__}"
    if resp.status_code != 200:
        code, _ = _parse_error(resp)
        _check_trip(code, resp.status_code, "debug_token")
        return False, {}, f"http_{resp.status_code}:code={code}"
    try:
        return True, resp.json().get("data") or {}, ""
    except ValueError:
        return False, {}, "invalid_json"


def post_engagement(post_id: str) -> tuple[dict[str, int | None], list[str]]:
    """(지표, 결과코드 목록). 반응·공유와 댓글을 따로 조회해 하나가 막혀도 나머지는 남긴다."""
    out: dict[str, int | None] = {"reactions": None, "shares": None, "comments": None}
    codes: list[str] = []
    ok, body, reason = get(post_id, {"fields": "reactions.summary(total_count).limit(0),shares"})
    if ok:
        out["reactions"] = _summary_total(body.get("reactions"))
        # shares 는 0 이면 응답에서 빠진다
        out["shares"] = int((body.get("shares") or {}).get("count", 0))
    else:
        codes.append(f"reactions:{reason}")
    if safety.tripped():
        return out, codes
    ok, body, reason = get(post_id, {"fields": "comments.summary(total_count).limit(0)"})
    if ok:
        out["comments"] = _summary_total(body.get("comments"))
    else:
        codes.append(f"comments:{reason}")
    return out, codes


def _summary_total(edge: dict | None) -> int | None:
    total = ((edge or {}).get("summary") or {}).get("total_count")
    return int(total) if isinstance(total, (int, float)) else None


def post_insights(post_id: str, metrics: tuple[str, ...]) -> tuple[dict[str, float], list[str]]:
    """지표 묶음 조회 → 실패하면 지표별로 다시 조회(invalid metric 하나가 전체를 막지 않게, F-02)."""
    if not metrics:
        return {}, ["insights:skipped"]
    values, codes = _insights_once(post_id, metrics)
    if values or len(metrics) == 1 or safety.tripped():
        return values, codes
    values, codes = {}, []
    for metric in metrics:
        v, c = _insights_once(post_id, (metric,))
        values.update(v)
        codes.extend(c)
        if safety.tripped():
            break
    return values, codes


def _insights_once(post_id: str, metrics: tuple[str, ...]) -> tuple[dict[str, float], list[str]]:
    ok, body, reason = get(f"{post_id}/insights", {"metric": ",".join(metrics), "period": "lifetime"})
    if not ok:
        return {}, [f"insights:{'+'.join(metrics)}:{reason}"]
    values: dict[str, float] = {}
    for row in body.get("data") or []:
        series = row.get("values") or []
        value = series[-1].get("value") if series and isinstance(series[-1], dict) else None
        if isinstance(value, (int, float)):
            values[str(row.get("name"))] = float(value)
    missing = [m for m in metrics if m not in values]
    return values, [f"insights:{m}:no_value" for m in missing]


# ── 쓰기 ──────────────────────────────────────────────────
def _result(status: str, object_id: str = "", reason: str = "", error_code: int | None = None) -> dict:
    return {"status": status, "object_id": object_id, "reason": reason, "error_code": error_code}


def _write(step: str, send: Callable[[], requests.Response]) -> tuple[dict | None, dict | None]:
    try:
        resp = send()
    except requests.exceptions.ConnectTimeout as e:
        logger.error(f"[Graph] {step} 연결 실패(미전송): {redact(e)[:80]}")
        return None, _result("failed", reason=f"{step}:connect_timeout")
    except requests.exceptions.RequestException as e:
        # 연결 수립 실패인지 전송 후 끊김인지 구분할 수 없으므로 보수적으로 unknown
        logger.error(f"[Graph] {step} 응답 미확정 — 게시 여부 확인 필요: {type(e).__name__}")
        return None, _result("unknown", reason=f"{step}:{type(e).__name__}")
    except OSError:
        return None, _result("failed", reason=f"{step}:file_read_error")
    if resp.status_code == 200:
        try:
            return resp.json(), None
        except ValueError:
            return None, _result("unknown", reason=f"{step}:invalid_json")
    code, message = _parse_error(resp)
    if resp.status_code >= 500:
        logger.error(f"[Graph] {step} 서버 오류 {resp.status_code} (code={code}) — 확인 필요")
        return None, _result("unknown", reason=f"{step}:http_{resp.status_code}", error_code=code)
    _check_trip(code, resp.status_code, step)
    kind = classify(code, resp.status_code)
    logger.error(f"[Graph] {step} 실패 {resp.status_code} {kind} (code={code}): {redact(message)[:120]}")
    return None, _result("failed", reason=f"{step}:{kind}", error_code=code)


def post_photo(image_path: str, caption: str) -> dict:
    if not caption.strip():
        return _result("failed", reason="empty_caption")
    if not settings.PAGE_ID or not settings.PAGE_TOKEN:
        return _result("failed", reason="not_configured")
    safety.guard_graph_write()
    url = f"{settings.GRAPH_BASE}/{settings.PAGE_ID}/photos"

    def send() -> requests.Response:
        with open(image_path, "rb") as fh:
            return requests.post(
                url, data={"caption": caption, "access_token": settings.PAGE_TOKEN},
                files={"source": fh}, timeout=settings.UPLOAD_TIMEOUT_SEC,
            )

    body, fail = _write("photos", send)
    if fail:
        return fail
    post_id = str(body.get("post_id") or body.get("id") or "")
    if not post_id:
        return _result("unknown", reason="photos:no_post_id")
    logger.info("[Graph] photo 게시 완료")
    return _result("ok", object_id=post_id)

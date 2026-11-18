"""
p76/graph.py
============
Facebook Graph API (v25.0) — 사건파일 76 페이지 전용.

  쓰기  POST /{page-id}/photos  source(multipart) + caption → id, post_id   (F-07, 10MB)
        예약 게시 2단계 (DN2026_0119, DSN-P76-NOVEL-003 §8 — 단일 사진 예약은 문서에 명시 없음)
          ① POST /{page-id}/photos  source + published=false + temporary=true → id (미게시 임시 사진)
          ② POST /{page-id}/feed    message + attached_media[0]={"media_fbid": id} + published=false
                                    + scheduled_publish_time + unpublished_content_type=SCHEDULED → id
        DELETE /{post-id}                       예약 취소 (문서끼리 다름 — E-1 실측 대상)
        POST   /{post-id}     is_published=false 공개된 게시물 비공개 전환 (중단 체크 후 T 경과, 리뷰 W1 —
                                                 PagePost 업데이트 파라미터 /reference/page-post, 확인 필요: E-1 실측)
        POST   /{comment-id}  is_hidden=true    댓글 숨김 (DN2026_0119 A3 — 답글·삭제·차단 없음)
  읽기  GET  /{page-id}?fields=id,name,followers_count
        GET  /debug_token?input_token=…                                      (health H2)
        GET  /{post-id}?fields=reactions.summary(total_count).limit(0),shares
        GET  /{post-id}?fields=comments.summary(total_count).limit(0)
        GET  /{post-id}/insights?metric=…                                     (read_insights 필요)
        GET  /{page-id}/scheduled_posts?fields=id,scheduled_publish_time      (예약 대조)
        GET  /{post-id}?fields=is_published,created_time,scheduled_publish_time (게시 확인)
        GET  /{post-id}/comments?fields=id,message,is_hidden                  (숨김 판정 — 본문은 로그 금지)
        GET  /{page-id}/posts?fields=id,created_time,status_type[,message]    (FB ID 없는 확인필요 대조 — 본문 로그 금지)

운영 원칙 (fb-page-publisher graph_client 와 같은 계약)
  - 쓰기는 재시도하지 않는다. 결과 status: ok / failed / unknown
      failed  : 게시되지 않음이 확정 (4xx, 연결 수립 실패)
      unknown : 전송 후 결과 미확정 (읽기 타임아웃, 연결 끊김, 5xx) → 확인필요
  - 읽기만 재시도(2회). 쓰기의 code 190·10·200대·368, 읽기의 190 은 회로 차단(safety.trip).
    읽기 권한 부족(10·200대)은 결과 코드로만 돌려준다.
  - 토큰·ID·캡션·댓글 본문은 로그에 남기지 않는다. 답글·DM 엔드포인트는 두지 않는다(DSN §9, DN2026_0068).
    댓글 쓰기는 숨김(is_hidden)만 둔다(DN2026_0119 A3).
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

import requests

from p76 import safety, settings
from p76.redact import redact

VERSION = "1.3.0"  # 1.3.0: 리뷰 W1 unpublish_post · W4 page_posts(with_message)
# 1.2.0: DN2026_0119 예약 게시 2단계·예약 조회·취소·게시 확인·댓글 숨김

logger = logging.getLogger(__name__)
READ_RETRY = 2
_sleep = time.sleep
_clock = time.time  # 예약 시각 범위 검사용 (테스트가 바꿔 끼운다)
SCHEDULE_MIN_SEC = 10 * 60          # /feed 문서: 예약 시각은 10분 ~ 30일 뒤 (DSN-P76-NOVEL-003 §8)
SCHEDULE_MAX_SEC = 30 * 24 * 3600
_probe = False  # DN2026_0119 E-1 예약 점검(probe) 실행 중이면 MODE 와 무관하게 쓰기 허용(회로 차단은 지킨다)


def set_probe(on: bool) -> None:
    """E-1 예약 점검 전용 (run_novel_publish probe 진입점만 호출). 회로 차단(safety.trip)은 그대로 적용된다."""
    global _probe
    _probe = bool(on)


def _guard() -> None:
    if _probe:
        if safety.tripped():
            raise safety.Tripped(safety.tripped())
        return
    safety.guard_graph_write()


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
    # 읽기는 토큰 무효(190·401)만 회로 차단. 권한 부족(10·200대)은 그 지표만 빈칸으로 두고 계속한다
    # (2026-10-28 실측: 게시물 반응 조회에 pages_read_user_content 필요 — 하나 막혔다고 수집 전체를 멈추지 않음)
    if classify(code, resp.status_code) == "auth_error":
        _check_trip(code, resp.status_code, "read")
    reason = f"http_{resp.status_code}:{classify(code, resp.status_code)}:code={code}"
    logger.warning(f"[Graph] GET 실패 {reason}: {redact(message)[:120]}")
    return False, {"error_code": code}, reason


def page_info() -> tuple[bool, dict, str]:
    return get(settings.PAGE_ID, {"fields": "id,name,followers_count"})


def page_posts(since: int, until: int, max_pages: int = 5, with_message: bool = False) -> tuple[bool, list[dict], str]:
    """페이지 게시물 목록 (since·until = Unix 초). 본문(message)은 with_message=True 일 때만 가져온다 —
    호출부가 메모리에서 해시 대조에만 쓰고 로그·기록에 남기지 않는다(리뷰 W4)."""
    posts: list[dict] = []
    fields = "id,created_time,status_type" + (",message" if with_message else "")
    params = {"fields": fields, "since": since, "until": until, "limit": 50}
    for _ in range(max_pages):
        ok, body, reason = get(f"{settings.PAGE_ID}/posts", params)
        if not ok:
            return False, posts, reason
        posts += [p for p in body.get("data") or [] if isinstance(p, dict) and p.get("id")]
        after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (body.get("paging") or {}).get("next"):
            break
        params = {**params, "after": after}
    return True, posts, ""


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
        if "permission_error" in reason:
            return out, [*codes, "comments:skipped_same_permission"]
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
    _guard()
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


# ── DN2026_0119 예약 게시 · 확인 · 취소 · 댓글 숨김 ─────────────
def scheduled_posts(max_pages: int = 3) -> tuple[bool, list[dict], str]:
    """예약 게시물 목록 [{id, scheduled_publish_time}] — 등록 확인·대조용."""
    out: list[dict] = []
    params = {"fields": "id,scheduled_publish_time", "limit": 50}
    for _ in range(max_pages):
        ok, body, reason = get(f"{settings.PAGE_ID}/scheduled_posts", params)
        if not ok:
            return False, out, reason
        out += [p for p in body.get("data") or [] if isinstance(p, dict) and p.get("id")]
        after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (body.get("paging") or {}).get("next"):
            break
        params = {**params, "after": after}
    return True, out, ""


def get_post(post_id: str) -> tuple[bool, dict, str]:
    """게시 여부 확인 — is_published 는 '예약 게시물이 게시되었는가'(/reference/page-post)."""
    return get(post_id, {"fields": "is_published,created_time,scheduled_publish_time"})


def list_comments(post_id: str, max_pages: int = 5) -> tuple[bool, list[dict], str]:
    """댓글 [{id, message, is_hidden}]. message 는 판정에만 쓰고 로그·기록에 남기지 않는다.
    filter=stream: 답글까지 포함(확인 필요 — R3, /reference/object/comments)."""
    out: list[dict] = []
    params = {"fields": "id,message,is_hidden", "filter": "stream", "limit": 100}
    for _ in range(max_pages):
        ok, body, reason = get(f"{post_id}/comments", params)
        if not ok:
            return False, out, reason
        out += [c for c in body.get("data") or [] if isinstance(c, dict) and c.get("id")]
        after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (body.get("paging") or {}).get("next"):
            break
        params = {**params, "after": after}
    return True, out, ""


def schedule_photo(image_path: str, caption: str, publish_ts: int, now_ts: float | None = None) -> dict:
    """사진 예약 게시 2단계. ①실패(미확정 포함) = failed — 미게시 임시 사진만 남고 공개·예약된 것은 없다.
    ②4xx = failed, ②응답 미확정(타임아웃·5xx·id 없음) = unknown → 호출부는 재등록하지 않고 대조한다.
    now_ts: 호출부의 현재 시각(초). 예약 시각이 그 기준 10분~30일 밖이면 요청 없이 failed."""
    if not caption.strip():
        return _result("failed", reason="empty_caption")
    if not settings.PAGE_ID or not settings.PAGE_TOKEN:
        return _result("failed", reason="not_configured")
    lead = int(publish_ts) - int(_clock() if now_ts is None else now_ts)
    if not SCHEDULE_MIN_SEC <= lead <= SCHEDULE_MAX_SEC:
        return _result("failed", reason="schedule:out_of_range")
    _guard()
    base = f"{settings.GRAPH_BASE}/{settings.PAGE_ID}"

    def send_photo() -> requests.Response:
        with open(image_path, "rb") as fh:
            return requests.post(
                f"{base}/photos", data={"published": "false", "temporary": "true",
                                        "access_token": settings.PAGE_TOKEN},
                files={"source": fh}, timeout=settings.UPLOAD_TIMEOUT_SEC,
            )

    body, fail = _write("schedule_photo", send_photo)
    if fail:
        return {**fail, "status": "failed"}
    # 응답 키: 문서상 id (확인 필요 — E-1 실측). post_id 는 미게시 사진에서 오지 않을 수 있다
    photo_id = str((body or {}).get("id") or "")
    if not photo_id:
        return _result("failed", reason="schedule_photo:no_id")
    _guard()

    def send_feed() -> requests.Response:
        return requests.post(
            f"{base}/feed",
            data={"message": caption, "attached_media[0]": json.dumps({"media_fbid": photo_id}),
                  "published": "false", "scheduled_publish_time": str(int(publish_ts)),
                  "unpublished_content_type": "SCHEDULED", "access_token": settings.PAGE_TOKEN},
            timeout=settings.HTTP_TIMEOUT_SEC,
        )

    body, fail = _write("schedule_feed", send_feed)
    if fail:
        return fail
    post_id = str((body or {}).get("id") or "")  # 응답 키: 문서상 id (확인 필요 — E-1 실측)
    if not post_id:
        return _result("unknown", reason="schedule_feed:no_id")
    logger.info("[Graph] 예약 게시 등록 완료")
    return _result("ok", object_id=post_id)


def _success(step: str, body: dict | None) -> dict:
    if (body or {}).get("success") is True:
        return _result("ok")
    return _result("unknown", reason=f"{step}:no_success")


def delete_post(post_id: str) -> dict:
    """예약 게시물 취소(DELETE). ok 는 응답 success=true 일 때만."""
    if not post_id or not settings.PAGE_TOKEN:
        return _result("failed", reason="not_configured")
    _guard()
    body, fail = _write("delete", lambda: requests.delete(
        f"{settings.GRAPH_BASE}/{post_id}", params={"access_token": settings.PAGE_TOKEN},
        timeout=settings.HTTP_TIMEOUT_SEC))
    return fail or _success("delete", body)


def unpublish_post(post_id: str) -> dict:
    """공개된 게시물을 비공개로 전환(POST /{post-id} is_published=false, 리뷰 W1).
    근거: PagePost 업데이트 파라미터 is_published(/reference/page-post). 실제 동작은 확인 필요(E-1 실측 전).
    ok 는 응답 success=true 일 때만 — 삭제·답글은 하지 않는다."""
    if not post_id or not settings.PAGE_TOKEN:
        return _result("failed", reason="not_configured")
    _guard()
    body, fail = _write("unpublish", lambda: requests.post(
        f"{settings.GRAPH_BASE}/{post_id}", data={"is_published": "false", "access_token": settings.PAGE_TOKEN},
        timeout=settings.HTTP_TIMEOUT_SEC))
    return fail or _success("unpublish", body)


def hide_comment(comment_id: str) -> dict:
    """댓글 숨김(is_hidden=true) — Page 게시물 댓글 한정. 답글·삭제·차단은 하지 않는다."""
    if not comment_id or not settings.PAGE_TOKEN:
        return _result("failed", reason="not_configured")
    _guard()
    body, fail = _write("hide", lambda: requests.post(
        f"{settings.GRAPH_BASE}/{comment_id}", data={"is_hidden": "true", "access_token": settings.PAGE_TOKEN},
        timeout=settings.HTTP_TIMEOUT_SEC))
    return fail or _success("hide", body)

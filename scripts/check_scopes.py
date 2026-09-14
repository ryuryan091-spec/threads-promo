"""답글 엔진에 필요한 scope 보유 여부를 실제 API 호출로 진단한다.

Threads 는 토큰의 scope 목록을 직접 조회하는 공개 엔드포인트를 제공하지 않는다.
따라서 각 권한이 필요한 최소 호출을 실제로 던져보고 성공/실패로 역판정한다.

검사 대상
  threads_basic            GET /me
  threads_content_publish  GET /{user-id}/threads_publishing_limit (quota_usage)
  threads_read_replies     GET /{post-id}/conversation
  threads_manage_replies   GET /{user-id}/threads_publishing_limit (reply_quota_usage)

주의: 실제 답글을 발행하지 않는다. GET 만 사용한다.
      따라서 threads_manage_replies 는 '읽기 필드 접근 가능'으로 간접 판정한다.
      확정 판정은 답글 워크플로우 dry_run 이 아니라 live 1건으로만 가능하다.
"""

from __future__ import annotations

import os
import sys

import requests

VERSION = "1.0.0"
API_BASE = "https://graph.threads.net/v1.0"
TIMEOUT = 20

OK = "OK  "
NG = "FAIL"
SKIP = "SKIP"


def _get(path: str, params: dict) -> tuple[bool, str]:
    try:
        resp = requests.get(f"{API_BASE}/{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return False, f"네트워크 오류: {exc}"

    if resp.status_code == 200:
        return True, resp.text[:200]

    try:
        err = resp.json()["error"]
        msg = f"{resp.status_code} code={err.get('code')} {err.get('message', '')[:120]}"
    except Exception:  # noqa: BLE001
        msg = f"{resp.status_code} {resp.text[:150]}"
    return False, msg


def main() -> int:
    token = os.environ.get("THREADS_LONG_LIVED_TOKEN", "").strip()
    if not token:
        print("[FAIL] THREADS_LONG_LIVED_TOKEN 이 없습니다.")
        return 1

    print(f"[ScopeCheck] v{VERSION} 시작\n")
    results: dict[str, bool] = {}

    # 1) threads_basic
    ok, detail = _get("me", {"fields": "id,username", "access_token": token})
    results["threads_basic"] = ok
    print(f"[{OK if ok else NG}] threads_basic")
    if not ok:
        print(f"       {detail}")
        print("\n기본 권한이 없습니다. 이후 검사는 의미가 없습니다.")
        return 1

    user_id = ""
    username = ""
    try:
        body = requests.get(
            f"{API_BASE}/me",
            params={"fields": "id,username", "access_token": token},
            timeout=TIMEOUT,
        ).json()
        user_id = str(body.get("id", ""))
        username = str(body.get("username", ""))
        print(f"       계정 @{username} (id={user_id})")
    except Exception:  # noqa: BLE001
        pass

    # 2) threads_content_publish
    ok, detail = _get(
        f"{user_id}/threads_publishing_limit",
        {"fields": "quota_usage,config", "access_token": token},
    )
    results["threads_content_publish"] = ok
    print(f"[{OK if ok else NG}] threads_content_publish")
    if not ok:
        print(f"       {detail}")

    # 3) threads_manage_replies (답글 쿼터 필드 접근으로 간접 판정)
    ok, detail = _get(
        f"{user_id}/threads_publishing_limit",
        {"fields": "reply_quota_usage,reply_config", "access_token": token},
    )
    results["threads_manage_replies"] = ok
    print(f"[{OK if ok else NG}] threads_manage_replies  (간접 판정)")
    if not ok:
        print(f"       {detail}")

    # 4) threads_read_replies — 내 글 하나를 잡아 conversation 조회
    ok_posts, _ = _get(
        f"{user_id}/threads", {"fields": "id", "limit": 1, "access_token": token}
    )
    if not ok_posts:
        results["threads_read_replies"] = False
        print(f"[{SKIP}] threads_read_replies — 내 글 조회 실패로 검사 불가")
    else:
        posts = requests.get(
            f"{API_BASE}/{user_id}/threads",
            params={"fields": "id", "limit": 1, "access_token": token},
            timeout=TIMEOUT,
        ).json().get("data") or []
        if not posts:
            results["threads_read_replies"] = False
            print(f"[{SKIP}] threads_read_replies — 발행된 글이 없어 검사 불가")
        else:
            post_id = posts[0]["id"]
            ok, detail = _get(
                f"{post_id}/conversation",
                {
                    "fields": "id,text,is_reply_owned_by_me",
                    "limit": 1,
                    "access_token": token,
                },
            )
            results["threads_read_replies"] = ok
            print(f"[{OK if ok else NG}] threads_read_replies")
            if not ok:
                print(f"       {detail}")

    # 5) threads_manage_insights
    ok, detail = _get(
        f"{user_id}/threads_insights",
        {"metric": "views", "access_token": token},
    )
    results["threads_manage_insights"] = ok
    print(f"[{OK if ok else NG}] threads_manage_insights")
    if not ok:
        print(f"       {detail}")

    # 판정
    print("\n===== 판정 =====")
    publish_ready = results.get("threads_basic") and results.get(
        "threads_content_publish"
    )
    reply_ready = results.get("threads_read_replies") and results.get(
        "threads_manage_replies"
    )

    print(f"발행 엔진 : {'사용 가능' if publish_ready else '권한 부족'}")
    insights_ready = results.get("threads_manage_insights", False)
    print(f"인사이트  : {'사용 가능' if insights_ready else '권한 부족'}")
    print(f"답글 엔진 : {'사용 가능' if reply_ready else '권한 부족'}")

    if not reply_ready:
        print(
            "\n답글 권한이 없습니다. 조치:\n"
            "  1. Meta 콘솔 > 이용 사례 > Threads API 액세스 > 맞춤 설정 > 권한 및 기능\n"
            "  2. threads_read_replies, threads_manage_replies 추가\n"
            "  3. 설정 화면의 사용자 토큰 생성기로 토큰 재발급\n"
            "  4. THREADS_LONG_LIVED_TOKEN Secret 교체 후 이 검사 재실행\n"
            "  임시 조치: Variables 에 REPLY_ENABLED=false 로 답글 엔진 정지"
        )
        return 1

    print("\n모든 권한 확인. 답글 엔진 dry_run 을 진행하십시오.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

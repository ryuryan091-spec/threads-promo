"""최초 1회 실행 스크립트 — 인가 코드 -> 장수명 토큰 + THREADS_USER_ID.

이 단계는 자동화할 수 없다. 인가 코드는 사람이 브라우저에서
Authorization Window에 로그인·동의해야 발급되며, 1시간 유효하고 1회만 쓸 수 있다.
여기서 로그인한 Threads 계정이 곧 게시물 작성자가 된다.

사용법
  1) 아래 URL을 브라우저에서 연다 (client_id / redirect_uri 치환).

     https://threads.com/oauth/authorize
       ?client_id=<THREADS_APP_ID>
       &redirect_uri=<REDIRECT_URI>
       &scope=threads_basic,threads_content_publish
       &response_type=code

     주의: 나중에 답글 자동화를 붙일 계획이면 threads_manage_replies를
           지금 함께 승인받아야 한다. 나중에 추가하면 재인가가 필요하다.

  2) 리디렉션된 주소의 code 파라미터를 복사한다.
  3) python scripts/bootstrap_token.py --code <CODE> --redirect-uri <URI>
  4) 출력된 값을 GitHub Secrets에 등록한다.
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

AUTH_BASE = "https://graph.threads.net"
API_BASE = "https://graph.threads.net/v1.0"
TIMEOUT = 20


def exchange_code_for_short_lived(
    app_id: str, app_secret: str, code: str, redirect_uri: str
) -> str:
    resp = requests.post(
        f"{AUTH_BASE}/oauth/access_token",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(f"단기 토큰 교환 실패 {resp.status_code}: {resp.text[:400]}")
    return resp.json()["access_token"]


def exchange_short_for_long_lived(app_secret: str, short_token: str) -> dict:
    resp = requests.get(
        f"{AUTH_BASE}/access_token",
        params={
            "grant_type": "th_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(f"장수명 교환 실패 {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def fetch_user(long_token: str) -> dict:
    resp = requests.get(
        f"{API_BASE}/me",
        params={"fields": "id,username", "access_token": long_token},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(f"프로필 조회 실패 {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True, help="Authorization Window가 발급한 코드")
    parser.add_argument("--redirect-uri", required=True, help="앱에 등록한 것과 완전히 동일해야 함")
    args = parser.parse_args()

    app_id = os.environ.get("THREADS_APP_ID", "").strip()
    app_secret = os.environ.get("THREADS_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise SystemExit("THREADS_APP_ID / THREADS_APP_SECRET 환경변수를 먼저 설정하십시오.")

    short_token = exchange_code_for_short_lived(
        app_id, app_secret, args.code, args.redirect_uri
    )
    long_body = exchange_short_for_long_lived(app_secret, short_token)
    long_token = long_body["access_token"]
    expires_days = int(long_body.get("expires_in", 0)) // 86400

    user = fetch_user(long_token)

    print("\n===== GitHub Secrets 등록값 =====")
    print(f"THREADS_USER_ID          = {user.get('id')}")
    print(f"THREADS_LONG_LIVED_TOKEN = {long_token}")
    print("=================================")
    print(f"\n작성자 계정 : @{user.get('username')}")
    print(f"토큰 유효   : 약 {expires_days}일")
    print("이 계정 명의로 게시물이 발행됩니다. 의도한 계정이 맞는지 확인하십시오.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

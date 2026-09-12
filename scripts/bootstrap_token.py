"""최초 1회 토큰 발급 — 인가 코드 -> 단기 토큰 -> 장수명 토큰.

이 단계는 자동화할 수 없다. 인가 코드는 사람이 브라우저에서 Authorization
Window에 로그인·동의해야 발급되며, 1시간 유효하고 1회만 쓸 수 있다.
여기서 로그인한 Threads 계정이 곧 게시물 작성자가 된다.

두 가지 모드
  --persist 없음 : 값을 화면에 출력한다. 로컬 실행용.
  --persist 있음 : 값을 화면에 출력하지 않고 GitHub Secret에 직접 기록한다.
                   Actions 실행용. 복사 과정에서 생기는 손상을 원천 차단한다.

필수 환경변수
  THREADS_APP_ID, THREADS_APP_SECRET
  --persist 사용 시 추가: GH_PAT_SECRETS_WRITE, GITHUB_REPOSITORY
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import token_manager  # noqa: E402

AUTH_BASE = "https://graph.threads.net"
API_BASE = "https://graph.threads.net/v1.0"
TIMEOUT = 20


def extract_code(raw: str) -> str:
    """인가 코드를 추출한다.

    전체 리디렉션 URL을 그대로 넣어도 되고, code 값만 넣어도 된다.
    주소창 끝의 '#_' 는 코드의 일부가 아니므로 제거한다.
    """
    value = raw.strip().strip('"').strip("'")

    # 전체 URL을 붙여넣은 경우 -> 쿼리스트링에서 추출
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        codes = parse_qs(parsed.query).get("code")
        if not codes:
            raise SystemExit(
                "URL에 code 파라미터가 없습니다.\n"
                f"  입력: {value[:80]}...\n"
                "  인가에 실패했거나 잘못된 URL입니다. 인가 URL 접속부터 다시 하십시오."
            )
        value = codes[0]

    if value.startswith("code="):
        value = value[len("code="):]

    for suffix in ("#_", "#"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]

    return value.strip()


def validate_code(code: str) -> None:
    """명백히 코드가 아닌 값을 교환 전에 차단한다.

    인가 코드는 1회용이므로, 잘못된 값으로 시도해 실패하면
    원래 코드가 이미 소모되었을 수 있다. 사전 차단이 중요하다.
    """
    if not code:
        raise SystemExit("인가 코드가 비어 있습니다.")

    if len(code) == 32 and all(c in "0123456789abcdefABCDEF" for c in code):
        raise SystemExit(
            f"입력값이 32자 16진 문자열입니다 (길이 {len(code)}).\n"
            "  THREADS_APP_SECRET 을 인가 코드 자리에 넣었을 가능성이 큽니다.\n"
            "  브라우저 인가 후 리디렉션된 주소의 code= 값을 사용하십시오."
        )

    if len(code) < 60:
        raise SystemExit(
            f"인가 코드가 너무 짧습니다 (길이 {len(code)}).\n"
            "  Meta 인가 코드는 통상 이보다 훨씬 깁니다. 복사 중 잘렸을 수 있습니다.\n"
            "  주소창을 클릭하고 Ctrl+A 로 전체 선택해 URL 전체를 붙여넣으십시오.\n"
            "  이 스크립트는 전체 URL 입력을 지원합니다."
        )

    if not code.startswith("AQ"):
        print(
            f"[경고] 인가 코드가 'AQ' 로 시작하지 않습니다 (시작: {code[:4]}). "
            "값이 올바른지 확인하십시오.",
            file=sys.stderr,
        )


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
        raise SystemExit(
            "STEP2 실패 (인가 코드 -> 단기 토큰)\n"
            f"  status={resp.status_code}\n  body={resp.text[:400]}\n"
            "  점검: 코드 1시간 초과 / 코드 재사용 / redirect_uri 불일치"
        )
    body = resp.json()
    if "access_token" not in body:
        raise SystemExit(f"STEP2 응답에 access_token 없음: {body}")
    return body["access_token"]


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
        raise SystemExit(
            "STEP3 실패 (단기 -> 장수명)\n"
            f"  status={resp.status_code}\n  body={resp.text[:400]}"
        )
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
    parser.add_argument(
        "--code",
        required=True,
        help="리디렉션된 전체 URL 또는 code 값. 전체 URL 권장",
    )
    parser.add_argument("--redirect-uri", required=True, help="앱에 등록한 값과 완전 동일")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="값을 출력하지 않고 GitHub Secret에 직접 기록",
    )
    args = parser.parse_args()

    app_id = os.environ.get("THREADS_APP_ID", "").strip()
    app_secret = os.environ.get("THREADS_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise SystemExit("THREADS_APP_ID / THREADS_APP_SECRET 환경변수를 설정하십시오.")

    code = extract_code(args.code)
    validate_code(code)
    redirect_uri = args.redirect_uri.strip()

    print(f"[STEP2] 인가 코드 교환 (코드 길이 {len(code)})")
    short_token = exchange_code_for_short_lived(app_id, app_secret, code, redirect_uri)
    print("[STEP2] 완료 — 단기 토큰 획득")

    print("[STEP3] 장수명 토큰 교환")
    long_body = exchange_short_for_long_lived(app_secret, short_token)
    long_token = long_body["access_token"]
    expires_days = int(long_body.get("expires_in", 0)) // 86400
    print(f"[STEP3] 완료 — 유효 약 {expires_days}일, 토큰 길이 {len(long_token)}")

    user = fetch_user(long_token)
    user_id = str(user.get("id", ""))
    username = user.get("username", "")

    print(f"\n작성자 계정 : @{username}")
    print("이 계정 명의로 게시물이 발행됩니다. 의도한 계정이 맞는지 확인하십시오.")

    if not args.persist:
        print("\n===== GitHub Secrets 등록값 =====")
        print("아래 '값 부분만' 복사하십시오. 키 이름과 등호는 포함하지 마십시오.")
        print("\n[THREADS_USER_ID]")
        print(user_id)
        print("\n[THREADS_LONG_LIVED_TOKEN]")
        print(long_token)
        print("\n=================================\n")
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    pat = os.environ.get("GH_PAT_SECRETS_WRITE", "").strip()
    if not repo or not pat:
        raise SystemExit(
            "--persist 에는 GITHUB_REPOSITORY 와 GH_PAT_SECRETS_WRITE 가 필요합니다."
        )

    token_manager.persist_token_to_secret(repo, pat, user_id, "THREADS_USER_ID")
    print("[PERSIST] THREADS_USER_ID 기록 완료")
    token_manager.persist_token_to_secret(
        repo, pat, long_token, "THREADS_LONG_LIVED_TOKEN"
    )
    print("[PERSIST] THREADS_LONG_LIVED_TOKEN 기록 완료")
    print("\n두 Secret이 갱신되었습니다. 값은 출력하지 않았습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

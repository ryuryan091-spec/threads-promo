"""Threads 인가(Authorization Window) URL 생성기.

STEP 1(인가 코드 획득)용 URL을 정확히 조립한다.
손으로 만들면 URL 인코딩과 scope 구분자에서 실수가 나므로 스크립트로 만든다.

사용 예
  python scripts/build_auth_url.py \
      --app-id 1234567890123456 \
      --redirect-uri "https://yumens2-byte.github.io/threads-promo/" \
      --with-replies

옵션
  --with-replies : 답글 자동화(X Reply Engine 이식)를 나중에 붙일 계획이면 지정.
                   scope는 사후 추가가 불가하고 전체 재인가가 필요하므로
                   계획이 있다면 지금 함께 승인받아야 한다.
  --with-insights: 게시물 인사이트 조회가 필요하면 지정.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlencode, urlparse

AUTHORIZE_ENDPOINT = "https://threads.com/oauth/authorize"

SCOPE_BASE = ["threads_basic", "threads_content_publish"]
SCOPE_REPLIES = ["threads_read_replies", "threads_manage_replies"]
SCOPE_INSIGHTS = ["threads_manage_insights"]


# 붙여넣기 중 앞글자가 잘렸을 때 나타나는 대표적 형태
TRUNCATED_SCHEMES = {
    "ttps": "https",
    "tps": "https",
    "ps": "https",
    "ttp": "http",
    "tp": "http",
    "htps": "https",
    "htt": "http",
}


def validate_redirect_uri(uri: str) -> list[str]:
    """앱에 등록한 값과 완전히 일치해야 하므로 흔한 실수를 사전 차단한다.

    여기서 걸러내지 못하면 Meta 는 4476001(리디렉션 URI 없음)로 응답한다.
    """
    problems: list[str] = []

    if uri != uri.strip():
        problems.append("앞뒤 공백 또는 개행이 포함되어 있습니다.")
    if " " in uri.strip():
        problems.append("중간에 공백이 포함되어 있습니다.")

    parsed = urlparse(uri.strip())
    scheme = parsed.scheme

    if scheme in TRUNCATED_SCHEMES:
        problems.append(
            f"scheme 이 '{scheme}' 입니다. 붙여넣기 중 앞글자가 잘린 것으로 보입니다. "
            f"'{TRUNCATED_SCHEMES[scheme]}://' 로 시작해야 합니다."
        )
    elif scheme != "https":
        problems.append(
            f"scheme 이 '{scheme}' 입니다. Meta 는 HTTPS 를 요구합니다."
        )

    if not parsed.netloc:
        problems.append("호스트가 비어 있습니다. URL 형식이 아닙니다.")

    return problems


def build(app_id: str, redirect_uri: str, scopes: list[str]) -> str:
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
    }
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", required=True, help="Threads app ID (일반 Meta App ID 아님)")
    parser.add_argument("--redirect-uri", required=True, help="앱 Settings에 등록한 값과 완전 동일")
    parser.add_argument("--with-replies", action="store_true")
    parser.add_argument("--with-insights", action="store_true")
    parser.add_argument(
        "--basic-only",
        action="store_true",
        help="threads_basic 만 요청. scope 문제와 redirect_uri 문제를 분리하는 격리 테스트용",
    )
    args = parser.parse_args()

    app_id = args.app_id.strip()
    redirect_uri = args.redirect_uri.strip()

    if not app_id:
        print("[중단] app-id 가 비어 있습니다.", file=sys.stderr)
        print("       THREADS_APP_ID Secret 이 등록되어 있는지 확인하십시오.", file=sys.stderr)
        print("       client_id 없이 인가하면 error_code 4476002 가 발생합니다.", file=sys.stderr)
        return 1

    if app_id.startswith("<") or app_id.endswith(">"):
        print(f"[중단] app-id 에 꺾쇠가 남아 있습니다: {app_id!r}", file=sys.stderr)
        print("       플레이스홀더를 실제 값으로 교체하십시오.", file=sys.stderr)
        return 1

    if not app_id.isdigit():
        print(f"[경고] app-id가 숫자가 아닙니다: {app_id!r}", file=sys.stderr)
        print("       Threads app ID는 숫자입니다. App Secret을 잘못 넣었는지 확인하십시오.\n",
              file=sys.stderr)

    problems = validate_redirect_uri(redirect_uri)
    if problems:
        print(f"[중단] redirect-uri 가 올바르지 않습니다: {redirect_uri!r}", file=sys.stderr)
        for problem in problems:
            print(f"       - {problem}", file=sys.stderr)
        print("       잘못된 값으로 인가하면 error_code 4476001 이 발생합니다.",
              file=sys.stderr)
        return 1

    if args.basic_only:
        scopes = ["threads_basic"]
        print("[격리모드] threads_basic 만 요청합니다. 성공하면 원인은 scope 미등록입니다.",
              file=sys.stderr)
        url = build(app_id, redirect_uri, scopes)
        print("\n===== 격리 테스트 URL (한 줄 복사) =====\n")
        print(url)
        print(f"\n[검증] client_id 포함 여부: "
              f"{'OK' if 'client_id=' in url and app_id in url else 'FAIL'}")
        print("\n결과 해석")
        print("  인가 화면이 뜬다  -> 원인은 scope. 콘솔 Permissions 탭에서 누락 scope 추가")
        print("  동일 오류가 난다  -> 원인은 redirect_uri. 콘솔 등록·저장 상태 재확인\n")
        return 0

    scopes = list(SCOPE_BASE)
    if args.with_replies:
        scopes += SCOPE_REPLIES
    if args.with_insights:
        scopes += SCOPE_INSIGHTS

    url = build(app_id, redirect_uri, scopes)

    print("\n===== STEP 1. 아래 URL 한 줄을 그대로 복사해 주소창에 붙여넣으십시오 =====")
    print("       (줄바꿈이 섞이면 client_id 가 전송되지 않아 4476002 오류가 납니다)\n")
    print(url)
    print(f"\n[검증] URL 길이 {len(url)}자 · client_id 포함 여부: "
          f"{'OK' if 'client_id=' in url and app_id in url else 'FAIL'}")
    print("\n===== 승인 요청 scope =====")
    for s in scopes:
        print(f"  - {s}")
    print("\n===== 이후 처리 =====")
    print("1) 발행 주체가 될 Threads 계정으로 로그인 · 동의")
    print("2) 리디렉션된 주소창의 code= 값을 복사")
    print("3) 끝에 붙은 '#_' 는 코드가 아니므로 반드시 제거")
    print("4) 1시간 안에 아래 실행")
    print(f'   python scripts/bootstrap_token.py --code "<CODE>" '
          f'--redirect-uri "{redirect_uri}"')
    print("\n[주의] 인가 코드는 1시간 유효 · 1회용입니다. 실패 시 이 URL부터 다시 시작합니다.")
    print("[주의] 여기서 로그인한 계정이 게시물 작성자로 확정됩니다.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

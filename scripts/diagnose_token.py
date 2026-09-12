"""THREADS_LONG_LIVED_TOKEN 형식 진단.

'Cannot parse access token' 은 만료가 아니라 문자열 자체가 토큰이 아니라는 뜻이다.
이 스크립트는 토큰 값을 절대 출력하지 않고, 형식 특성만 보고한다.

사용
  로컬:   THREADS_LONG_LIVED_TOKEN=... python scripts/diagnose_token.py
  Actions: 워크플로우에 임시 스텝으로 추가해 실행

종료코드
  0 = 형식상 이상 없음 (실제 유효성은 --live 로 확인)
  1 = 형식 이상 검출
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

API_BASE = "https://graph.threads.net/v1.0"
TIMEOUT = 20


def mask(value: str) -> str:
    """앞 4자 + 뒤 4자만 노출. 로그 유출 방지."""
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]} (길이 {len(value)})"


def inspect(raw: str) -> list[str]:
    """형식 이상 목록을 반환한다. 비어 있으면 정상."""
    problems: list[str] = []

    if raw != raw.strip():
        problems.append("앞뒤 공백 또는 개행이 포함되어 있습니다.")

    value = raw.strip()

    if not value:
        problems.append("값이 비어 있습니다.")
        return problems

    if any(ch in value for ch in ('"', "'", "`")):
        problems.append("따옴표가 포함되어 있습니다. Secret에는 따옴표 없이 값만 넣습니다.")

    if any(ch in value for ch in ("\n", "\r", "\t")):
        problems.append("개행 또는 탭이 포함되어 있습니다.")

    if " " in value:
        problems.append("중간에 공백이 포함되어 있습니다.")

    if value.endswith("#_") or "#_" in value:
        problems.append(
            "'#_' 가 포함되어 있습니다. 인가 코드 복사 시 딸려오는 문자이며 토큰의 일부가 아닙니다."
        )

    if value.startswith("AQ"):
        problems.append(
            "값이 'AQ' 로 시작합니다. 인가 코드(authorization code)를 토큰 자리에 "
            "등록했을 가능성이 큽니다. 인가 코드는 토큰이 아니며 1시간 후 만료됩니다."
        )

    if value.isdigit():
        problems.append("값이 숫자로만 구성되어 있습니다. THREADS_USER_ID 를 잘못 넣었을 수 있습니다.")

    if len(value) == 32 and all(c in "0123456789abcdef" for c in value.lower()):
        problems.append("32자 16진 문자열입니다. THREADS_APP_SECRET 을 잘못 넣었을 수 있습니다.")

    if len(value) < 60:
        problems.append(
            f"길이가 {len(value)}자로 짧습니다. 장수명 액세스 토큰은 통상 이보다 훨씬 깁니다. "
            "복사 중 잘렸을 가능성을 확인하십시오."
        )

    return problems


def persist_user_id(user_id: str) -> None:
    """조회한 사용자 ID를 GitHub Secret 에 기록한다."""
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from src import token_manager

    repo = _os.environ.get("GITHUB_REPOSITORY", "").strip()
    pat = _os.environ.get("GH_PAT_SECRETS_WRITE", "").strip()
    if not repo or not pat:
        print("[PERSIST] 생략 — GITHUB_REPOSITORY / GH_PAT_SECRETS_WRITE 미설정")
        print("[PERSIST] 아래 값을 THREADS_USER_ID Secret 에 직접 등록하십시오:")
        print(f"          {user_id}")
        return

    token_manager.persist_token_to_secret(repo, pat, user_id, "THREADS_USER_ID")
    print("[PERSIST] THREADS_USER_ID 기록 완료")


def live_check(token: str, do_persist: bool = False) -> int:
    """실제 API로 유효성 확인. 성공 시 작성자 계정을 출력한다."""
    resp = requests.get(
        f"{API_BASE}/me",
        params={"fields": "id,username", "access_token": token},
        timeout=TIMEOUT,
    )
    if resp.status_code == 200:
        body = resp.json()
        user_id = str(body.get("id", ""))
        print(f"[LIVE] 유효. 작성자 계정 = @{body.get('username')} (id={user_id})")
        print("       이 계정 명의로 게시물이 발행됩니다.")
        if do_persist and user_id:
            persist_user_id(user_id)
        return 0

    print(f"[LIVE] 실패 {resp.status_code}: {resp.text[:300]}")
    try:
        message = resp.json()["error"]["message"]
    except Exception:  # noqa: BLE001
        return 1

    if "Cannot parse" in message:
        print("  -> 문자열이 토큰 형식이 아닙니다. Secret 등록값을 교체하십시오.")
    elif "expired" in message.lower():
        print("  -> 만료되었습니다. 인가부터 재수행이 필요합니다.")
    elif "not accepted the invite" in message:
        print("  -> Threads Tester 초대 수락이 완료되지 않았습니다.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="실제 API 호출로 유효성까지 확인"
    )
    parser.add_argument(
        "--persist-user-id",
        action="store_true",
        help="조회한 THREADS_USER_ID 를 GitHub Secret 에 기록 (--live 필요)",
    )
    args = parser.parse_args()

    raw = os.environ.get("THREADS_LONG_LIVED_TOKEN")
    if raw is None:
        print("[FAIL] 환경변수 THREADS_LONG_LIVED_TOKEN 이 설정되어 있지 않습니다.")
        return 1

    print(f"[값] {mask(raw.strip())}")

    problems = inspect(raw)
    if problems:
        print("\n[형식 이상 검출]")
        for p in problems:
            print(f"  - {p}")
        print("\n조치: bootstrap_token.py 로 장수명 토큰을 재발급한 뒤 Secret 을 교체하십시오.")
        return 1

    print("[형식] 이상 없음")

    if args.live:
        return live_check(raw.strip(), args.persist_user_id)

    print("실제 유효성 확인은 --live 옵션으로 수행하십시오.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

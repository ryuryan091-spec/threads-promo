"""장수명 토큰 갱신 및 영속화.

지시사항: "토큰 재발급은 프로그램 내에서 발급하고 해당값을 변수로 받아서 호출".
→ refresh()가 새 토큰을 반환하고, 같은 실행에서 그 변수를 그대로 쓴다.

추가 처리(설계 판단):
  갱신 응답의 새 토큰을 버리면 다음 실행이 다시 옛 토큰을 쓰게 되고,
  옛 토큰의 만료 시각은 갱신해도 앞당겨지지 않으므로 60일째 파이프라인이
  정지한다. 따라서 새 토큰을 GitHub Secret에 덮어써 영속화한다.
  DB는 도입하지 않고 Secret 하나만 상태로 둔다.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from dataclasses import dataclass

import requests
from nacl import encoding, public

from . import config

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
TOKEN_SECRET_NAME = "THREADS_LONG_LIVED_TOKEN"


class TokenRefreshError(RuntimeError):
    pass


class SecretPersistError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExpiryAssessment:
    """저장 실패 상태에서 기존 토큰이 얼마나 남았는지에 대한 판정."""

    days_left: int | None      # 발급일 미상이면 None
    level: str                 # ok | warn | urgent | critical | unknown
    message: str

    @property
    def should_alert(self) -> bool:
        return self.level in ("warn", "urgent", "critical", "unknown")


def effective_issue_date(issued_at_raw: str, refreshed_at_raw: str) -> str:
    """만료 판정의 기준일을 정한다.

    주 1회 갱신 체제에서는 마지막 갱신일이 실제 기준이다.
    갱신 기록이 없으면 최초 발급일로 판정한다.
    """
    candidates = [
        value.strip()
        for value in (issued_at_raw, refreshed_at_raw)
        if value and value.strip()
    ]
    valid: list[dt.date] = []
    for value in candidates:
        try:
            valid.append(dt.date.fromisoformat(value))
        except ValueError:
            continue
    if not valid:
        return ""
    return max(valid).isoformat()


def assess_expiry(today: dt.date, issued_at_raw: str) -> ExpiryAssessment:
    """발급일 기준으로 기존 토큰의 잔여 수명을 판정한다.

    저장이 실패해 갱신값이 반영되지 않은 상태에서만 의미가 있다.
    저장이 성공하면 토큰은 매번 새로 60일이 되므로 경보가 불필요하다.
    """
    if not issued_at_raw:
        return ExpiryAssessment(
            None,
            "unknown",
            "TOKEN_ISSUED_AT Variable 이 없어 잔여 수명을 계산할 수 없습니다. "
            "토큰 발급일(YYYY-MM-DD)을 등록하면 만료 전에 경보를 받을 수 있습니다.",
        )

    try:
        issued = dt.date.fromisoformat(issued_at_raw)
    except ValueError:
        return ExpiryAssessment(
            None,
            "unknown",
            f"TOKEN_ISSUED_AT 형식이 잘못되었습니다: {issued_at_raw!r} (YYYY-MM-DD 필요)",
        )

    days_left = config.TOKEN_LIFETIME_DAYS - (today - issued).days
    expiry = issued + dt.timedelta(days=config.TOKEN_LIFETIME_DAYS)

    if days_left <= 0:
        level = "critical"
        message = (
            f"토큰이 이미 만료되었을 수 있습니다 (발급 {issued}, 만료 예정 {expiry}). "
            "즉시 재발급하십시오."
        )
    elif days_left <= config.TOKEN_CRITICAL_DAYS:
        level = "critical"
        message = f"토큰 만료까지 {days_left}일. 오늘 재발급하십시오. (만료 {expiry})"
    elif days_left <= config.TOKEN_URGENT_DAYS:
        level = "urgent"
        message = f"토큰 만료까지 {days_left}일. 재발급을 서두르십시오. (만료 {expiry})"
    elif days_left <= config.TOKEN_WARN_DAYS:
        level = "warn"
        message = f"토큰 만료까지 {days_left}일. PAT 를 복구하거나 재발급을 준비하십시오. (만료 {expiry})"
    else:
        level = "ok"
        message = f"토큰 만료까지 {days_left}일 (만료 {expiry})."

    return ExpiryAssessment(days_left, level, message)


def refresh_long_lived_token(current_token: str) -> str:
    """장수명 토큰을 갱신하고 새 토큰 문자열을 반환한다.

    갱신 가능 조건: 발급 후 24시간 경과 + 미만료 + threads_basic 권한 보유.
    일 1회 실행 스케줄이면 24시간 조건은 항상 충족된다.
    """
    try:
        resp = requests.get(
            f"{config.THREADS_AUTH_BASE}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": current_token},
            timeout=config.HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        # 네트워크 오류도 TokenRefreshError 로 감싼다.
        # 그래야 호출자가 기존 토큰으로 폴백할 수 있다.
        raise TokenRefreshError(f"네트워크 오류: {exc}") from exc

    if resp.status_code != 200:
        raise TokenRefreshError(f"{resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    new_token = body.get("access_token")
    if not new_token:
        raise TokenRefreshError(f"access_token 없음: {body}")

    expires_in = body.get("expires_in")
    if expires_in:
        log.info("토큰 갱신 완료. 남은 유효기간 약 %d일", int(expires_in) // 86400)
    return str(new_token)


def _encrypt_for_repo(public_key_b64: str, secret_value: str) -> str:
    """GitHub Secret은 저장소 공개키로 libsodium sealed box 암호화가 필요하다."""
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")


def persist_token_to_secret(
    repo: str, pat: str, token_value: str, secret_name: str = TOKEN_SECRET_NAME
) -> None:
    """값을 GitHub Secret에 덮어쓴다.

    주의: 워크플로우 기본 GITHUB_TOKEN으로는 불가하다.
          secrets:write 권한을 가진 Fine-grained PAT가 별도로 필요하다.
    """
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        key_resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/actions/secrets/public-key",
            headers=headers,
            timeout=config.HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        raise SecretPersistError(f"공개키 조회 네트워크 오류: {exc}") from exc

    if key_resp.status_code != 200:
        hint = ""
        if key_resp.status_code == 401:
            hint = " — GH_PAT_SECRETS_WRITE 가 미등록·만료되었거나 값이 손상되었습니다."
        elif key_resp.status_code == 403:
            hint = " — PAT 에 Secrets: Read and write 권한이 없습니다."
        elif key_resp.status_code == 404:
            hint = " — PAT 의 Repository access 에 이 레포가 포함되지 않았습니다."
        raise SecretPersistError(
            f"공개키 조회 실패 {key_resp.status_code}{hint}: {key_resp.text[:200]}"
        )

    key_body = key_resp.json()
    encrypted = _encrypt_for_repo(key_body["key"], token_value)

    try:
        put_resp = requests.put(
            f"{GITHUB_API}/repos/{repo}/actions/secrets/{secret_name}",
            headers=headers,
            json={"encrypted_value": encrypted, "key_id": key_body["key_id"]},
            timeout=config.HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        raise SecretPersistError(f"Secret 갱신 네트워크 오류: {exc}") from exc

    if put_resp.status_code not in (201, 204):
        raise SecretPersistError(f"Secret 갱신 실패 {put_resp.status_code}: {put_resp.text[:200]}")

    log.info("Secret %s 갱신 완료", secret_name)

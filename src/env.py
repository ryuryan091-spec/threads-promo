"""환경변수 로딩 및 필수값 검증.

DB를 두지 않으므로 상태를 가진 값은 GitHub Secrets 하나뿐이다.
(THREADS_LONG_LIVED_TOKEN — 매 실행 시 갱신 후 덮어쓴다)
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class MissingEnvError(RuntimeError):
    """필수 환경변수 누락."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingEnvError(f"필수 환경변수 누락: {name}")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    """실행에 필요한 설정 일체."""

    threads_app_id: str
    threads_app_secret: str
    threads_user_id: str
    threads_token: str

    # Secret 덮어쓰기용. 없으면 갱신 결과를 영속화하지 못하고
    # 60일 뒤 토큰이 만료되므로 경고 대상이다.
    gh_pat: str
    gh_repo: str  # "owner/repo"

    telegram_bot_token: str
    telegram_chat_id: str

    dry_run: bool

    @property
    def can_persist_token(self) -> bool:
        return bool(self.gh_pat and self.gh_repo)

    @property
    def can_notify(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_settings() -> Settings:
    dry_run_raw = _optional("DRY_RUN", "true").lower()
    return Settings(
        threads_app_id=_require("THREADS_APP_ID"),
        threads_app_secret=_require("THREADS_APP_SECRET"),
        threads_user_id=_require("THREADS_USER_ID"),
        threads_token=_require("THREADS_LONG_LIVED_TOKEN"),
        gh_pat=_optional("GH_PAT_SECRETS_WRITE"),
        gh_repo=_optional("GITHUB_REPOSITORY"),
        telegram_bot_token=_optional("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_optional("TELEGRAM_ALERT_CHAT_ID"),
        dry_run=dry_run_raw not in ("false", "0", "no"),
    )

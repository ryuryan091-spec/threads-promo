"""Threads 동영상용 공개 URL 준비 (v1.7.0 숏폼).

Threads API 는 video_url 을 직접 내려받는다(파일 업로드 엔드포인트가 없다).
레포의 고아(orphan) 브랜치 SHORTS_MEDIA_BRANCH 에 그날 영상만 강제 푸시하고
raw.githubusercontent.com URL 을 쓴다. 매번 덮어쓰므로 브랜치에 이력이 쌓이지 않는다.

주의(검증 과제 V3): raw URL 의 Content-Type 과 Threads 컨테이너 처리 성공 여부는
실제 게시로 확인해야 한다. verify() 가 응답 상태·Content-Type 을 로그로 남긴다.
publish job 은 GITHUB_TOKEN 에 contents: write 권한이 있어야 한다.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

from . import config
from .redact import redact

VERSION = "1.0.0"

log = logging.getLogger(__name__)


class MediaHostError(RuntimeError):
    """공개 URL 을 준비하지 못했다."""


def raw_url(repo: str, name: str, branch: str = config.SHORTS_MEDIA_BRANCH) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{name}"


def _git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise MediaHostError(f"git {args[0]} 실패: {redact(proc.stderr)[-400:]}")


def publish_file(video: Path, name: str, *, repo: str | None = None, token: str | None = None) -> str:
    """video 를 고아 브랜치에 name 으로 강제 푸시하고 raw URL 을 돌려준다."""
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = token or os.environ.get("GITHUB_TOKEN", "").strip()
    if not repo or not token:
        raise MediaHostError("GITHUB_REPOSITORY / GITHUB_TOKEN 이 없습니다")
    work = Path(tempfile.mkdtemp(prefix="shorts-media-"))
    try:
        shutil.copyfile(video, work / name)
        _git(["init", "-q", "-b", config.SHORTS_MEDIA_BRANCH], work)
        _git(["-c", "user.name=threads-promo-bot", "-c", "user.email=actions@users.noreply.github.com",
              "add", name], work)
        _git(["-c", "user.name=threads-promo-bot", "-c", "user.email=actions@users.noreply.github.com",
              "commit", "-q", "-m", f"shorts media {name}"], work)
        remote = f"https://x-access-token:{token}@github.com/{repo}.git"
        _git(["push", "-q", "--force", remote, f"HEAD:{config.SHORTS_MEDIA_BRANCH}"], work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    url = raw_url(repo, name)
    log.info("공개 URL 준비 %s", url)
    return url


def verify(url: str) -> str:
    """URL 응답 확인. 200 이 아니면 MediaHostError. 반환은 Content-Type(로그·V3 검증용)."""
    try:
        resp = requests.head(url, timeout=config.HTTP_TIMEOUT_SEC, allow_redirects=True)
    except requests.RequestException as exc:
        raise MediaHostError(f"공개 URL 확인 실패: {redact(str(exc))}") from exc
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    log.info("공개 URL 응답 status=%s Content-Type=%s Content-Length=%s", resp.status_code,
             content_type or "-", resp.headers.get("Content-Length", "-"))
    if resp.status_code != 200:
        raise MediaHostError(f"공개 URL 응답 {resp.status_code}: {url}")
    return content_type

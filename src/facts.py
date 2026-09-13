"""실제 사실 수집.

생성 모델에게 "구체적 경험담"을 요구하면서 근거를 주지 않으면,
지어내는 것 외에 방법이 없다. 실제로 확인 가능한 사실만 모아 프롬프트에 넣는다.

수집 가능한 것
  - git 커밋 로그 (레포에 실재)
  - 레포 상태 (자산 개수, 모듈 수)
  - 런타임 상태 (발행 쿼터, 최근 발행 수)

수집 불가능한 것
  - 시장 데이터 (이 레포에 없음)
  - 웹툰 제작 과정 (다른 레포)
  -> 해당 기둥은 구체 진술을 금지한다. facts 로 메우려 하지 않는다.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1.0.0"

log = logging.getLogger(__name__)

GIT_LOG_DAYS = 14
GIT_LOG_MAX = 12
COMMIT_MSG_MAX = 90


@dataclass(frozen=True)
class Facts:
    """프롬프트에 넣을 실제 사실 묶음."""

    commits: list[str] = field(default_factory=list)
    asset_count: int = 0
    module_count: int = 0
    quota_used: int | None = None
    recent_post_count: int | None = None
    episodes: list[str] = field(default_factory=list)

    @property
    def has_evidence(self) -> bool:
        """BUILD 기둥용 근거(커밋)가 있는지."""
        return bool(self.commits)

    @property
    def has_episodes(self) -> bool:
        """STORY 기둥용 근거(회차 기록)가 있는지."""
        return bool(self.episodes)

    def to_episode_block(self) -> str:
        """STORY 기둥에 넣을 회차 기록 블록."""
        if not self.episodes:
            return ""
        lines = ["# 실제로 발행한 회차 기록"]
        lines += [f"- {e}" for e in self.episodes]
        return "\n".join(lines)

    def to_prompt_block(self) -> str:
        """프롬프트에 넣을 문자열. 사실이 없으면 빈 문자열."""
        if not self.has_evidence:
            return ""

        lines = ["# 실제로 있었던 일 (최근 작업 기록)"]
        lines += [f"- {c}" for c in self.commits]

        state: list[str] = []
        if self.asset_count:
            state.append(f"이미지 자산 {self.asset_count}개")
        if self.module_count:
            state.append(f"파이썬 모듈 {self.module_count}개")
        if self.quota_used is not None:
            state.append(f"오늘 발행 {self.quota_used}건")
        if state:
            lines.append(f"- 현재 상태: {', '.join(state)}")

        return "\n".join(lines)


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603 — 인자 고정, 사용자 입력 없음
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git 실행 실패: %s", exc)
        return ""

    if result.returncode != 0:
        log.warning("git %s 실패: %s", args[0], result.stderr.strip()[:200])
        return ""
    return result.stdout


def collect_commits(repo_root: Path) -> list[str]:
    """최근 커밋 메시지를 모은다.

    Actions 의 checkout 은 기본 fetch-depth 가 1 이라 로그가 비어 있을 수 있다.
    워크플로우에서 fetch-depth 를 늘려야 사실이 수집된다.
    비어 있어도 실패로 다루지 않는다. 근거가 없으면 없는 대로 쓴다.
    """
    raw = _run_git(
        [
            "log",
            f"--since={GIT_LOG_DAYS}.days",
            f"--max-count={GIT_LOG_MAX}",
            "--pretty=format:%s",
            "--no-merges",
        ],
        repo_root,
    )
    if not raw.strip():
        log.info("커밋 기록 없음 — 사실 주입 없이 진행합니다.")
        return []

    seen: set[str] = set()
    commits: list[str] = []
    for line in raw.splitlines():
        message = line.strip()
        if not message or message in seen:
            continue
        seen.add(message)
        commits.append(message[:COMMIT_MSG_MAX])
    return commits


def collect(
    repo_root: Path,
    assets_dir: Path,
    *,
    quota_used: int | None = None,
    recent_post_count: int | None = None,
    episodes: list[str] | None = None,
) -> Facts:
    """사실을 모은다. 어떤 단계가 실패해도 나머지는 유지한다."""
    commits = collect_commits(repo_root)

    asset_count = 0
    try:
        asset_count = len(
            [p for p in assets_dir.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
        )
    except OSError:
        pass

    module_count = 0
    try:
        module_count = len(list((repo_root / "src").glob("*.py")))
    except OSError:
        pass

    facts = Facts(
        commits=commits,
        asset_count=asset_count,
        module_count=module_count,
        quota_used=quota_used,
        recent_post_count=recent_post_count,
        episodes=episodes or [],
    )
    log.info(
        "사실 수집 — 커밋 %d건, 회차 %d건, 자산 %d개, 모듈 %d개",
        len(facts.commits), len(facts.episodes),
        facts.asset_count, facts.module_count,
    )
    return facts

"""사실 수집 및 주입 테스트.

배경: 근거 없이 "구체적 경험담"을 요구해 모델이 하지 않은 작업을
      했다고 쓴 사고가 실제로 발생했다. 그 재발을 막는 테스트다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from src import ai_writer, facts


def _git_repo(tmp_path: Path, messages: list[str]) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    for m in messages:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", m], cwd=tmp_path, check=True
        )
    return tmp_path


class TestCommitCollection:
    def test_collects_messages(self, tmp_path: Path):
        repo = _git_repo(tmp_path, ["첫 작업", "둘째 작업"])
        got = facts.collect_commits(repo)
        assert "첫 작업" in got
        assert "둘째 작업" in got

    def test_deduplicates(self, tmp_path: Path):
        repo = _git_repo(tmp_path, ["같은 작업", "같은 작업", "다른 작업"])
        got = facts.collect_commits(repo)
        assert got.count("같은 작업") == 1

    def test_respects_max(self, tmp_path: Path):
        repo = _git_repo(tmp_path, [f"작업 {i}" for i in range(30)])
        assert len(facts.collect_commits(repo)) <= facts.GIT_LOG_MAX

    def test_no_repo_returns_empty(self, tmp_path: Path):
        assert facts.collect_commits(tmp_path / "nope") == []

    def test_git_failure_is_swallowed(self, tmp_path: Path):
        with mock.patch.object(
            facts.subprocess, "run", side_effect=OSError("git 없음")
        ):
            assert facts.collect_commits(tmp_path) == []


class TestFactsBlock:
    def test_empty_when_no_commits(self):
        assert facts.Facts().to_prompt_block() == ""
        assert not facts.Facts().has_evidence

    def test_includes_commits_and_state(self):
        f = facts.Facts(commits=["작업 하나"], asset_count=3, module_count=13)
        block = f.to_prompt_block()
        assert "작업 하나" in block
        assert "이미지 자산 3개" in block
        assert "파이썬 모듈 13개" in block

    def test_collect_survives_missing_assets(self, tmp_path: Path):
        repo = _git_repo(tmp_path, ["작업"])
        got = facts.collect(repo, tmp_path / "nope")
        assert got.commits
        assert got.asset_count == 0


class TestEvidencePolicy:
    """근거가 없는 기둥에서 구체 진술을 금지하는지."""

    def test_story_is_the_evidence_pillar(self):
        """BUILD 제거 후 근거 보유 기둥은 STORY 뿐이다."""
        assert ai_writer.PILLARS["STORY"].evidence_available

    def test_build_pillar_removed(self):
        """BUILD 는 유입 정합성 문제로 제거되었다."""
        assert "BUILD" not in ai_writer.PILLARS
        assert "BUILD" not in ai_writer.PILLAR_ROTATION
        assert not ai_writer.uses_commit_evidence()

    def test_market_promo_have_none(self):
        """근거 소스가 없는 기둥은 구체 진술을 금지해야 한다."""
        for key in ("MARKET", "PROMO"):
            assert not ai_writer.PILLARS[key].evidence_available, key

    def test_evidence_pillar_gets_facts(self):
        block = facts.Facts(commits=["폴백 구현"]).to_prompt_block()
        prompt = ai_writer._build_user_prompt(
            ai_writer.PILLARS["STORY"], "소재", [], block
        )
        assert "폴백 구현" in prompt
        assert "여기 없는 작업" in prompt

    def test_non_evidence_pillar_gets_warning(self):
        block = facts.Facts(commits=["폴백 구현"]).to_prompt_block()
        prompt = ai_writer._build_user_prompt(
            ai_writer.PILLARS["MARKET"], "소재", [], block
        )
        assert "폴백 구현" not in prompt
        assert "근거 없음" in prompt

    def test_evidence_pillar_without_facts_gets_warning(self):
        prompt = ai_writer._build_user_prompt(
            ai_writer.PILLARS["STORY"], "소재", [], ""
        )
        assert "근거 없음" in prompt

    def test_rotation_balance(self):
        """PROMO 25% 상한 유지. 홍보를 늘리면 도달이 떨어진다."""
        import collections

        c = collections.Counter(ai_writer.PILLAR_ROTATION)
        total = len(ai_writer.PILLAR_ROTATION)
        assert c["PROMO"] / total <= 0.25
        assert c["STORY"] / total >= 0.3

    def test_no_adjacent_duplicates(self):
        r = ai_writer.PILLAR_ROTATION
        for i in range(len(r)):
            assert r[i] != r[(i + 1) % len(r)]


class TestFactConstraintsInPrompt:
    """환각 방지 문구가 프롬프트에 실제로 들어 있는지."""

    def test_system_prompt_forbids_fabrication(self):
        for phrase in ("실제로 하지 않은 작업", "지어낸 수치", "근거를 넘어서"):
            assert phrase in ai_writer.SYSTEM_PROMPT, phrase

    def test_fact_constraint_is_top_priority(self):
        assert "다른 모든 지시보다 우선" in ai_writer.SYSTEM_PROMPT

    def test_reply_prompt_forbids_reporting_undone_work(self):
        from src import reply_engine

        prompt = reply_engine.REPLY_SYSTEM_PROMPT
        assert "하지 않은 작업의 결과를 보고하지 않습니다" in prompt
        assert "아직 안 해봤습니다" in prompt

    def test_reply_prompt_forbids_fake_aggregation(self):
        from src import reply_engine

        assert "모아보니 대부분" in reply_engine.REPLY_SYSTEM_PROMPT

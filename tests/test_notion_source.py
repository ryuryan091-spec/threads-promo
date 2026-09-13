"""Notion Tracker DB 연동 테스트.

핵심 관심사는 두 가지다.
  1. 스키마를 모르는 상태에서도 안전하게 동작하는가
  2. 시장 수치·민감 정보가 프롬프트로 새지 않는가
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
import requests

from src import notion_source


def _prop(ptype: str, value):
    if ptype == "title":
        return {"type": "title", "title": [{"plain_text": value}]}
    if ptype == "rich_text":
        return {"type": "rich_text", "rich_text": [{"plain_text": value}]}
    if ptype == "select":
        return {"type": "select", "select": {"name": value}}
    if ptype == "status":
        return {"type": "status", "status": {"name": value}}
    if ptype == "multi_select":
        return {"type": "multi_select",
                "multi_select": [{"name": v} for v in value]}
    if ptype == "number":
        return {"type": "number", "number": value}
    return {"type": ptype}


def _resp(status: int, body: dict):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body
    r.text = str(body)
    return r


class TestSanitize:
    """시장 수치가 본문으로 새면 자본시장법 해석 여지가 생긴다."""

    @pytest.mark.parametrize(
        "raw",
        ["10Y 4.57%", "WTI $81.77", "HY OAS 271bp", "나스닥 -1.40%", "F&G 43.5"],
    )
    def test_numbers_removed(self, raw: str):
        got = notion_source.sanitize(raw)
        assert "%" not in got
        assert "$" not in got
        assert "bp" not in got or not any(ch.isdigit() for ch in got)

    @pytest.mark.parametrize("raw", ["Ep61", "EX-10", "Day 16", "16일차"])
    def test_identifiers_kept(self, raw: str):
        assert notion_source.sanitize(raw) == raw

    def test_categorical_kept(self):
        assert notion_source.sanitize("BATTLE") == "BATTLE"
        assert notion_source.sanitize("무승부") == "무승부"

    def test_empty(self):
        assert notion_source.sanitize("   ") == ""


class TestPropertyTypeGate:
    """스키마를 모르므로 타입 기준으로만 기본 채택한다."""

    def test_safe_types_are_taken(self):
        props = {
            "회차": _prop("title", "Ep61"),
            "타입": _prop("select", "BATTLE"),
            "상태": _prop("status", "발행완료"),
            "태그": _prop("multi_select", ["아크", "배틀"]),
        }
        line = notion_source._row_to_line(props, set())
        assert "Ep61" in line
        assert "BATTLE" in line
        assert "발행완료" in line

    def test_number_excluded_by_default(self):
        props = {
            "회차": _prop("title", "Ep61"),
            "VIX": _prop("number", 18.77),
        }
        line = notion_source._row_to_line(props, set())
        assert "18.77" not in line
        assert "Ep61" in line

    def test_rich_text_excluded_by_default(self):
        props = {
            "회차": _prop("title", "Ep61"),
            "메모": _prop("rich_text", "블룸버그 기준 10Y 4.57%"),
        }
        line = notion_source._row_to_line(props, set())
        assert "블룸버그" not in line
        assert "4.57" not in line

    def test_allowlist_opens_excluded_field(self):
        props = {
            "회차": _prop("title", "Ep61"),
            "비고": _prop("rich_text", "저강도 연속"),
        }
        line = notion_source._row_to_line(props, {"비고"})
        assert "저강도 연속" in line

    def test_allowlisted_field_still_sanitized(self):
        """허용목록에 넣어도 수치는 살균된다."""
        props = {"비고": _prop("rich_text", "종가 $81.77 기준")}
        line = notion_source._row_to_line(props, {"비고"})
        assert "$81.77" not in line


class TestFetch:
    def test_no_config_returns_empty(self):
        assert notion_source.fetch_episodes("", "db", 5) == []
        assert notion_source.fetch_episodes("tok", "", 5) == []

    def test_network_error_returns_empty(self):
        with mock.patch.object(
            notion_source.requests, "post",
            side_effect=requests.RequestException("down"),
        ):
            assert notion_source.fetch_episodes("t", "db", 5) == []

    def test_401_returns_empty_not_raise(self):
        with mock.patch.object(
            notion_source.requests, "post", return_value=_resp(401, {})
        ):
            assert notion_source.fetch_episodes("t", "db", 5) == []

    def test_404_returns_empty_not_raise(self):
        with mock.patch.object(
            notion_source.requests, "post", return_value=_resp(404, {})
        ):
            assert notion_source.fetch_episodes("t", "db", 5) == []

    def test_success_returns_lines(self):
        body = {
            "results": [
                {"properties": {
                    "회차": _prop("title", "Ep61"),
                    "타입": _prop("select", "BATTLE"),
                    "VIX": _prop("number", 18.77),
                }}
            ]
        }
        with mock.patch.object(
            notion_source.requests, "post", return_value=_resp(200, body)
        ):
            lines = notion_source.fetch_episodes("t", "db", 5)
        assert len(lines) == 1
        assert "Ep61" in lines[0]
        assert "18.77" not in lines[0]

    def test_page_size_capped(self):
        with mock.patch.object(
            notion_source.requests, "post", return_value=_resp(200, {"results": []})
        ) as post:
            notion_source.fetch_episodes("t", "db", 999)
        assert post.call_args.kwargs["json"]["page_size"] <= 20


class TestPillarWiring:
    def test_story_pillar_now_has_evidence(self):
        from src import ai_writer

        assert ai_writer.PILLARS["STORY"].evidence_available

    def test_story_brief_bans_character_names_and_numbers(self):
        from src import ai_writer

        brief = ai_writer.PILLARS["STORY"].brief
        assert "캐릭터 고유명은 쓰지 않고" in brief
        assert "시장 수치" in brief

    def test_episode_block_format(self):
        from src import facts

        f = facts.Facts(episodes=["Ep61 / 타입=BATTLE"])
        block = f.to_episode_block()
        assert "실제로 발행한 회차 기록" in block
        assert "Ep61" in block
        assert f.has_episodes

    def test_empty_episode_block(self):
        from src import facts

        assert facts.Facts().to_episode_block() == ""


class TestSettingsGate:
    def test_can_fetch_requires_both(self):
        from src import config
        from src.env import load_settings

        base = {
            "THREADS_APP_ID": "1", "THREADS_APP_SECRET": "s",
            "THREADS_LONG_LIVED_TOKEN": "t",
        }
        os.environ.update(base)
        os.environ["NOTION_TOKEN"] = "ntn_x"
        try:
            with mock.patch.object(config, "NOTION_DB_ID", ""):
                assert not load_settings().can_fetch_episodes
            with mock.patch.object(config, "NOTION_DB_ID", "db123"):
                assert load_settings().can_fetch_episodes
        finally:
            os.environ.pop("NOTION_TOKEN", None)


class TestRealSchema:
    """실제 EDT 에피소드 트래커 스키마 기준 회귀 테스트.

    스키마(2026-09-13 확인)
      title  : 에피소드
      select : 에피소드 타입 / 전투 결과 / 메인 히어로 / 활성 빌런 /
               발행 상태 / SNS 발행 / Act Override 사유
      number : 번호 / Arc Day / arc_tension / Battle Balance / 14일 카운터
      date   : 발행일        url : 소스 패키지      rich_text : 특이사항
    """

    ROW = {
        "에피소드": _prop("title", "두 개의 전선, 하나의 선택"),
        "번호": _prop("number", 61),
        "에피소드 타입": _prop("select", "BATTLE"),
        "전투 결과": _prop("select", "Draw"),
        "메인 히어로": _prop("select", "EDT"),
        "활성 빌런": _prop("select", "Debt Titan"),
        "발행 상태": _prop("select", "완료"),
        "Arc Day": _prop("number", 16),
        "arc_tension": _prop("number", 84),
        "Battle Balance": _prop("number", -2),
        "특이사항": _prop("rich_text", "WTI $81.77 / VIX 18.77 기준"),
    }

    def test_internal_metrics_excluded(self):
        """arc_tension·Battle Balance 는 내부 지표다. 외부 노출 금지."""
        line = notion_source._row_to_line(self.ROW, set())
        for banned in ("84", "-2", "16", "61"):
            assert banned not in line

    def test_character_names_blocked_by_default(self):
        """STORY 프롬프트가 고유명 금지를 지시하므로 데이터도 막아야 한다."""
        line = notion_source._row_to_line(self.ROW, set())
        assert "Debt Titan" not in line
        assert "EDT" not in line

    def test_character_names_openable_by_allowlist(self):
        line = notion_source._row_to_line(self.ROW, {"메인 히어로", "활성 빌런"})
        assert "Debt Titan" in line

    def test_market_numbers_in_notes_sanitized(self):
        line = notion_source._row_to_line(self.ROW, {"특이사항"})
        assert "81.77" not in line
        assert "18.77" not in line

    def test_useful_content_survives(self):
        """차단만 하고 쓸 내용이 안 남으면 의미가 없다."""
        line = notion_source._row_to_line(self.ROW, set())
        assert "두 개의 전선" in line
        assert "BATTLE" in line
        assert "Draw" in line

    def test_deny_list_covers_source_url(self):
        assert "소스 패키지" in notion_source.DENY_PROPERTY_NAMES

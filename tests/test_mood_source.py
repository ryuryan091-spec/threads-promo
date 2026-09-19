"""CHAT 근거 수집(mood_source) + lint_chat 테스트."""

from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest
import requests

from src import config, content, mood_source

NOW = dt.datetime(2026, 9, 21, 0, 30, tzinfo=dt.UTC)   # KST 09:30


def _rss(items: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<item><title>{t}</title><pubDate>{d}</pubDate></item>" for t, d in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


FRESH = "Mon, 21 Sep 2026 00:00:00 GMT"
STALE = "Fri, 18 Sep 2026 00:00:00 GMT"


# ---------------------------------------------------------------------------
# 살균 · 테마
# ---------------------------------------------------------------------------


class TestSanitize:
    @pytest.mark.parametrize("raw,banned", [
        ("[속보] 연준 금리 0.25%p 인하 - 연합뉴스", ["0", "25", "%", "연합뉴스", "속보"]),
        ("유가 $85 돌파… 환율 1,390원", ["85", "$", "1,390", "원 "]),
        ("국채 금리 4.2% 상승 https://x.com/a", ["4.2", "https", "%"]),
    ])
    def test_removes_numbers_currency_source(self, raw, banned):
        out = mood_source.sanitize_headline(raw)
        for b in banned:
            assert b not in out, (b, out)

    def test_keeps_theme_words(self):
        out = mood_source.sanitize_headline("[종합] 연준 금리 동결 - 매체")
        assert "연준" in out and "금리" in out

    def test_extract_only_allowlist(self):
        themes = mood_source.extract_themes(["엔비디아 실적 발표", "연준 금리 동결", "금리 부담"])
        assert "엔비디아" not in themes
        assert themes[0] == "금리"
        assert set(themes) <= set(config.CHAT_THEME_ALLOWLIST)

    def test_extract_prefers_longer_overlap(self):
        themes = mood_source.extract_themes(["실적 시즌 개막", "실적 시즌 긴장"])
        assert "실적 시즌" in themes
        assert "실적" not in themes

    def test_extract_limit(self):
        texts = [" ".join(config.CHAT_THEME_ALLOWLIST)]
        assert len(mood_source.extract_themes(texts)) <= mood_source.MAX_THEMES

    def test_no_single_char_gold_false_positive(self):
        """'금' 단독 용어는 금리·금통위에 오탐하므로 허용목록에 두지 않는다."""
        assert "금" not in config.CHAT_THEME_ALLOWLIST


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


class TestRss:
    def test_parse_filters_stale(self):
        titles = mood_source.parse_rss(_rss([("연준 금리", FRESH), ("유가", STALE)]), NOW)
        assert titles == ["연준 금리"]

    def test_parse_drops_missing_date(self):
        titles = mood_source.parse_rss(_rss([("연준 금리", "")]), NOW)
        assert titles == []

    def test_parse_broken_xml(self):
        with pytest.raises(mood_source.MoodSourceError):
            mood_source.parse_rss("<rss><item>", NOW)

    def test_parse_limit(self):
        xml = _rss([(f"금리 {i}", FRESH) for i in range(30)])
        assert len(mood_source.parse_rss(xml, NOW)) == config.MOOD_RSS_MAX_ITEMS

    def test_fetch_requires_urls(self):
        with pytest.raises(mood_source.MoodSourceError, match="미설정"):
            mood_source.fetch_rss((), NOW)

    def test_fetch_success(self):
        resp = mock.Mock(status_code=200, text=_rss([("연준 금리 동결 - 매체", FRESH),
                                                       ("유가 급등", FRESH)]))
        with mock.patch.object(mood_source.requests, "get", return_value=resp):
            mood = mood_source.fetch_rss(("https://rss.example/a",), NOW)
        assert mood.source == "rss"
        assert "금리" in mood.themes and "유가" in mood.themes
        assert "매체" not in mood.to_prompt_block()

    def test_fetch_http_error(self):
        resp = mock.Mock(status_code=403, text="")
        with (
            mock.patch.object(mood_source.requests, "get", return_value=resp),
            pytest.raises(mood_source.MoodSourceError),
        ):
            mood_source.fetch_rss(("https://rss.example/a",), NOW)

    def test_fetch_network_error(self):
        with (
            mock.patch.object(mood_source.requests, "get",
                              side_effect=requests.ConnectionError("x")),
            pytest.raises(mood_source.MoodSourceError),
        ):
            mood_source.fetch_rss(("https://rss.example/a",), NOW)

    def test_fetch_no_allowlisted_theme(self):
        resp = mock.Mock(status_code=200, text=_rss([("연예 소식", FRESH)]))
        with (
            mock.patch.object(mood_source.requests, "get", return_value=resp),
            pytest.raises(mood_source.MoodSourceError, match="테마 0건"),
        ):
            mood_source.fetch_rss(("https://rss.example/a",), NOW)


# ---------------------------------------------------------------------------
# 웹 검색
# ---------------------------------------------------------------------------


def _web_body(text: str, *, searched: bool = True, stop: str = "end_turn") -> dict:
    blocks: list[dict] = []
    if searched:
        blocks += [
            {"type": "server_tool_use", "id": "s1", "name": "web_search",
             "input": {"query": "시장"}},
            {"type": "web_search_tool_result", "tool_use_id": "s1",
             "content": [{"type": "web_search_result", "url": "https://a", "title": "t",
                          "encrypted_content": "x"}]},
        ]
    blocks.append({"type": "text", "text": text})
    return {"content": blocks, "stop_reason": stop}


class TestWeb:
    def test_parse_valid(self):
        mood = mood_source.parse_web_response(
            _web_body('{"themes": ["금리", "유가", "엔비디아"], "mood": "관망"}'))
        assert mood.themes == ("금리", "유가")
        assert mood.mood_word == "관망"

    def test_parse_requires_search_result(self):
        with pytest.raises(mood_source.MoodSourceError, match="검색 결과 없음"):
            mood_source.parse_web_response(
                _web_body('{"themes": ["금리"], "mood": "관망"}', searched=False))

    def test_parse_search_error_block(self):
        body = {"content": [{"type": "web_search_tool_result", "tool_use_id": "s",
                             "content": {"type": "web_search_tool_result_error",
                                         "error_code": "unavailable"}}],
                "stop_reason": "end_turn"}
        with pytest.raises(mood_source.MoodSourceError, match="unavailable"):
            mood_source.parse_web_response(body)

    def test_parse_unknown_mood_dropped(self):
        mood = mood_source.parse_web_response(
            _web_body('{"themes": ["금리"], "mood": "폭락 공포"}'))
        assert mood.mood_word == ""

    def test_parse_no_allowed_theme(self):
        with pytest.raises(mood_source.MoodSourceError):
            mood_source.parse_web_response(_web_body('{"themes": ["애플"], "mood": "관망"}'))

    def test_payload_uses_basic_tool_version(self):
        payload = mood_source._web_payload(NOW.date(), [])
        tool = payload["tools"][0]
        assert tool["type"] == "web_search_20250305"
        assert tool["name"] == "web_search"
        assert tool["max_uses"] == config.MOOD_WEB_MAX_USES

    def test_fetch_requires_key(self):
        with pytest.raises(mood_source.MoodSourceError):
            mood_source.fetch_web("", NOW.date())

    def test_fetch_resumes_pause_turn(self):
        paused = _web_body("", stop="pause_turn")
        final = _web_body('{"themes": ["환율"], "mood": "경계"}')
        with mock.patch.object(mood_source, "_post_messages",
                               side_effect=[paused, final]) as post:
            mood = mood_source.fetch_web("sk", NOW.date())
        assert mood.themes == ("환율",)
        second = post.call_args_list[1].args[1]["messages"]
        assert second[-1]["role"] == "assistant"

    def test_post_http_error(self):
        resp = mock.Mock(status_code=400, text="web search disabled")
        with (
            mock.patch.object(mood_source.requests, "post", return_value=resp),
            pytest.raises(mood_source.MoodSourceError, match="400"),
        ):
            mood_source._post_messages("sk", {})


# ---------------------------------------------------------------------------
# collect 폴백
# ---------------------------------------------------------------------------


class TestCollect:
    def test_first_success(self):
        with mock.patch.object(mood_source, "fetch_web",
                               return_value=mood_source.Mood("web", ("금리",))):
            mood = mood_source.collect("web", api_key="k", today=NOW.date(), now=NOW)
        assert mood.source == "web"

    def test_fallback_to_other(self):
        with (
            mock.patch.object(mood_source, "fetch_rss",
                              side_effect=mood_source.MoodSourceError("x")),
            mock.patch.object(mood_source, "fetch_web",
                              return_value=mood_source.Mood("web", ("유가",))),
        ):
            mood = mood_source.collect("rss", api_key="k", today=NOW.date(), now=NOW)
        assert mood.source == "web"

    def test_both_fail_returns_none(self):
        with (
            mock.patch.object(mood_source, "fetch_rss", side_effect=RuntimeError("x")),
            mock.patch.object(mood_source, "fetch_web",
                              side_effect=mood_source.MoodSourceError("y")),
        ):
            mood = mood_source.collect("web", api_key="k", today=NOW.date(), now=NOW)
        assert mood.source == "none"
        assert mood.to_prompt_block() == ""

    def test_none_mode(self):
        assert mood_source.collect("none", api_key="k", today=NOW.date(),
                                   now=NOW).source == "none"


# ---------------------------------------------------------------------------
# lint_chat
# ---------------------------------------------------------------------------


class TestLintChat:
    def test_passes_clean(self):
        content.lint_chat("오늘 아침은 다들 연준 얘기만 하네요.\n이런 날 뭐부터 확인하세요?")

    def test_passes_allowlisted_english(self):
        content.lint_chat("FOMC 앞둔 아침이라 조용하네요. 다들 뭐 보세요?")

    @pytest.mark.parametrize("text,msg", [
        ("금리 3번 인하 얘기네요?", "숫자"),
        ("금리 세 번 인하, 반등할 것 같나요?", "전망"),
        ("엔비디아 얘기뿐이네요?", "기업"),
        ("파월 발언 기다리는 아침이네요?", "기업"),
        ("NVDA 얘기뿐이네요?", "영문"),
        ("오늘은 매수 타이밍일까요?", "금칙어"),
    ])
    def test_blocks(self, text, msg):
        with pytest.raises(content.ContentPolicyError, match=msg):
            content.lint_chat(text)

    def test_length(self):
        with pytest.raises(content.ContentPolicyError, match="CHAT 본문"):
            content.lint_chat("가" * (config.CHAT_TEXT_MAX_LEN + 1))


class TestLintChatFalsePositives:
    """v1.1.1: Actions dry_run 에서 확인된 일반어 오차단 수정 검증."""

    @pytest.mark.parametrize("text", [
        "제가 매일 만드는 건 전자 쪽입니다. 어느 쪽이세요?",   # 전자(前者) — 실측 문장
        "스터디 그룹에서도 금리 얘기뿐이네요?",
        "증권 앱 열기 전에 뭐부터 보세요?",
        "어느 증권사가 아니라 흐름이 궁금해요?",
        "자산운용 얘기가 많은 아침이네요?",
        "메타버스 얘기는 요즘 조용하네요?",
        "메타인지가 필요한 아침이네요?",
        "알파벳 순서로 정리해 봤어요. 어떠세요?",
        "애플리케이션 알림부터 끄셨나요?",
    ])
    def test_common_words_pass(self, text):
        content.lint_chat(text)

    @pytest.mark.parametrize("text", [
        "삼성전자 얘기뿐이네요?",
        "미래에셋증권 얘기네요?",
        "금융그룹 발표가 있었네요?",
        "메타가 또 화제네요?",
        "애플이 발표했네요?",
        "메타 얘기뿐이네요?",
    ])
    def test_entities_blocked(self, text):
        with pytest.raises(content.ContentPolicyError, match="기업"):
            content.lint_chat(text)

    def test_real_dry_run_output_passes(self):
        """2026-09-19 Actions dry_run 실제 생성문."""
        content.lint_chat(
            "반도체 얘기 나오니까 다들 눈빛이 바뀌는 게 느껴지네요. 저는 그럴 때일수록 일부러 "
            "한 박자 늦게 반응하려고 합니다. 오늘 분위기, 경계하는 쪽이세요 아니면 그냥 지켜보는 쪽이세요"
        )

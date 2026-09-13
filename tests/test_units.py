"""저수준 모듈 전수 테스트.

token_manager / notifier / threads_client._request / ai_writer / run_reply
E2E 로 닿지 않는 오류 경로를 개별 검증한다.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
import requests

from src import ai_writer, notifier, token_manager
from src.threads_client import ThreadsApiError

# ---------------------------------------------------------------------------
# token_manager
# ---------------------------------------------------------------------------


def _resp(status: int, body: dict | str) -> mock.Mock:
    r = mock.Mock()
    r.status_code = status
    r.text = body if isinstance(body, str) else json.dumps(body)
    r.json.return_value = body if isinstance(body, dict) else {}
    r.content = r.text.encode()
    return r


class TestTokenRefresh:
    def test_success_returns_new_token(self):
        with mock.patch.object(
            token_manager.requests, "get",
            return_value=_resp(200, {"access_token": "THAA_new", "expires_in": 5183944}),
        ):
            assert token_manager.refresh_long_lived_token("THAA_old") == "THAA_new"

    def test_http_error_raises(self):
        with (
            mock.patch.object(
                token_manager.requests, "get",
                return_value=_resp(400, {"error": {"code": 190}}),
            ),
            pytest.raises(token_manager.TokenRefreshError),
        ):
            token_manager.refresh_long_lived_token("bad")

    def test_network_error_raises(self):
        with (
            mock.patch.object(
                token_manager.requests, "get",
                side_effect=requests.RequestException("timeout"),
            ),
            pytest.raises(token_manager.TokenRefreshError),
        ):
            token_manager.refresh_long_lived_token("t")

    def test_missing_field_raises(self):
        with (
            mock.patch.object(
                token_manager.requests, "get", return_value=_resp(200, {"ok": True})
            ),
            pytest.raises(token_manager.TokenRefreshError),
        ):
            token_manager.refresh_long_lived_token("t")


class TestSecretPersist:
    """401 Bad credentials 재발 시 원인이 드러나야 한다."""

    def test_bad_credentials_raises(self):
        with (
            mock.patch.object(
                token_manager.requests, "get",
                return_value=_resp(401, {"message": "Bad credentials"}),
            ),
            pytest.raises(token_manager.SecretPersistError, match="401"),
        ):
            token_manager.persist_token_to_secret("owner/repo", "bad_pat", "v")

    def test_forbidden_raises(self):
        with (
            mock.patch.object(
                token_manager.requests, "get",
                return_value=_resp(403, {"message": "Resource not accessible"}),
            ),
            pytest.raises(token_manager.SecretPersistError),
        ):
            token_manager.persist_token_to_secret("owner/repo", "pat", "v")


# ---------------------------------------------------------------------------
# notifier — 실패해도 본류를 막지 않아야 한다
# ---------------------------------------------------------------------------


class TestNotifier:
    def test_skips_when_unconfigured(self):
        with mock.patch.object(notifier.requests, "post") as post:
            notifier.send("", "", "메시지")
        assert not post.called

    def test_sends_when_configured(self):
        with mock.patch.object(notifier.requests, "post") as post:
            notifier.send("bot", "chat", "메시지")
        assert post.called

    def test_network_failure_is_swallowed(self):
        with mock.patch.object(
            notifier.requests, "post", side_effect=requests.RequestException("down")
        ):
            notifier.send("bot", "chat", "메시지")  # 예외가 새면 안 된다


# ---------------------------------------------------------------------------
# threads_client._request — 오류 파싱
# ---------------------------------------------------------------------------


class TestRequestErrorParsing:
    def test_auth_error_detected(self):
        err = ThreadsApiError(400, '{"error":{"code":190}}', code=190)
        assert err.is_auth_error

    def test_non_auth_error(self):
        err = ThreadsApiError(400, '{"error":{"code":36001}}', code=36001)
        assert not err.is_auth_error

    def test_message_contains_status_and_code(self):
        err = ThreadsApiError(400, "payload", code=24)
        assert "400" in str(err)
        assert "24" in str(err)


# ---------------------------------------------------------------------------
# ai_writer — 응답 파싱 전수
# ---------------------------------------------------------------------------


class TestAiWriterParsing:
    @pytest.mark.parametrize(
        "raw",
        [
            '{"text": "본문입니다"}',
            '```json\n{"text": "본문입니다"}\n```',
            '```\n{"text": "본문입니다"}\n```',
            '설명이 앞에 붙음 {"text": "본문입니다"} 뒤에도 붙음',
        ],
    )
    def test_json_variants(self, raw: str):
        assert ai_writer._extract_json(raw)["text"] == "본문입니다"

    def test_non_json_raises(self):
        with pytest.raises(ai_writer.AiWriterError):
            ai_writer._extract_json("JSON 이 아닙니다")

    def test_api_error_raises(self):
        with (
            mock.patch.object(
                ai_writer.requests, "post", return_value=_resp(401, {"e": 1})
            ),
            pytest.raises(ai_writer.AiWriterError, match="401"),
        ):
            ai_writer.generate("key", "STORY", "소재")

    def test_network_error_raises(self):
        with (
            mock.patch.object(
                ai_writer.requests, "post",
                side_effect=requests.RequestException("down"),
            ),
            pytest.raises(ai_writer.AiWriterError),
        ):
            ai_writer.generate("key", "STORY", "소재")

    def test_empty_text_raises(self):
        body = {"content": [{"type": "text", "text": '{"text": ""}'}]}
        with (
            mock.patch.object(
                ai_writer.requests, "post", return_value=_resp(200, body)
            ),
            pytest.raises(ai_writer.AiWriterError),
        ):
            ai_writer.generate("key", "STORY", "소재")

    def test_unknown_pillar_raises(self):
        with pytest.raises(ai_writer.AiWriterError, match="기둥"):
            ai_writer.generate("key", "NOPE", "소재")

    def test_success_returns_text(self):
        body = {"content": [{"type": "text", "text": '{"text": "생성된 본문"}'}]}
        with mock.patch.object(
            ai_writer.requests, "post", return_value=_resp(200, body)
        ):
            assert ai_writer.generate("key", "STORY", "소재") == "생성된 본문"

    def test_recent_texts_in_prompt(self):
        pillar = ai_writer.PILLARS["STORY"]
        prompt = ai_writer._build_user_prompt(pillar, "소재", ["어제 글", "그제 글"])
        assert "어제 글" in prompt
        assert "겹치지 않게" in prompt

    def test_all_pillars_have_seeds(self):
        for key, pillar in ai_writer.PILLARS.items():
            assert pillar.seeds, key
            assert pillar.brief, key

    def test_rotation_covers_all_pillars(self):
        assert set(ai_writer.PILLAR_ROTATION) == set(ai_writer.PILLARS)

    def test_seed_is_deterministic(self):
        a = ai_writer.pick_seed("STORY", 100)
        b = ai_writer.pick_seed("STORY", 100)
        assert a == b
        assert a in ai_writer.PILLARS["STORY"].seeds


# ---------------------------------------------------------------------------
# 프롬프트 안전장치
# ---------------------------------------------------------------------------


class TestPromptSafety:
    def test_system_prompt_bans_investment_terms(self):
        for term in ("매수", "매도", "목표가", "종목추천"):
            assert term in ai_writer.SYSTEM_PROMPT

    def test_system_prompt_bans_engagement_bait(self):
        assert "좋아요 누르면" in ai_writer.SYSTEM_PROMPT

    def test_system_prompt_bans_links(self):
        assert "링크" in ai_writer.SYSTEM_PROMPT

    def test_reply_prompt_bans_promotion(self):
        from src import reply_engine

        assert "구독" in reply_engine.REPLY_SYSTEM_PROMPT


class TestNetworkResilience:
    """네트워크 오류가 전용 예외로 감싸져야 폴백이 동작한다."""

    def test_refresh_network_error_wrapped(self):
        with (
            mock.patch.object(
                token_manager.requests, "get",
                side_effect=requests.RequestException("down"),
            ),
            pytest.raises(token_manager.TokenRefreshError, match="네트워크"),
        ):
            token_manager.refresh_long_lived_token("t")

    def test_persist_network_error_wrapped(self):
        with (
            mock.patch.object(
                token_manager.requests, "get",
                side_effect=requests.RequestException("down"),
            ),
            pytest.raises(token_manager.SecretPersistError, match="네트워크"),
        ):
            token_manager.persist_token_to_secret("owner/repo", "pat", "v")

    def test_401_hint_mentions_pat(self):
        with mock.patch.object(
            token_manager.requests, "get",
            return_value=_resp(401, {"message": "Bad credentials"}),
        ):
            try:
                token_manager.persist_token_to_secret("owner/repo", "pat", "v")
            except token_manager.SecretPersistError as exc:
                assert "GH_PAT_SECRETS_WRITE" in str(exc)
            else:
                raise AssertionError("예외가 발생하지 않았습니다")

    def test_403_hint_mentions_permission(self):
        with mock.patch.object(
            token_manager.requests, "get",
            return_value=_resp(403, {"message": "forbidden"}),
        ):
            try:
                token_manager.persist_token_to_secret("owner/repo", "pat", "v")
            except token_manager.SecretPersistError as exc:
                assert "Read and write" in str(exc)
            else:
                raise AssertionError("예외가 발생하지 않았습니다")


class TestJsonRobustness:
    """실제 발행에서 터진 케이스. 본문에 줄바꿈이 드는 것은 정상 동작이다."""

    def test_literal_newline_in_string(self):
        raw = '{"text": "첫 줄입니다.\n두 번째 줄입니다."}'
        assert "\n" in ai_writer._extract_json(raw)["text"]

    def test_literal_tab_in_string(self):
        raw = '{"text": "앞\t뒤"}'
        assert ai_writer._extract_json(raw)["text"]

    def test_code_fence_with_newline(self):
        raw = '```json\n{"text": "첫 줄\n둘째 줄"}\n```'
        assert "둘째 줄" in ai_writer._extract_json(raw)["text"]

    def test_unescaped_quotes_recovered(self):
        raw = '{"text": "그가 "그렇다"고 했습니다.\n정말?"}'
        assert "그렇다" in ai_writer._extract_json(raw)["text"]

    def test_unrecoverable_raises_ai_writer_error(self):
        with pytest.raises(ai_writer.AiWriterError):
            ai_writer._extract_json('{"txt": ')

    def test_parse_failure_is_wrapped_not_raw_json_error(self):
        """JSONDecodeError 가 그대로 새면 폴백이 동작하지 않는다."""
        import json as _json

        try:
            ai_writer._extract_json("{ not json at all ")
        except _json.JSONDecodeError:
            raise AssertionError("JSONDecodeError 가 그대로 전파되었습니다") from None
        except ai_writer.AiWriterError:
            pass

    def test_generate_falls_back_on_broken_json(self):
        """생성 결과가 깨져도 AiWriterError 로 나와 재시도·폴백이 가능해야 한다."""
        body = {"content": [{"type": "text", "text": "완전히 깨진 응답"}]}
        with (
            mock.patch.object(
                ai_writer.requests, "post", return_value=_resp(200, body)
            ),
            pytest.raises(ai_writer.AiWriterError),
        ):
            ai_writer.generate("key", "STORY", "소재")


class TestExpiryAssessment:
    """저장 실패 상태에서 기존 토큰 잔여 수명 판정."""

    TODAY = __import__("datetime").date(2026, 9, 13)

    @pytest.mark.parametrize(
        ("issued", "level", "alert"),
        [
            ("2026-09-13", "ok", False),
            ("2026-08-09", "ok", False),
            ("2026-07-30", "warn", True),
            ("2026-07-23", "urgent", True),
            ("2026-07-17", "critical", True),
            ("2026-07-13", "critical", True),
        ],
    )
    def test_levels(self, issued: str, level: str, alert: bool):
        got = token_manager.assess_expiry(self.TODAY, issued)
        assert got.level == level
        assert got.should_alert is alert

    def test_missing_issued_at_is_unknown_and_alerts(self):
        got = token_manager.assess_expiry(self.TODAY, "")
        assert got.level == "unknown"
        assert got.should_alert
        assert got.days_left is None

    def test_bad_format_is_unknown(self):
        got = token_manager.assess_expiry(self.TODAY, "2026/09/13")
        assert got.level == "unknown"
        assert "형식" in got.message

    def test_expired_message_urges_reissue(self):
        got = token_manager.assess_expiry(self.TODAY, "2026-07-01")
        assert got.days_left is not None and got.days_left < 0
        assert "재발급" in got.message

    def test_message_includes_expiry_date(self):
        got = token_manager.assess_expiry(self.TODAY, "2026-07-30")
        assert "2026-09-28" in got.message


class TestExpiryAlertWiring:
    """영속화 실패 시 경보가 실제로 나가는지."""

    @staticmethod
    def _settings(**over):
        s = mock.Mock()
        s.telegram_bot_token = "bot"
        s.telegram_chat_id = "chat"
        s.threads_token = "THAAold"
        s.can_persist_token = True
        s.gh_repo = "owner/repo"
        s.gh_pat = "pat"
        for k, v in over.items():
            setattr(s, k, v)
        return s

    def test_persist_failure_sends_alert(self):
        from src import config, main

        with (
            mock.patch.object(main.token_manager, "refresh_long_lived_token",
                              return_value="THAAnew"),
            mock.patch.object(main.token_manager, "persist_token_to_secret",
                              side_effect=token_manager.SecretPersistError("401")),
            mock.patch.object(config, "TOKEN_ISSUED_AT", "2026-07-23"),
            mock.patch.object(main.notifier, "send") as send,
        ):
            got = main._acquire_token(self._settings())

        assert got == "THAAnew"
        assert send.called
        assert "긴급" in send.call_args[0][2]

    def test_no_pat_sends_alert(self):
        from src import config, main

        with (
            mock.patch.object(main.token_manager, "refresh_long_lived_token",
                              return_value="THAAnew"),
            mock.patch.object(config, "TOKEN_ISSUED_AT", ""),
            mock.patch.object(main.notifier, "send") as send,
        ):
            main._acquire_token(self._settings(can_persist_token=False))

        assert send.called
        assert "확인필요" in send.call_args[0][2]

    def test_success_sends_no_expiry_alert(self):
        from src import main

        with (
            mock.patch.object(main.token_manager, "refresh_long_lived_token",
                              return_value="THAAnew"),
            mock.patch.object(main.token_manager, "persist_token_to_secret"),
            mock.patch.object(main.notifier, "send") as send,
        ):
            got = main._acquire_token(self._settings())

        assert got == "THAAnew"
        assert not send.called

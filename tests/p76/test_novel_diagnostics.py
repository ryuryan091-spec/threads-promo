import json
from types import SimpleNamespace

import pytest

from p76 import novel_diagnostics, novel_llm


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (403, False),
                                             (429, True), (500, True), (None, False)])
def test_diagnostic_is_allowlisted(status, retryable):
    error = RuntimeError("SECRET-PROMPT sk-ant-private")
    error.status_code = status
    error.request_id = "req_1234567890"
    error.body = {"error": {"type": "invalid_request_error",
                            "message": "json_schema additionalProperties SECRET-PROMPT sk-ant-private"}}
    result = novel_diagnostics.diagnose(error, {"model": "test", "messages": ["SECRET-PROMPT"]})
    assert result["retryable"] is retryable
    assert result["field_hints"] == ["json_schema", "additionalProperties"]
    assert result["request_id"] == "req_1234567890"
    assert "SECRET" not in json.dumps(result) and "sk-ant" not in json.dumps(result)


def test_malformed_provider_fields_are_not_logged():
    error = RuntimeError("private")
    error.body = {"error": {"type": ["private"], "message": {"secret": "private"}}}
    error.request_id = "req_private\nINJECTED"
    error.status_code = "private"
    result = novel_diagnostics.diagnose(error, {})
    assert result["error_type"] == "unknown"
    assert result["request_id"] == "unavailable"
    assert result["http_status"] is None


def test_config_hash_stable_excludes_prompt_and_detects_schema_change():
    error = RuntimeError()
    a = {"model": "test", "output_config": {"format": {"schema": {"type": "object"}}}}
    b = {**a, "messages": ["private"], "system": "private"}
    assert novel_diagnostics.diagnose(error, a) == novel_diagnostics.diagnose(error, b)
    b["max_tokens"] = 10
    assert novel_diagnostics.diagnose(error, a)["config_sha256"] != novel_diagnostics.diagnose(error, b)["config_sha256"]


@pytest.mark.parametrize("stream", [False, True])
def test_call_logs_diagnostics_without_error_body(stream, caplog):
    def fail(**kwargs):
        error = RuntimeError("SECRET-PROMPT")
        error.status_code = 400
        error.body = {"error": {"type": "invalid_request_error", "message": "max_tokens SECRET-PROMPT"}}
        raise error
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=fail, stream=fail)))
    with pytest.raises(novel_llm.LLMError, match="api:RuntimeError:400"):
        novel_llm.call(client, label="N1-BIBLE:write", model=novel_llm.settings.MODEL_WRITER, effort="high",
                       system="SECRET-PROMPT", user_blocks=[{"text": "SECRET-PROMPT"}],
                       schema={"type": "object"}, max_tokens=32, stream=stream)
    assert "diagnostic=" in caplog.text and "max_tokens" in caplog.text
    assert "SECRET-PROMPT" not in caplog.text


@pytest.mark.parametrize("message,hint", [
    ("Schema is too complex for compilation.", "schema_compilation_complexity"),
    ("SCHEMA IS TOO COMPLEX\nFOR COMPILATION.", "schema_compilation_complexity"),
    ("Schema compilation timed out.", "schema_compilation_timeout"),
    ("Your credit balance is too low to access the Anthropic API.", "credit_balance"),
    ("Insufficient credits", "credit_balance"),
    ("Schema is too complex for compilation. Insufficient credits", "ambiguous"),
    ("unrecognized rejection", "unknown"),
    ("", "unknown"),
])
def test_provider_reason_hints_do_not_expose_message(message, hint):
    error = RuntimeError()
    error.status_code = 400
    error.body = {"error": {"type": "invalid_request_error",
                            "message": message + " SECRET-PROMPT sk-ant-private"}}
    result = novel_diagnostics.diagnose(error, {})
    assert result["reason_hint"] == hint
    assert result["retryable"] is False
    assert "SECRET" not in json.dumps(result) and "sk-ant" not in json.dumps(result)


@pytest.mark.parametrize("status,error_type", [(500, "api_error"), (400, "unknown"),
                                              (None, "invalid_request_error")])
def test_reason_requires_matching_provider_error(status, error_type):
    error = RuntimeError()
    error.status_code = status
    error.body = {"type": error_type, "message": "Insufficient credits"}
    assert novel_diagnostics.diagnose(error, {})["reason_hint"] == "unknown"


def test_field_hints_case_insensitive():
    error = RuntimeError()
    error.body = {"message": "JSON_SCHEMA ADDITIONALPROPERTIES"}
    assert novel_diagnostics.diagnose(error, {})["field_hints"] == [
        "json_schema", "additionalProperties"]


@pytest.mark.parametrize("message,reason", [
    ("You have reached your specified API usage limits", "organization_spend_limit"),
    ("You have reached your specified workspace API usage limits", "workspace_spend_limit"),
])
def test_spend_limit_messages(message, reason):
    error = RuntimeError()
    error.status_code = 400
    error.body = {"error": {"type": "invalid_request_error", "message": message}}
    result = novel_diagnostics.diagnose(error, {})
    assert result["reason_hint"] == reason
    assert result["message_present"] is True
    assert result["message_length"] == len(message)


def test_tier_spend_limit_does_not_retry_429():
    error = RuntimeError()
    error.status_code = 429
    error.body = {"error": {"type": "rate_limit_error", "details": {
        "error_code": "enforced_spend_limit_reached"}}}
    result = novel_diagnostics.diagnose(error, {})
    assert result["retryable"] is False
    assert result["detail_code"] == "enforced_spend_limit_reached"
    assert result["message_present"] is False
    error.body["error"]["details"]["error_code"] = "SECRET-UNKNOWN"
    result = novel_diagnostics.diagnose(error, {})
    assert result["retryable"] is True
    assert "SECRET" not in json.dumps(result)


def test_private_message_masks_before_truncation():
    error = RuntimeError("NEVER-STRINGIFY")
    error.body = {"message": "x" * 1195 + " sk-ant-" + "PRIVATE" * 50}
    result = novel_diagnostics.private_message(error, {})
    assert len(result) <= 1200
    assert "PRIVATE" not in result
    error.body = {"message": None}
    assert novel_diagnostics.private_message(error, {}) == ""


def test_observed_grammar_size_error():
    error = RuntimeError()
    error.status_code = 400
    error.body = {"error": {"type": "invalid_request_error", "message":
        "The compiled grammar is too large, which would cause performance issues."}}
    result = novel_diagnostics.diagnose(error, {})
    assert result["reason_hint"] == "schema_compiled_grammar_size"
    assert result["retryable"] is False

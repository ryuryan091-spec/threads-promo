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
        novel_llm.call(client, label="N1-BIBLE:write", model="test", effort="high",
                       system="SECRET-PROMPT", user_blocks=[{"text": "SECRET-PROMPT"}],
                       schema={"type": "object"}, max_tokens=32, stream=stream)
    assert "diagnostic=" in caplog.text and "max_tokens" in caplog.text
    assert "SECRET-PROMPT" not in caplog.text

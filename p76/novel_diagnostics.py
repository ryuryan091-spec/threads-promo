"""Allowlisted API diagnostics. Never return provider messages or request bodies."""
from __future__ import annotations

import hashlib
import json
import re

from p76.redact import redact

DETAIL_CODES = frozenset({"enforced_spend_limit_reached"})
H_API_ERROR = "API 오류 진단 (JSON)"

ERROR_TYPES = frozenset({"invalid_request_error", "authentication_error", "permission_error",
                         "not_found_error", "rate_limit_error", "overloaded_error", "api_error"})
FIELDS = ("output_config", "json_schema", "additionalProperties", "max_tokens",
          "thinking", "effort", "fallbacks", "betas", "model", "cache_control")


def _reason_hint(message: str, status: int | None, error_type: str) -> str:
    """Fixed hints from provider evidence, not a root-cause assertion or retry policy."""
    if status == 400 and error_type == "invalid_request_error":
        normalized = " ".join(message.casefold().split())
        patterns = (
            ("schema_compiled_grammar_size", r"\bcompiled grammar is too large\b"),
            ("workspace_spend_limit", r"you have reached your specified workspace api usage limits"),
            ("organization_spend_limit", r"you have reached your specified api usage limits"),
            ("schema_compilation_complexity", r"\bschema is too complex for compilation\b"),
            ("schema_compilation_timeout", r"\bschema compilation (?:timed out|timeout)\b"),
            ("credit_balance", r"\b(?:credit balance is too low|insufficient credits)\b"),
        )
        matches = [code for code, pattern in patterns if re.search(pattern, normalized)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return "ambiguous"
    return "unknown"


def private_message(error: Exception, params: dict) -> str:
    """Bounded, redacted provider message for the existing private Notion ledger only.

    Never stringify the exception/request. Remove echoed input blocks/lines and
    quoted strings before credential masking. Unknown unquoted fragments can
    remain, so this must never be sent to public logs or PRs.
    """
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    message = detail.get("message") if isinstance(detail, dict) else None
    if not isinstance(message, str):
        return ""
    texts = []
    for block in params.get("system", []):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    for item in params.get("messages", []):
        for block in item.get("content", []):
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
    fragments = {part for value in texts for part in [value, *value.splitlines()] if part}
    for fragment in sorted(fragments, key=len, reverse=True):
        message = message.replace(fragment, "[input redacted]")
    message = re.sub(r'"[^"\n]*"|`[^`\n]*`', "[quoted redacted]", message)
    # Mask before truncation: cutting a credential first can defeat its pattern.
    message = redact(message)
    return " ".join(message.split())[:1200]


def diagnose(error: Exception, params: dict) -> dict:
    """Messages are inspected for allowlisted hints; text is never emitted."""
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    error_type = detail.get("type")
    message = detail.get("message")
    message_present = isinstance(message, str)
    message = message if message_present else ""
    details = detail.get("details")
    detail_code = details.get("error_code") if isinstance(details, dict) else None
    detail_code = detail_code if isinstance(detail_code, str) and detail_code in DETAIL_CODES else "unknown"
    fields = [field for field in FIELDS
              if re.search(r"(?<!\w)" + re.escape(field) + r"(?!\w)", message, re.IGNORECASE)]
    request_id = getattr(error, "request_id", None)
    # Strict provider ID shape prevents arbitrary response content entering logs.
    request_id = request_id if isinstance(request_id, str) and re.fullmatch(
        r"req_[A-Za-z0-9]{8,80}", request_id) else "unavailable"
    status = getattr(error, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    config = {key: params.get(key) for key in
              ("model", "max_tokens", "thinking", "output_config", "betas", "fallbacks")}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:16]
    return {"http_status": status,
            "reason_hint": _reason_hint(message, status, error_type),
            "message_present": message_present, "message_length": len(message),
            "detail_code": detail_code,
            "error_type": error_type if isinstance(error_type, str) and error_type in ERROR_TYPES else "unknown",
            "field_hints": fields, "request_id": request_id, "config_sha256": digest,
            "retryable": detail_code != "enforced_spend_limit_reached" and (
                status in (408, 409, 429) or (status is not None and status >= 500)
                or type(error).__name__ in ("APIConnectionError", "APITimeoutError"))}

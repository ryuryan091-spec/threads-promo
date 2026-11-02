"""Allowlisted API diagnostics. Never return provider messages or request bodies."""
from __future__ import annotations

import hashlib
import json
import re

ERROR_TYPES = frozenset({"invalid_request_error", "authentication_error", "permission_error",
                         "not_found_error", "rate_limit_error", "overloaded_error", "api_error"})
FIELDS = ("output_config", "json_schema", "additionalProperties", "max_tokens",
          "thinking", "effort", "fallbacks", "betas", "model", "cache_control")


def diagnose(error: Exception, params: dict) -> dict:
    """Messages are inspected only for fixed field names; text is never emitted."""
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    error_type = detail.get("type")
    message = detail.get("message")
    message = message if isinstance(message, str) else ""
    fields = [field for field in FIELDS
              if re.search(r"(?<!\w)" + re.escape(field) + r"(?!\w)", message)]
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
            "error_type": error_type if isinstance(error_type, str) and error_type in ERROR_TYPES else "unknown",
            "field_hints": fields, "request_id": request_id, "config_sha256": digest,
            "retryable": status in (408, 409, 429) or (status is not None and status >= 500)}

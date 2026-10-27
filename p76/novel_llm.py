"""
p76/novel_llm.py
================
Claude API 호출 (공식 anthropic SDK, D-N6 · DSN-P76-NOVEL-002 §8).

  - beta Messages + 서버측 fallback("default", D-N4) · adaptive thinking(기본) · effort · 구조화 출력
  - 시스템 프롬프트 끝(과 바이블 블록 끝)에 cache_control
  - stop_reason 을 먼저 본다: refusal → 거절(재시도 없음) / max_tokens → 실패(잘린 원고 사용 금지)
  - 응답 model 이 요청과 다르면 fallback 으로 보고 표시한다 → 호출자가 HUMAN_REQUIRED 로 올린다
  - 원고·프롬프트는 로그에 남기지 않는다. 오류는 예외 클래스·HTTP 상태만.
  - 비용: usage 로 계산(가격표 settings.MODEL_PRICES). 호출 전 추정치로 예산을 확인한다.

dry_run 은 FixtureClient(합성 응답)를 쓴다 — API 키·비용 없음.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from p76 import settings

VERSION = "1.0.0"

logger = logging.getLogger(__name__)
CHARS_PER_TOKEN = 1.5  # 한국어 혼합 대략치 (추정용)


class LLMError(RuntimeError):
    """API 호출 실패 (메시지는 코드만)."""


@dataclass
class Result:
    data: dict | None
    stop_reason: str
    model: str
    cost: float
    refusal: str = ""
    fallback: bool = False
    usage: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.data is not None and not self.refusal


def price(model: str) -> tuple[float, float, float]:
    return settings.MODEL_PRICES.get(model, settings.MODEL_PRICE_UNKNOWN)


def cost_of(model: str, usage: dict) -> float:
    p_in, p_out, p_cache = price(model)
    total = (
        usage.get("input_tokens", 0) * p_in
        + usage.get("cache_creation_input_tokens", 0) * p_in * settings.CACHE_WRITE_FACTOR
        + usage.get("cache_read_input_tokens", 0) * p_cache
        + usage.get("output_tokens", 0) * p_out
    )
    return round(total / 1_000_000, 4)


def estimate(model: str, chars: int, max_tokens: int, expected_out: int) -> float:
    """호출 전 상한 추정 — 입력은 전량 미캐시, 출력은 기대치와 max_tokens 의 중간."""
    p_in, p_out, _ = price(model)
    out_tokens = (expected_out + max_tokens) / 2
    return round((chars / CHARS_PER_TOKEN * p_in + out_tokens * p_out) / 1_000_000, 4)


def make_client() -> Any:
    if settings.MODE == "dry_run":
        return FixtureClient()
    if not settings.CLAUDE_KEY:
        raise LLMError("claude_key_missing")
    import anthropic  # 러너에서만 설치(p76/requirements-novel.txt)

    for name in ("anthropic", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return anthropic.Anthropic(api_key=settings.CLAUDE_KEY, max_retries=2, timeout=900.0)


def _usage(msg: Any) -> dict:
    u = getattr(msg, "usage", None)
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return {k: int(getattr(u, k, 0) or 0) for k in keys}


def _text(msg: Any) -> str:
    return "".join(getattr(b, "text", "") for b in getattr(msg, "content", []) or []
                   if getattr(b, "type", "") == "text")


def call(client: Any, *, label: str, model: str, effort: str, system: str, user_blocks: list[dict],
         schema: dict, max_tokens: int, stream: bool) -> Result:
    """user_blocks: [{"text": str, "cache": bool}] — 앞쪽 고정 블록에 cache=True."""
    content = []
    for block in user_blocks:
        item: dict = {"type": "text", "text": block["text"]}
        if block.get("cache"):
            item["cache_control"] = {"type": "ephemeral"}
        content.append(item)
    params = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        "betas": [settings.FALLBACK_BETA],
        "fallbacks": "default",
    }
    try:
        if stream:
            with client.beta.messages.stream(**params) as s:
                msg = s.get_final_message()
        else:
            msg = client.beta.messages.create(**params)
    except Exception as e:  # noqa: BLE001 — SDK 예외 계층 전체. 메시지에는 요청 본문이 섞일 수 있어 버린다
        status = getattr(e, "status_code", "") or ""
        logger.error(f"[NovelLLM] {label} 호출 실패 {type(e).__name__} {status}")
        raise LLMError(f"api:{type(e).__name__}{':' + str(status) if status else ''}") from None
    usage = _usage(msg)
    used_model = str(getattr(msg, "model", "") or model)
    result_cost = cost_of(used_model, usage)
    stop = str(getattr(msg, "stop_reason", "") or "")
    fallback = used_model != model
    logger.info(f"[NovelLLM] {label} stop={stop} fallback={str(fallback).lower()} "
                f"in={usage['input_tokens']} cache_r={usage['cache_read_input_tokens']} "
                f"out={usage['output_tokens']} cost=${result_cost}")
    if stop == "refusal":
        details = getattr(msg, "stop_details", None)
        category = str(getattr(details, "category", "") or "unknown")
        return Result(None, stop, used_model, result_cost, refusal=category, fallback=fallback, usage=usage)
    if stop != "end_turn":
        return Result(None, stop, used_model, result_cost, fallback=fallback, usage=usage)
    try:
        data = json.loads(_text(msg))
    except ValueError:
        return Result(None, "invalid_json", used_model, result_cost, fallback=fallback, usage=usage)
    if not isinstance(data, dict):
        return Result(None, "invalid_json", used_model, result_cost, fallback=fallback, usage=usage)
    return Result(data, stop, used_model, result_cost, fallback=fallback, usage=usage)


# ── dry_run 합성 응답 ─────────────────────────────────────
class _Msg:
    def __init__(self, data: dict, model: str):
        self.content = [type("B", (), {"type": "text", "text": json.dumps(data, ensure_ascii=False)})()]
        self.stop_reason = "end_turn"
        self.stop_details = None
        self.model = model
        self.usage = type("U", (), {"input_tokens": 0, "output_tokens": 0,
                                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})()


class _Stream:
    def __init__(self, msg: _Msg):
        self.msg = msg

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> _Msg:
        return self.msg


class FixtureClient:
    """dry_run 용. 요청 스키마의 required 키로 바이블·회차·검수 중 무엇인지 고른다."""

    def __init__(self) -> None:
        self.beta = type("Beta", (), {"messages": self})()
        self.calls: list[dict] = []

    def _respond(self, params: dict) -> _Msg:
        from p76 import novel_fixtures

        self.calls.append(params)
        required = set(params["output_config"]["format"]["schema"].get("required", []))
        if "arc" in required:
            data = novel_fixtures.bible()
        elif "body" in required:
            data = novel_fixtures.episode(params)
        else:
            data = novel_fixtures.review()
        return _Msg(data, params["model"])

    def stream(self, **params: Any) -> _Stream:
        return _Stream(self._respond(params))

    def create(self, **params: Any) -> _Msg:
        return self._respond(params)

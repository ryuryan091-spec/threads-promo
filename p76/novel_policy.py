"""Bounded Sonnet generation, repair checkpoints and notified quality exceptions.

DN2026_0119(DSN-P76-NOVEL-003 §5-1): 검수 플래그·BLOCK 도 자동 수정 입력이 된다(결과코드 'review:<판정>|<코드>').
자동 수정 횟수는 행 페이지의 '자동 게이트 (JSON)' 섹션에 둔다.
"""
from __future__ import annotations

import hashlib
import json

from p76 import notion, novel_tg
from p76 import novel_store as store
from p76 import settings as S

H_DRAFT = "수정용 초안 (JSON)"
H_DRAFT_META = "수정용 초안 출처 (JSON)"
H_USAGE = "사용량 (JSON)"
H_RELEASE = "품질 경고 알림 (JSON)"
H_GATE = "자동 게이트 (JSON)"  # DN2026_0119 {"repairs": 자동 수정 횟수}
REVIEW_PREFIX = "review:"
QUALITY = {"R09:cliche", "R09:repeated_sentences", "Q:too_long"}


def quality_only(errors: list[str], body: str) -> bool:
    # Short/oversized output cannot use the warning exception.
    return bool(errors) and 2000 <= len(body.strip()) <= 6000 and all(
        e in QUALITY or e.startswith("R01:length:") for e in errors)


def allow_quality(row: dict, errors: list[str], draft: dict) -> bool:
    return (notion.number(row, S.N_ATTEMPTS) == S.NOVEL_MAX_ATTEMPTS
            and quality_only(errors, str(draft.get("body") or "")))


def content_hash(draft: dict) -> str:
    public = {k: draft.get(k) or "" for k in ("body", "question", "subtitle", "card_quote")}
    return hashlib.sha256(json.dumps(public, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def public_draft(row: dict) -> dict:
    sec = store.sections(row)
    return {"body": sec.get(store.H_BODY, ""), "question": sec.get(store.H_QUESTION, ""),
            "subtitle": sec.get(store.H_SUBTITLE, ""), "card_quote": sec.get(store.H_QUOTE, "")}


def save_json(row: dict, heading: str, data: dict) -> None:
    store.append(row, store.section_blocks([(heading, json.dumps(data, ensure_ascii=False), "json")]))


def save_candidate(row: dict, data: dict, result=None) -> None:
    save_json(row, H_DRAFT, data)
    if result is not None:
        save_json(row, H_DRAFT_META, {"model": result.model, "fallback": result.fallback,
                  "hash": hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})


def candidate_origin(row: dict, data: dict) -> tuple[str, bool]:
    meta = store.json_section(row, H_DRAFT_META)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    trusted = (meta.get("hash") == digest and meta.get("model") == S.MODEL_WRITER
               and meta.get("fallback") is False)
    return (S.MODEL_WRITER if trusted else "unverified"), not trusted


def release_ok(row: dict) -> bool:
    meta = store.json_section(row, store.H_META)
    if not meta.get("quality_warnings"):
        return True
    if store.state(row) == S.NS_APPROVED and notion.select(row, S.N_VERDICT) != "PASS":
        # DN2026_0125: 코드의 자동 승인은 검수 PASS 일 때만 일어난다(run_novel._gate·_migrate_*).
        #   PASS 가 아닌 '승인' = 회장이 원고(품질 경고 포함)를 보고 직접 승인한 것 → 발행 결정으로 본다.
        return True
    digest = content_hash(public_draft(row))
    receipt = store.json_section(row, H_RELEASE)
    return (meta.get("quality_hash") == digest == receipt.get("hash")
            and receipt.get("sent") is True and receipt.get("mode") == S.MODE
            and notion.select(row, S.N_VERDICT) == "PASS")


def notify_quality(row: dict, log) -> None:
    meta = store.json_section(row, store.H_META)
    errors = meta.get("quality_warnings")
    if not errors or release_ok(row) or S.MODE == "dry_run":
        return
    draft = public_draft(row)
    digest = content_hash(draft)
    if (notion.select(row, S.N_VERDICT) != "PASS" or store.stopped(row)
            or meta.get("quality_hash") != digest or not allow_quality(row, errors, draft)):
        return
    text = (f"⚠️ {store.key(row)} 생성 3/3회 종료 — 품질 경고: {', '.join(errors)}.\n"
            "완성 원고 AI 검수 PASS. 이 경고를 포함한 원고를 기존 일정에 따라 발행합니다.\n"
            + ("shadow 모드: 실제 게시 없음.\n" if S.MODE != "live" else "")
            + "원고와 경고는 아래 노션에서 확인하세요. 중단 체크 시 발행하지 않습니다.")
    if not novel_tg.send("T7", text, novel_tg.page_link(row["id"])):
        log.degrade("tg:quality_notice_failed")
        return
    save_json(row, H_RELEASE, {"hash": digest, "sent": True, "mode": S.MODE, "codes": errors})
    log.writes += 1


def record_usage(row: dict, label: str, res) -> None:
    total = store.json_section(row, H_USAGE)
    total["calls"] = total.get("calls", 0) + 1
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        total[key] = total.get(key, 0) + res.usage.get(key, 0)
    thinking = res.usage.get("thinking_tokens")
    if thinking is not None:
        total["known_thinking_tokens"] = total.get("known_thinking_tokens", 0) + thinking
    else:
        total["thinking_unknown_calls"] = total.get("thinking_unknown_calls", 0) + 1
    total["last_stage"] = label.rsplit(":", 1)[-1]
    total["cost_usd"] = round(total.get("cost_usd", 0) + res.cost, 4)
    save_json(row, H_USAGE, total)


def gate(row: dict) -> dict:
    data = store.json_section(row, H_GATE)
    return {"repairs": int(data.get("repairs") or 0)}


def save_gate(row: dict, data: dict) -> None:
    save_json(row, H_GATE, {"repairs": int(data.get("repairs") or 0)})


def review_result_code(verdict: str, codes: str) -> str:
    """검수 미통과 → 다음 실행의 자동 수정 입력 (코드만, 원고 없음)."""
    return f"{REVIEW_PREFIX}{verdict}|{codes}"


def _review_findings(row: dict) -> dict:
    review = store.json_section(row, store.H_REVIEW)
    if not review:
        review = store.json_section(row, store.H_META).get("review") or {}
    return {k: review.get(k) for k in ("legal_claims", "advice_tone", "evidence") if review.get(k)}


def review_repair(row: dict, schema: dict, blocks: list[dict], candidate: dict, raw: str) -> tuple:
    """DN2026_0119: 검수 플래그·BLOCK 자동 수정 — 전체 스키마로 다시 받되 직전 초안과 검수 코드를 준다(API 입력에만)."""
    codes = raw[len(REVIEW_PREFIX):]
    text = ("[자동 수정 — 안전 검수 미통과] 다음 검수 코드를 해소하도록 고쳐 주세요: " + codes
            + "\n식별 가능성·수법 상세·법률 단정·조언 어조·피해자 탓·신체 위해 묘사를 줄이고, 나머지는 유지하세요.")
    findings = _review_findings(row)
    if findings:
        text += "\n[검수 지적]\n" + json.dumps(findings, ensure_ascii=False)
    if candidate:
        text += "\n[직전 초안]\n" + json.dumps(candidate, ensure_ascii=False)
    return schema, blocks + [{"text": text}], {}, set()


def repair_request(row: dict, schema: dict, blocks: list[dict]) -> tuple:
    candidate = store.json_section(row, H_DRAFT)
    raw = notion.plain(row, S.N_RESULT)
    if raw.startswith(REVIEW_PREFIX):
        return review_repair(row, schema, blocks, candidate, raw)
    if not candidate or not raw.startswith("rules:"):
        return schema, blocks, {}, set()
    codes = raw[len("rules:"):].split(",")
    fields = set()
    for code in codes:
        if "@" in code:
            fields.add(code.rsplit("@", 1)[1])
        elif code.startswith("Q:"):
            fields.add("question")
        elif code.startswith("B01:"):
            fields.add("arc")
        elif code.startswith("B02:"):
            fields.add("cast")
        elif code.startswith("B03:"):
            fields.update(("source", "arc"))
        elif "body" in candidate:
            fields.update(("body", "continuity", "summary"))
        else:
            fields.update(candidate)  # unknown location: retain full schema, use prior draft
    props = schema.get("properties", {})
    fields &= set(candidate)
    if not fields or not fields <= set(props):
        # Schemas without field definitions cannot safely be projected.
        return schema, blocks + [{"text": "[직전 초안 — 결함만 수정]\n" + json.dumps(candidate, ensure_ascii=False)}], {}, set()
    projected = {"type": "object", "properties": {k: props[k] for k in sorted(fields)},
                 "required": sorted(fields), "additionalProperties": False}
    if "$defs" in schema:
        projected["$defs"] = schema["$defs"]
    instruction = ("[부분 수정] 전체 원고를 다시 쓰지 마세요. 다음 최상위 필드만 반환하세요: "
                   + ",".join(sorted(fields)) + "\n[직전 초안]\n" + json.dumps(candidate, ensure_ascii=False))
    return projected, blocks + [{"text": instruction}], candidate, fields

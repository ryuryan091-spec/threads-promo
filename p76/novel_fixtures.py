"""
p76/novel_fixtures.py
=====================
dry_run·테스트용 합성 응답. 실제 일지·소재·인물과 무관한 문장만 쓴다 (STD-TEST-14).
"""
from __future__ import annotations

import re

VERSION = "1.0.0"


def bible() -> dict:
    cast = [
        {"slot": slot, "alias": alias, "role": role, "speech": "짧게 말한다", "appearance_en": "short hair, gray coat",
         "never": "얼굴 묘사 없음"}
        for slot, alias, role in (("K1", "의뢰인 S", "의뢰인"), ("K2", "용의자 M", "용의자"), ("K3", "목격자 R", "목격자"),
                                  ("S4", "관리자 P", "관리자"), ("S5", "회사 쪽", "회사"))
    ]
    arc = [
        {"episode": m, "stage": stage, "d_plus": m, "place": "작은 회의실", "beats": [f"합성 장면 {m}-1", f"합성 장면 {m}-2"],
         "requirement": "-", "quote_journals": [], "hook": "다음 장소 예고"}
        for m, stage in enumerate(("의뢰", "잠입", "관찰", "탐문", "전환", "대면", "결말"), start=1)
    ]
    return {
        "candidates": [{"title": "합성 시즌", "logline": "합성 로그라인", "chosen": True, "reason": "-"}],
        "season_title": "합성 시즌 제목",
        "logline": "합성 로그라인 한 줄",
        "case_types": ["T02"],
        "source": {"kind": "창작", "pm_ids": [], "promoted_journals": []},
        "company": {"description": "중간 규모 사무직 회사", "spaces": ["작은 회의실", "복도", "탕비실"]},
        "cast": cast,
        "arc": arc,
        "foreshadowing": [{"id": "F1", "plant": 1, "payoff": 7, "note": "합성 복선"}],
        "ending": {"judgement": "합성 판단", "first_step": "상담 예약"},
        "risk_notes": [],
    }


def episode(params: dict) -> dict:
    text = "".join(c.get("text", "") for c in params["messages"][0]["content"])
    found = re.search(r"\[이번 화\]\s*제(\d)화", text) or re.search(r"제(\d)화", text)
    m = int(found.group(1)) if found else 1
    lines = [f"D+{m}. 합성 원고 제{m}화의 첫 장면이다."]
    i = 0
    while sum(len(x) + 1 for x in lines) < 3200:
        i += 1
        lines.append(f"제{m}화 합성 문장 {i}번은 기록 연습을 위해 만든 문장이며 실제 사람과 무관하다.")
        if i % 6 == 0:
            lines.append("")
    return {
        "subtitle": f"합성 제{m}화",
        "body": "\n".join(lines),
        "question": "기록을 남겨 본 적이 있나요?",
        "card_quote": "기록은 남는다.",
        "quoted_journals": [],
        "placeholders_used": [],
        "continuity": {"characters_seen": ["의뢰인 S"], "new_facts": [], "open_threads": [], "closed_threads": [],
                       "d_plus_end": m},
        "summary": f"합성 제{m}화 요약",
        "self_check": {"no_real_identifiers": True, "harm_within_limits": True, "no_victim_blame": True,
                       "no_legal_text": True, "suspect_not_convicted": True, "notes": ""},
    }


def review() -> dict:
    return {"identifiability": 0, "method_detail": 0, "victim_blame": "pass", "graphic_harm": "pass",
            "vigilante": "pass", "premature_guilt": "pass", "legal_claims": [], "advice_tone": [],
            "meta_policy": "low", "verdict": "PASS", "evidence": []}

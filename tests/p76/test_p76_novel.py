"""사건 소설(B형) 자동 작성·게시 — TN-01~18 (DSN-P76-NOVEL-002 §10 · DN2026_0072). 합성 데이터만 쓴다."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import yaml
from p76_fake import DB05, DB12, FAKE_PAGE_ID, FakeResp, png_bytes

from p76 import (
    notion,
    novel_fixtures,
    novel_llm,
    novel_policy,
    novel_prompts,
    novel_rules,
    novel_store,
    novel_tg,
    run_novel,
    run_novel_publish,
    settings,
)
from p76.notion import KST
from p76.runlog import RunLog

S = settings
ROOT = Path(__file__).resolve().parents[2]
CHAT = "-100555000111"
FAKE_NOVEL_BOT = "7000000001:" + "Nv" * 18  # FAKE
FAKE_CLAUDE_KEY = "sk-ant-" + "FAKE" * 8  # FAKE
NB_SCHEMA = {"type": "object", "required": ["arc", "cast", "source"]}
NE_SCHEMA = {"type": "object", "required": ["body", "question"]}
NR_SCHEMA = {"type": "object", "required": ["verdict"]}


def kst(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=KST)


# ── 가짜 텔레그램 · LLM ──────────────────────────────────
class FakeTG:
    exceptions = requests.exceptions

    def __init__(self):
        self.sent: list[str] = []
        self.urls: list[str] = []

    def post(self, url, json=None, timeout=None):
        self.urls.append(url)
        self.sent.append(json["text"])
        return FakeResp(200, {"ok": True})

    def get(self, url, params=None, timeout=None):
        raise AssertionError("getUpdates 금지 (DN2026_0072)")


class Scripted(novel_llm.FixtureClient):
    """script 항목: dict(data=…, stop=…, model=…, category=…, out=…) 또는 None(합성 응답)."""

    def __init__(self, script=None):
        super().__init__()
        self.script = list(script or [])

    def _respond(self, params):
        item = self.script.pop(0) if self.script else None
        if item is None:
            return super()._respond(params)
        self.calls.append(params)
        msg = novel_llm._Msg(item.get("data") or {}, item.get("model") or params["model"])
        msg.stop_reason = item.get("stop", "end_turn")
        if item.get("category"):
            msg.stop_details = SimpleNamespace(category=item["category"])
        if item.get("data") is None:
            msg.content = []
        msg.usage.output_tokens = item.get("out", 0)
        return msg


def _h(level, text):
    kind = f"heading_{level}"
    return {"type": kind, kind: {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def _code(text):
    return {"type": "code", "code": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def _row(cells):
    return {"type": "table_row", "table_row": {"cells": [[{"plain_text": c}] for c in cells]}}


def add_prompt_page(fake, title="⚙️ 소설 프롬프트 정본 v1.0", law_date="2026-11-01"):
    pid = fake.add_plain_page(title)
    table_id = "ab" * 16  # FAKE
    fake.set_blocks(pid, [
        _h(3, "NB system"), _code("바이블 시스템 프롬프트 표식-NB"),
        _h(3, "NB-S 출력 스키마"), _code(json.dumps(NB_SCHEMA)),
        _h(3, "NE system"), _code("회차 시스템 프롬프트 표식-NE {cliches} {style_samples} {{LAW_76_2}}"),
        _h(3, "NE-S 출력 스키마"), _code(json.dumps(NE_SCHEMA)),
        _h(3, "NR system"), _code("검수 시스템 프롬프트 표식-NR"),
        _h(3, "NR-S 출력 스키마"), _code(json.dumps(NR_SCHEMA)),
        _h(2, "법률 블록 (코드 치환용)"), {"type": "table", "id": table_id, "table": {}},
        _h(2, "클리셰 목록 (초안)"), _code("숨이 멎었다\n공기가 무겁다 / 공기가 무거웠다"),
    ])
    fake.set_blocks(table_id, [
        _row(["블록", "문안", "최종 확인일"]),
        _row(["LAW_76_2", "⚖️ 법률 블록 76의2 합성 문안", law_date]),
        _row(["LAW_1350", "📞 상담 안내 합성 문안", "확인 필요"]),
        _row(["LAW_DISCLAIMER", "작중 판단 합성 면책", "—"]),
    ])
    return pid


@pytest.fixture
def novel(fake_notion, fake_graph, env, monkeypatch):
    pid = add_prompt_page(fake_notion)
    env(PAGE76_MODE="shadow", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12,
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=pid, PAGE76_CLAUDE_KEY=FAKE_CLAUDE_KEY)
    tg = FakeTG()
    monkeypatch.setattr(novel_tg, "requests", tg)
    for no in (5, 7, 21):
        fake_notion.add(DB05, **{S.D5_TITLE: f"수사일지 #{no:03d}", S.D5_NO: no, S.D5_STATE: S.S_DONE,
                                 "피드본문": f"합성 수사일지 {no}번 본문. D+1"})

    def render(label, headline, meta, work_dir, runner=None):
        p = Path(work_dir) / "card.png"
        p.write_bytes(png_bytes(1080, 1350))
        return str(p)
    monkeypatch.setattr("p76.card_render.render", render)
    return SimpleNamespace(fake=fake_notion, graph=fake_graph, tg=tg, prompt_page=pid)


def run(when, client=None):
    log = RunLog("novel", "t")
    client = client if client is not None else Scripted()
    run_novel.body(log, now=when, client=client)
    return log, client


def rows(fake, kind=None):
    out = fake.rows(DB12)
    return [r for r in out if kind is None or fake.val(r, S.N_KIND) == kind]


def row(fake, key):
    return next(r for r in fake.rows(DB12) if fake.val(r, S.N_KEY) == key)


def add_canon_bible(fake, n=1):
    page = fake.add(DB12, **{S.N_KEY: f"N{n}-BIBLE", S.N_KIND: S.NK_BIBLE, S.N_SEASON: n, S.N_STATE: S.NS_CANON})
    fake.set_blocks(page["id"], novel_store.section_blocks(
        [(novel_store.H_BIBLE, json.dumps(novel_fixtures.bible(), ensure_ascii=False), "json")]))
    return page


def add_episode_row(fake, n, m, state, body=None, question="기록을 남겨 본 적이 있나요?", due=None,
                    verdict="PASS", model=None):
    body = body if body is not None else novel_fixtures.episode(
        {"messages": [{"content": [{"text": f"제{m}화"}]}]})["body"]
    page = fake.add(DB12, **{S.N_KEY: f"N{n}-E{m:02d}", S.N_KIND: S.NK_EPISODE, S.N_SEASON: n, S.N_EPISODE: m,
                             S.N_STATE: state, S.N_DUE: (due or run_novel.due_date(n, m)).isoformat(),
                             S.N_VERDICT: verdict, S.N_MODEL: model or S.MODEL_WRITER})
    fake.set_blocks(page["id"], novel_store.section_blocks([
        (novel_store.H_BODY, body, "code"), (novel_store.H_QUESTION, question, "code"),
        (novel_store.H_QUOTE, "기록은 남는다.", "code"), (novel_store.H_SUBTITLE, "합성 부제", "code"),
        (novel_store.H_META, json.dumps({"summary": "요약", "quoted_journals": []}), "json"),
    ]))
    return page


# ── 규칙 (TN-10·11) ──────────────────────────────────────
def _body(extra=""):
    return novel_fixtures.episode({"messages": [{"content": [{"text": "제2화"}]}]})["body"] + extra


@pytest.mark.parametrize("extra,code", [
    ("\n2026-11-29 오후였다.", "R02:real_date"),
    ("\n그 회사는 캐피탈이었다.", "R04:industry"),
    ("\n서울의 사무실이었다.", "R04:place"),
    ("\n과태료가 나온다고 했다.", "R05:legal_text"),
    ("\n제76조의2를 떠올렸다.", "R05:legal_text"),
    ("\n댓글로 남겨 주세요.", "R06:pushy_phrase"),
    ("\n사망 소식이었다.", "R07:harm_term"),
    ("\n그는 가해자였다.", "R08:convicted_early"),
    ("\n숨이 멎었다. 숨이 멎었다. 공기가 무거웠다.", "R09:cliche"),
    ("\n앞 문장 {{LAW_76_2}}", "R11:placeholder_not_alone"),
    ("\n{{LAW_116}}", "R11:law_final_only"),
    ("\n{{LAW_NEW}}", "R11:unknown_placeholder"),
])
def test_tn10_rules_catch(env, extra, code):
    errors = novel_rules.check_episode(_body(extra), 2, cliches=("숨이 멎었다", "공기가 무겁다 / 공기가 무거웠다"))
    assert code in errors


def test_tn10_rules_clean_and_banned(env):
    assert novel_rules.check_episode(_body(), 2) == []
    assert novel_rules.check_episode(_body("\n비밀단어"), 2, banned_terms=("비밀단어",)) == ["R03:banned_term"]
    assert any(e.startswith("R01:length") for e in novel_rules.check_episode("D+1 짧다", 2))


def test_tn10_quote_must_match(env):
    body = _body("\n\n📓 수사일지 #005 발췌\n원문과 다른 줄\n\n끝.")
    assert "R10:quote_mismatch:#005" in novel_rules.check_episode(body, 2, journals={5: "원문 줄"})
    ok = _body("\n\n📓 수사일지 #005 발췌\n원문 줄\n\n끝.")
    assert novel_rules.check_episode(ok, 2, journals={5: "앞\n원문 줄\n뒤"}) == []


def test_tn10_repeated_sentences(env):
    body = _body()
    assert "R09:repeated_sentences" in novel_rules.check_episode(body, 2, prev_bodies=(body,))


def test_tn11_laws_and_footer():
    laws = {"LAW_76_2": ("법문", date(2026, 11, 1)), "LAW_1350": ("상담", None), "LAW_DISCLAIMER": ("면책", None)}
    text, errors = novel_rules.fill_laws("a\n{{LAW_76_2}}\n{{LAW_DISCLAIMER}}", laws, date(2026, 11, 29))
    assert text == "a\n법문\n면책" and errors == []
    # DN2026_0119 R10: 확인일 없음·90일 초과 → 오류 없이 그 줄 제거(앞뒤 빈 줄 정리), 문안 없음 → 차단
    text, errors = novel_rules.fill_laws("a\n\n{{LAW_1350}}\n\nb", laws, date(2026, 11, 29))
    assert errors == [] and text == "a\n\nb"
    text, errors = novel_rules.fill_laws("a\n{{LAW_76_2}}", laws, date(2027, 3, 1))  # 90일 초과
    assert errors == [] and text == "a"
    text, errors = novel_rules.fill_laws("{{LAW_DISCLAIMER}}", laws, date(2027, 3, 1))  # 면책은 확인일 무관
    assert errors == [] and text == "면책"
    _, errors = novel_rules.fill_laws("a\n{{LAW_116}}", laws, date(2026, 11, 29))
    assert errors == ["LAW:missing:LAW_116"]
    foot = novel_rules.footer(1, 2, "질문?")
    for must in ("※ 여러 사례를 재구성한 픽션입니다.", "법률 자문이 아닙니다.", "AI의 도움", "#사건파일76", "💬 질문?",
                 "매일 연재(금요일 휴재)"):
        assert must in foot
    assert "매주 일요일" not in foot
    # 리뷰 W7: 자동 숨김은 금칙어 기준 — '확인 시' 약속을 하지 않는다
    assert "(실명·회사명은 적지 말아 주세요. 해당 댓글은 숨김 처리될 수 있습니다.)" in foot
    assert "확인 시" not in foot


def test_r10_dropped_law_logged_as_code_only(caplog):
    caplog.set_level(logging.WARNING)
    laws = {"LAW_116": ("비공개 합성 법문 표식", None)}
    text, errors = novel_rules.fill_laws("앞\n{{LAW_116}}\n뒤", laws, date(2026, 11, 29))
    assert text == "앞\n뒤" and errors == []
    assert "LAW:dropped:LAW_116" in caplog.text and "비공개 합성 법문 표식" not in caplog.text


def test_verdict_recomputed():
    clean = novel_fixtures.review()
    assert run_novel.computed_verdict(clean) == "PASS"
    assert run_novel.computed_verdict({**clean, "identifiability": 1}) == "HUMAN_REQUIRED"
    # DN2026_0126 B안: 차단은 식별(≥2)·위해 묘사·Meta high 만
    assert run_novel.computed_verdict({**clean, "identifiability": 2}) == "BLOCK"
    assert run_novel.computed_verdict({**clean, "graphic_harm": "fail"}) == "BLOCK"
    assert run_novel.computed_verdict({**clean, "meta_policy": "high"}) == "BLOCK"
    for warn in ({"method_detail": 3}, {"victim_blame": "fail"}, {"vigilante": "fail"}, {"premature_guilt": "fail"},
                 {"legal_claims": ["x"]}, {"advice_tone": ["x"]}, {"meta_policy": "med"}):
        assert run_novel.computed_verdict({**clean, **warn}) == "HUMAN_REQUIRED", warn
    assert run_novel.computed_verdict({}) == "BLOCK"  # 값 없음 = 보수적
    assert run_novel._worse("PASS", "HUMAN_REQUIRED") == "HUMAN_REQUIRED"


def test_model_verdict_field_is_ignored(novel):
    """DN2026_0126: 검수 모델의 verdict 필드가 아니라 점수로 계산한 판정을 쓴다."""
    run(kst(2026, 11, 22, 10, 44), Scripted([None, {"data": {**novel_fixtures.review(), "verdict": "BLOCK"}}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CANON and novel.fake.val(bible, S.N_RESULT) == "auto:gate1"


def test_prompt_page_parse_and_draft_refused(novel, env):
    p = novel_prompts.load()
    assert p.systems["nb"].endswith("표식-NB") and p.schemas["ne"] == NE_SCHEMA
    assert p.laws["LAW_76_2"][1] == date(2026, 11, 1) and p.laws["LAW_1350"][1] is None
    assert p.cliches[0] == "숨이 멎었다" and p.tag("nb").startswith("NB-1.0/")
    env(PAGE76_NOVEL_PROMPT_PAGE_ID=add_prompt_page(novel.fake, title="⚙️ 소설 프롬프트 정본 v1 (초안)"))
    with pytest.raises(novel_prompts.PromptError):
        novel_prompts.load()


# ── TN-01 바이블 자동 정본 (DN2026_0119: 묵시 2시간 대기 삭제) ─────────────
def test_tn01_bible_pass_becomes_canon_immediately(novel):
    log, client = run(kst(2026, 11, 22, 22, 15))  # 야간 실행도 대기 없음
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CANON
    assert novel.fake.val(bible, S.N_RESULT) == "auto:gate1"
    assert "표식-NB" in json.dumps(client.calls[0]["system"], ensure_ascii=False)
    assert "fallbacks" not in client.calls[0] and client.calls[0]["model"] == "claude-sonnet-5-5"
    assert [c["model"] for c in client.calls] == [S.MODEL_WRITER, S.MODEL_REVIEWER]  # PASS → 2차 검수 없음
    assert any("정본 자동 등록" in t and "조치 불필요" in t and "11-29(일)" in t for t in novel.tg.sent)
    assert not any("묵시" in t or "체크해 주세요" in t or "바꿔야" in t for t in novel.tg.sent)  # AC1
    assert all(t.startswith("[사건파일76·소설] ") for t in novel.tg.sent)


def test_tn01_bible_not_before_lead_days(novel):
    _, client = run(kst(2026, 11, 21, 10, 44))  # 제1화(11-29) D-7 = 11-22 전
    assert client.calls == [] and rows(novel.fake, S.NK_BIBLE) == []


def test_tn01_canon_without_telegram(novel, env):
    env(PAGE76_NOVEL_TG_BOT_TOKEN="")
    run(kst(2026, 11, 22, 10, 44))
    assert novel.fake.val(row(novel.fake, "N1-BIBLE"), S.N_STATE) == S.NS_CANON


def test_tn03_telegram_is_send_only(novel):
    """같은 봇을 쓰는 다른 파이프라인의 update 를 건드리지 않는다 — getUpdates·웹훅 호출 없음."""
    run(kst(2026, 11, 22, 10, 44))
    run(kst(2026, 11, 22, 12, 44))
    assert novel.tg.urls and all(u.endswith("/sendMessage") for u in novel.tg.urls)
    source = "\n".join(p.read_text(encoding="utf-8") for p in (Path(run_novel.__file__).parent).glob("*.py"))
    for method in ("getUpdates", "setWebhook", "deleteWebhook"):
        assert f'"{method}"' not in source and f"'{method}'" not in source  # 호출 인자로 쓰이지 않음


# ── TN-02 중단 (노션 체크·스위치) ───────────────────────
def test_tn02_notion_checkbox_stop(novel):
    flagged = {**novel_fixtures.review(), "identifiability": 1, "verdict": "PASS"}
    run(kst(2026, 11, 22, 10, 44), Scripted([None, {"data": flagged}]))  # 자동 수정 대기(검사실패)
    bible = row(novel.fake, "N1-BIBLE")
    bible["properties"][S.N_STOP]["checkbox"] = True
    run(kst(2026, 11, 22, 12, 44))
    assert novel.fake.val(bible, S.N_STATE) == S.NS_STOPPED
    assert any("중단 접수" in t for t in novel.tg.sent)


def test_tn02_stop_after_canon_halts_episodes(novel):
    bible = add_canon_bible(novel.fake)
    bible["properties"][S.N_STOP]["checkbox"] = True
    log, client = run(kst(2026, 11, 26, 10, 44))
    assert novel.fake.val(bible, S.N_STATE) == S.NS_STOPPED
    assert rows(novel.fake, S.NK_EPISODE) == [] and client.calls == []


def test_tn02_global_switch(novel, env):
    env(PAGE76_NOVEL_ENABLED="false")
    run(kst(2026, 11, 22, 10, 44))
    assert not any(DB12 in c[1] for c in novel.fake.calls)


# ── TN-06·08·09 판정·거절 ────────────────────────────────
def test_tn06_human_required_bible_is_warn_canon(novel):
    """DN2026_0126: 경고(HUMAN_REQUIRED) 바이블은 자동 수정·사람 승인 없이 정본 + 텔레그램 경고."""
    flagged = {**novel_fixtures.review(), "identifiability": 1, "legal_claims": ["법 문장"], "verdict": "PASS"}
    _, client = run(kst(2026, 11, 22, 10, 44), Scripted([None, {"data": flagged}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CANON
    assert novel.fake.val(bible, S.N_RESULT) == "auto:warn:gate1"
    assert novel.fake.val(bible, S.N_VERDICT) == "HUMAN_REQUIRED"
    assert len(client.calls) == 2  # 작성 + 1차 검수만(2차·재작성 없음)
    warn = [t for t in novel.tg.sent if "경고 승인" in t]
    assert len(warn) == 1 and "identifiability:1" in warn[0] and "legal:1" in warn[0]
    assert "법 문장" not in warn[0]  # 원고 인용은 보내지 않는다


def test_tn06_fallback_model_is_warn(novel):
    run(kst(2026, 11, 22, 10, 44), Scripted([{"data": novel_fixtures.bible(), "model": "claude-opus-4-8"}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CANON  # DN2026_0126: fallback = 경고 승인
    assert novel.fake.val(bible, S.N_RESULT) == "auto:warn:gate1"


def test_tn08_refusal_no_retry(novel):
    log, client = run(kst(2026, 11, 22, 10, 44), Scripted([{"data": None, "stop": "refusal", "category": "cyber"}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_REFUSED
    assert len(client.calls) == 1


def test_tn09_max_tokens_not_saved(novel):
    run(kst(2026, 11, 22, 10, 44), Scripted([{"data": None, "stop": "max_tokens"}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CHECK_FAILED
    assert novel_store.H_BIBLE not in novel_store.sections(bible)
    assert novel_policy.H_DRAFT not in novel_store.sections(bible)


def test_review_block_goes_to_auto_repair(novel):
    add_canon_bible(novel.fake)
    blocked = {**novel_fixtures.review(), "identifiability": 3, "verdict": "PASS"}
    run(kst(2026, 11, 26, 10, 44), Scripted([None, {"data": blocked}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED  # BLOCK(식별) → 자동 수정
    assert novel.fake.val(e1, S.N_RESULT).startswith("review:BLOCK|")


@pytest.mark.parametrize("over", [{"method_detail": 3}, {"victim_blame": "fail"}, {"advice_tone": ["조언"]}])
def test_non_blocking_review_is_warn_approved(novel, over):
    """DN2026_0126 B안: 식별·위해·Meta high 가 아니면 경고 승인(사람 승인 없음)."""
    add_canon_bible(novel.fake)
    run(kst(2026, 11, 26, 10, 44), Scripted([None, {"data": {**novel_fixtures.review(), **over}}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_APPROVED and novel.fake.val(e1, S.N_RESULT) == "auto:warn:gate1"
    assert run_novel.auto_publishable(e1)
    assert any("경고 승인" in t and "제1화" in t for t in novel.tg.sent)


@pytest.mark.parametrize("over", [{"graphic_harm": "fail"}, {"meta_policy": "high"}, {"identifiability": 2}])
def test_blocking_review_never_approved_on_gate1(novel, over):
    add_canon_bible(novel.fake)
    run(kst(2026, 11, 26, 10, 44), Scripted([None, {"data": {**novel_fixtures.review(), **over}}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED and not run_novel.auto_publishable(e1)


# ── 회차 생성 ────────────────────────────────────────────
def test_episode_generation_buffer(novel):
    add_canon_bible(novel.fake)
    run(kst(2026, 11, 25, 10, 44))
    assert rows(novel.fake, S.NK_EPISODE) == []  # 제1화 D-3(11-26) 전
    for hour in (10, 12, 14, 16):
        run(kst(2026, 11, 26, hour, 44))
    keys = sorted(novel.fake.val(r, S.N_KEY) for r in rows(novel.fake, S.NK_EPISODE))
    assert keys == ["N1-E01", "N1-E02", "N1-E03"]  # 버퍼 3화, 4화(12-02)는 D-3 전
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_APPROVED and novel.fake.val(e1, S.N_RESULT) == "auto:gate1"
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-29"
    assert novel.fake.val(row(novel.fake, "N1-E03"), S.N_DUE) == "2026-12-01"  # 매일 연재
    assert "제1화 합성 문장" in novel.fake.page_text(e1["id"])
    assert any("자동 승인" in t and "11-29(일)" in t and "조치 불필요" in t for t in novel.tg.sent)
    assert not any("(일) /" in t and "12-01" in t for t in novel.tg.sent)  # T2 요일은 실제 요일


def test_episode_rule_retry_then_fail(novel):
    add_canon_bible(novel.fake)
    bad = {**novel_fixtures.episode({"messages": [{"content": [{"text": "제1화"}]}]}), "body": "D+1 짧은 원고"}
    log, client = run(kst(2026, 11, 26, 10, 44), Scripted([{"data": bad}, {"data": bad}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED
    assert len(client.calls) == 1  # one writer call per scheduled execution
    run(kst(2026, 11, 26, 12, 44))  # 다음 실행에서 재시도(시도 2/3)
    assert novel.fake.val(e1, S.N_STATE) == S.NS_APPROVED
    assert novel.fake.val(e1, S.N_ATTEMPTS) == 2


def test_tn14_banned_terms_never_sent(novel, env):
    env(PAGE76_BANNED_TERMS="비공개금칙어")
    add_canon_bible(novel.fake)
    _, client = run(kst(2026, 11, 26, 10, 44))
    assert client.calls and "비공개금칙어" not in json.dumps(client.calls, ensure_ascii=False)


# ── TN-12 예산 ───────────────────────────────────────────
def test_tn12_month_budget_blocks_calls(novel):
    novel.fake.add(DB12, **{S.N_KEY: "CONTROL", S.N_KIND: S.NK_CONTROL,
                            S.N_RESULT: json.dumps({"month": "2026-11", "spent": 59.5})})  # 기본 예산 60 (DN2026_0119 A6)
    log, client = run(kst(2026, 11, 22, 10, 44))
    assert client.calls == [] and "budget:month" in log.codes
    assert any("예산" in t for t in novel.tg.sent)


def test_tn12_run_cap(novel):
    log, client = run(kst(2026, 11, 22, 10, 44), Scripted([{"data": novel_fixtures.bible(), "out": 320_000}]))
    assert "budget:run" in log.codes  # 첫 호출 $3.2 → 검수 호출 전 중단


def test_cost_accounting():
    assert novel_llm.cost_of("claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 0}) == 4.0
    assert novel_llm.cost_of("claude-sonnet-5-5", {"output_tokens": 100_000}) == 1.0
    assert novel_llm.cost_of("other-model", {"output_tokens": 1_000_000}) == 50.0


# ── TN-07·17 게시 (p76.run_novel_publish, DN2026_0072) ────
@pytest.fixture
def live(novel, env):
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page, PAGE76_CLAUDE_KEY=FAKE_CLAUDE_KEY,
        PAGE76_BANNED_TERMS="시험금칙어")  # DN2026_0124: live 게시는 금칙어가 있어야 등록한다
    add_canon_bible(novel.fake)
    novel.graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = FakeResp(200, {"id": "p1", "post_id": "61_99"})
    return novel


def pub(when):
    log = RunLog("novel_publish", "t")
    run_novel_publish.body(log, now=when)
    return log


def test_tn07_code_approves_only_via_gate_and_never_skips_notice(live):
    e1 = add_episode_row(live.fake, 1, 1, S.NS_REVIEW)
    with pytest.raises(novel_store.TransitionError):
        novel_store.transition(e1, S.NS_PUBLISHED)  # 예고 없이 게시 불가
    novel_store.transition(e1, S.NS_APPROVED)  # DN2026_0119 A1: 자동 게이트 통과 = 승인(코드 전이 허용)
    with pytest.raises(novel_store.TransitionError):
        novel_store.transition(e1, S.NS_PUBLISHED)  # 승인에서도 예약 등록(발행예고) 없이 게시 불가


def test_publish_switch_off(live, env):
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="false",
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=live.prompt_page)
    add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    pub(kst(2026, 11, 29, 8, 17))
    pub(kst(2026, 11, 29, 10, 21))
    assert live.graph.writes() == [] and live.tg.sent == []


def test_generation_pipeline_never_posts(live):
    add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    run(kst(2026, 11, 29, 10, 44))
    assert live.graph.writes() == []
    assert live.fake.val(row(live.fake, "N1-E01"), S.N_STATE) == S.NS_APPROVED


def test_remind_day_before(live):
    add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    run(kst(2026, 11, 28, 20, 25), Scripted([{"data": None, "stop": "max_tokens"}]))  # 제2화 생성 실패(재시도 대기)
    e2 = row(live.fake, "N1-E02")
    assert live.fake.val(e2, S.N_STATE) == S.NS_CHECK_FAILED
    assert any("자동 예약 게시됩니다" in t and "N1-E01" in t and "11-29(일)" in t for t in live.tg.sent)
    assert not any("N1-E02" in t for t in live.tg.sent)  # 모레 게시분은 안내 대상 아님·실패도 알림 없음
    assert not any("승인'해야" in t or "체크" in t for t in live.tg.sent)  # AC1
    novel_store.update(row(live.fake, "N1-E01"), {S.N_STATE: {"select": {"name": S.NS_PUBLISHED}}})
    run(kst(2026, 11, 29, 20, 25), Scripted([{"data": None, "stop": "max_tokens"}]))
    assert any("자동 검수 중" in t and "N1-E02" in t and "조치 불필요" in t for t in live.tg.sent)


# ── TN-13·15·16 로그·멱등·모드 ───────────────────────────
def test_tn16_dry_run_writes_nothing(novel, env):
    env(PAGE76_MODE="dry_run", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12,
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page)
    log = RunLog("novel", "t")
    run_novel.body(log, now=kst(2026, 11, 22, 10, 44))  # client=None → FixtureClient (키 없이)
    assert novel.fake.writes() == [] and novel.tg.sent == []


def test_tn16_publish_dry_run_writes_nothing(novel, env):
    env(PAGE76_MODE="dry_run", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page)
    add_canon_bible(novel.fake)
    add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    pub(kst(2026, 11, 29, 8, 17))
    assert novel.fake.writes() == [] and novel.tg.sent == [] and novel.graph.writes() == []


def test_tn15_rerun_same_slot_is_idempotent(novel):
    run(kst(2026, 11, 22, 10, 44))
    run(kst(2026, 11, 22, 10, 44))
    assert len(rows(novel.fake, S.NK_BIBLE)) == 1
    assert len([r for r in rows(novel.fake) if novel.fake.val(r, S.N_KEY) == "CONTROL"]) == 1


def test_season_dates():
    # 시작 11-29(일) → 매일, 금요일(12-04) 휴재. 시즌 연속(시즌 사이 휴재 없음)
    assert run_novel.season_start(1) == S.NOVEL_START == date(2026, 11, 29)
    assert run_novel.due_date(1, 7) == date(2026, 12, 6)
    assert run_novel.season_start(2) == date(2026, 12, 7)
    assert run_novel.due_date(1, 3, (date(2026, 11, 30),)) == date(2026, 12, 2)


# ── 공개 문구 검사 (치명-1, novel_rules 1.1.0 · run_novel 1.2.0) ─────────────
@pytest.mark.parametrize("field,text,code", [
    ("card_quote", "2026-11-29 오후였다.", "R02:real_date@card_quote"),
    ("card_quote", "비밀단어", "R03:banned_term@card_quote"),
    ("subtitle", "캐피탈의 밤", "R04:industry@subtitle"),
    ("subtitle", "서울의 사무실", "R04:place@subtitle"),
    ("question", "과태료를 아시나요?", "R05:legal_text@question"),
    ("question", "댓글로 남겨 주세요", "R06:pushy_phrase@question"),
    ("card_quote", "사망 소식", "R07:harm_term@card_quote"),
    ("subtitle", "가해자의 얼굴", "R08:convicted_early@subtitle"),
    ("card_quote", "{{LAW_76_2}}", "R11:placeholder_in_public_field@card_quote"),
])
def test_public_fields_rules_catch(field, text, code):
    fields = {"card_quote": "기록은 남는다.", "subtitle": "합성 부제", "question": "기록을 남겨 본 적이 있나요?"}
    fields[field] = text
    assert code in novel_rules.check_public_fields(fields, 2, banned_terms=("비밀단어",))


def test_public_fields_clean_and_final_episode():
    clean = {"card_quote": "기록은 남는다.", "subtitle": "합성 부제", "question": "기록을 남겨 본 적이 있나요?"}
    assert novel_rules.check_public_fields(clean, 2) == []
    assert novel_rules.check_public_fields({"subtitle": "가해자의 얼굴"}, S.NOVEL_EPISODES) == []  # 최종화는 허용
    assert novel_rules.check_public_fields({}, 2) == []


def _ep(**over):
    return {**novel_fixtures.episode({"messages": [{"content": [{"text": "제1화"}]}]}), **over}


def test_public_field_banned_term_holds_episode(novel, env):
    env(PAGE76_BANNED_TERMS="비공개금칙어")
    add_canon_bible(novel.fake)
    _, client = run(kst(2026, 11, 26, 10, 44), Scripted([{"data": _ep(card_quote="비공개금칙어 한 줄")}]))
    e1 = row(novel.fake, "N1-E01")
    # DN2026_0126: 금칙어 = 차단 → 보류(사람) 대신 재작성 대상. 검수 호출 없음, 단어는 결과에 없음
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED
    assert novel.fake.val(e1, S.N_RESULT) == "rules:R03:banned_term@card_quote"
    assert len(client.calls) == 1
    assert not run_novel.auto_publishable(e1)


def test_public_field_violation_retries_then_fails(novel):
    add_canon_bible(novel.fake)
    bad = _ep(subtitle="서울의 밤")
    log, client = run(kst(2026, 11, 26, 10, 44), Scripted([{"data": bad}, {"data": bad}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED
    assert "R04:place@subtitle" in novel.fake.val(e1, S.N_RESULT)
    assert len(client.calls) == 1
    _, retry = run(kst(2026, 11, 26, 12, 44), Scripted([{"data": bad}]))
    assert "R04:place@subtitle" in json.dumps(retry.calls[0], ensure_ascii=False)


def test_review_input_includes_public_fields(novel):
    add_canon_bible(novel.fake)
    ep = _ep(card_quote="합성 표지 인용 표식", subtitle="합성 부제 표식", question="합성 질문 표식?")
    _, client = run(kst(2026, 11, 26, 10, 44), Scripted([{"data": ep}]))
    review_call = json.dumps(client.calls[1], ensure_ascii=False)
    for text in ("합성 표지 인용 표식", "합성 부제 표식", "합성 질문 표식?"):
        assert text in review_call
    assert novel.fake.val(row(novel.fake, "N1-E01"), S.N_STATE) == S.NS_APPROVED


def test_publish_rechecks_edited_public_fields(live):
    e1 = add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    live.fake.set_blocks(e1["id"], novel_store.section_blocks([
        (novel_store.H_BODY, _body(), "code"), (novel_store.H_QUESTION, "기록을 남겨 본 적이 있나요?", "code"),
        (novel_store.H_QUOTE, "판교의 밤", "code"), (novel_store.H_SUBTITLE, "합성 부제", "code"),
        (novel_store.H_META, json.dumps({"summary": "요약", "quoted_journals": []}), "json"),
    ]))
    pub(kst(2026, 11, 29, 8, 17))
    pub(kst(2026, 11, 29, 10, 21))
    assert live.graph.writes() == [] and live.fake.val(e1, S.N_STATE) == S.NS_HELD
    assert "R04:place@card_quote" in live.fake.val(e1, S.N_RESULT)


# ── 게시 선점 (치명-2, run_novel_publish 1.1.0) ─────────────────────────────
# ── G4 리뷰(R6-04) 반영: 같은 날 2건 · 미게시 확정 시 선점 해제 · 선점 후 재확인 ─────
def test_publish_rechecks_edited_subtitle(live):
    e1 = add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    live.fake.set_blocks(e1["id"], novel_store.section_blocks([
        (novel_store.H_BODY, _body(), "code"), (novel_store.H_QUESTION, "기록을 남겨 본 적이 있나요?", "code"),
        (novel_store.H_QUOTE, "", "code"), (novel_store.H_SUBTITLE, "여의도의 밤", "code"),
        (novel_store.H_META, json.dumps({"summary": "요약", "quoted_journals": []}), "json"),
    ]))
    pub(kst(2026, 11, 29, 8, 17))
    pub(kst(2026, 11, 29, 10, 21))
    assert live.graph.writes() == [] and "R04:place@subtitle" in live.fake.val(e1, S.N_RESULT)


# ── 1.2.1 / 1.1.1: arc 객체 수용 · R05 바이블 경고 · 재시도 피드백 이월 · 예정 시각 기준 ─────
def _dict_arc_bible(extra_arc=None):
    b = novel_fixtures.bible()
    arc = {f"e{a['episode']}": {k: v for k, v in a.items() if k != "episode"} for a in reversed(b["arc"])}
    arc.update(extra_arc or {})
    return {**b, "arc": arc}


def test_normalize_arc_dict_to_list(env):
    out = run_novel.normalize_arc(_dict_arc_bible())
    assert isinstance(out["arc"], list)
    assert [a["episode"] for a in out["arc"]] == list(range(1, S.NOVEL_EPISODES + 1))
    assert out["arc"][0]["stage"] == "의뢰"
    assert novel_rules.check_bible(out) == []


def test_normalize_arc_bad_key_is_episode_zero(env):
    out = run_novel.normalize_arc(_dict_arc_bible({"ep8": {"stage": "덤"}, "e9x": {"stage": "덤2"}}))
    assert [a["episode"] for a in out["arc"]].count(0) == 2  # 버리지 않는다
    assert "B01:arc_incomplete" in novel_rules.check_bible(out)
    wrong_number = run_novel.normalize_arc({**novel_fixtures.bible(), "arc": {"e1": {}, "e3": {}}})
    assert "B01:arc_incomplete" in novel_rules.check_bible(wrong_number)


def test_normalize_arc_list_unchanged(env):
    b = novel_fixtures.bible()
    assert run_novel.normalize_arc(b) == b
    assert run_novel.normalize_arc({"cast": []}) == {"cast": []}


def test_bible_r05_is_warning_episode_r05_still_error(env):
    b = {**novel_fixtures.bible(), "risk_notes": ["과태료 언급 주의"]}
    assert not any(e.startswith("R05") for e in novel_rules.check_bible(b))
    assert novel_rules.bible_warnings(b) == ["W05:legal_text"]
    assert novel_rules.bible_warnings(novel_fixtures.bible()) == []
    assert "R05:legal_text" in novel_rules.check_episode(_body("\n과태료가 나온다고 했다."), 2)


def test_bible_dict_arc_and_w05_accepted(novel, caplog):
    caplog.set_level(logging.WARNING)
    b = {**_dict_arc_bible(), "risk_notes": ["합성 과태료 메모 표식"]}
    run(kst(2026, 11, 22, 10, 44), Scripted([{"data": b}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CANON
    assert "W05:legal_text" in caplog.text and "합성 과태료 메모 표식" not in caplog.text
    stored = novel_store.json_section(bible, novel_store.H_BIBLE)
    assert [a["episode"] for a in stored["arc"]] == list(range(1, S.NOVEL_EPISODES + 1))


def _set(page, **props):
    from p76 import notion
    novel_store.update(page, {k: (notion.v_number(v) if isinstance(v, int) else notion.v_rich(v))
                              for k, v in props.items()})


def test_prev_rule_codes_carried_to_first_note_episode(novel):
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_CHECK_FAILED)
    _set(e1, **{S.N_RESULT: "rules:R04:place@subtitle,R09:cliche", S.N_ATTEMPTS: 1})
    _, client = run(kst(2026, 11, 26, 10, 44), Scripted([{"data": _ep()}]))
    first = client.calls[0]["messages"][0]["content"][-1]["text"]
    assert "직전 실행이 다음 규칙에 걸렸다: R04:place@subtitle, R09:cliche." in first
    assert novel.fake.val(e1, S.N_STATE) == S.NS_APPROVED and novel.fake.val(e1, S.N_ATTEMPTS) == 2


def test_prev_rule_codes_carried_to_first_note_bible(novel):
    page = novel.fake.add(DB12, **{S.N_KEY: "N1-BIBLE", S.N_KIND: S.NK_BIBLE, S.N_SEASON: 1,
                                   S.N_STATE: S.NS_CHECK_FAILED, S.N_RESULT: "rules:B02:cast_incomplete",
                                   S.N_ATTEMPTS: 1})
    _, client = run(kst(2026, 11, 22, 10, 44))
    assert "직전 실행이 다음 규칙에 걸렸다: B02:cast_incomplete." in \
        client.calls[0]["messages"][0]["content"][-1]["text"]
    assert novel.fake.val(page, S.N_STATE) == S.NS_CANON


def test_no_carry_note_for_non_rules_failure(novel):
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_CHECK_FAILED)
    _set(e1, **{S.N_RESULT: "llm:max_tokens", S.N_ATTEMPTS: 1})
    _, client = run(kst(2026, 11, 26, 10, 44), Scripted([{"data": _ep()}]))
    assert "직전 실행이" not in client.calls[0]["messages"][0]["content"][-1]["text"]


def test_same_rules_retry_at_most_three_total(novel, glive):
    add_canon_bible(novel.fake)
    bad = _ep(subtitle="서울의 밤")
    calls = []
    for when in (kst(2026, 11, 26, 10, 44), kst(2026, 11, 26, 12, 44), kst(2026, 11, 26, 14, 44),
                 kst(2026, 11, 26, 16, 44), kst(2026, 11, 27, 10, 44)):
        _, client = run(when, Scripted([{"data": bad}]))
        calls.extend(client.calls)
    e1 = row(novel.fake, "N1-E01")
    # 3회 소진 → 최종 실패(순연·초기화) → 순연으로 생성 시작일도 하루 밀려(11-27) 그날 재생성 1회
    assert len(calls) == 4
    assert novel.fake.val(e1, S.N_ATTEMPTS) == 1
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-30"  # 11-29 순연
    assert not run_novel.auto_publishable(e1)


def test_different_rules_keep_retrying(novel):
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_CHECK_FAILED)
    _set(e1, **{S.N_RESULT: "rules:R09:cliche", S.N_ATTEMPTS: 1})
    bad = _ep(subtitle="서울의 밤")
    run(kst(2026, 11, 26, 10, 44), Scripted([{"data": bad}, {"data": bad}]))
    assert novel.fake.val(e1, S.N_ATTEMPTS) == 2
    assert not any(run_novel.REPEAT_NOTE in t for t in novel.tg.sent)


# ── 예정 시각 기준 판정 (STD-SCHED-03) ───────────────────
def test_today_follows_scheduled_time(env, monkeypatch):
    from p76 import sched
    monkeypatch.setattr(S, "SCHEDULE_CRON", "44 14 * * *")  # KST 23:44
    now = kst(2026, 11, 16, 1, 30)  # 실제 실행은 다음 날 01:30
    ctx = run_novel.Ctx(log=RunLog("novel", "t"), now=now, rows=[], ctrl_page={}, ctrl={},
                        sched_now=sched.effective_now(now))
    assert ctx.today == date(2026, 11, 15) and ctx.kst.hour == 23
    assert ctx.now == now  # 기록용 now 는 실제 시각
    monkeypatch.setattr(S, "SCHEDULE_CRON", "")
    ctx2 = run_novel.Ctx(log=RunLog("novel", "t"), now=now, rows=[], ctrl_page={}, ctrl={},
                         sched_now=sched.effective_now(now))
    assert ctx2.today == date(2026, 11, 16)


def test_remind_uses_scheduled_hour_and_day(live, monkeypatch):
    add_episode_row(live.fake, 1, 1, S.NS_REVIEW)
    monkeypatch.setattr(S, "SCHEDULE_CRON", "25 11 * * *")  # KST 20:25 예정
    run(kst(2026, 11, 29, 0, 40))  # 4시간 넘게 늦게 시작 — 예정 시각(11-28 20시대) 기준
    assert any("자동 예약 게시됩니다" in t and "N1-E01" in t for t in live.tg.sent)


# ── R6 리뷰 반영 (W4·W5) ────────────────────────────────
def test_rule_kinds_ignore_values():
    assert run_novel._rule_kinds(["R01:length:3012", "R04:place@subtitle"]) == {"R01:length", "R04:place@subtitle"}
    assert run_novel._rule_kinds(["R01:length:2999"]) == run_novel._rule_kinds(["R01:length:4100"])


def test_same_rule_kind_with_different_numbers_stops(novel):
    add_canon_bible(novel.fake)
    short1 = _ep(body="D+1 짧은 본문")
    run(kst(2026, 11, 26, 10, 44), Scripted([{"data": short1}, {"data": short1}]))
    e1 = row(novel.fake, "N1-E01")
    first = novel.fake.val(e1, S.N_RESULT)
    assert first.startswith("rules:") and "R01:length:" in first
    assert novel.fake.val(e1, S.N_ATTEMPTS) == 1
    short2 = _ep(body="D+1 길이가 다른 짧은 본문입니다")
    run(kst(2026, 11, 26, 12, 44), Scripted([{"data": short2}, {"data": short2}]))
    assert novel.fake.val(e1, S.N_RESULT) != first  # 글자 수는 다르지만
    assert novel.fake.val(e1, S.N_ATTEMPTS) == 2
    run(kst(2026, 11, 26, 14, 44), Scripted([{"data": short2}]))
    # 3/3 소진 → 최종 실패(A2-a): 시도 초기화·순연
    assert novel.fake.val(e1, S.N_ATTEMPTS) == 0
    assert novel.fake.val(e1, S.N_RESULT) == "final:rules:exhausted"


@pytest.mark.parametrize("over", [{"identifiability": 3}, {"identifiability": 1}])
def test_quality_warning_never_overrides_ai_review(novel, over):
    """규칙 경고(품질)는 검수 판정을 덮어쓰지 않는다: 식별 BLOCK 이면 경고 원고라도 승인되지 않는다(DN2026_0126)."""
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_CHECK_FAILED)
    _set(e1, **{S.N_ATTEMPTS: 1})
    bad = _ep(question="기" * 121)
    run(kst(2026, 11, 26, 10, 44), Scripted([{"data": bad}, {"data": {**novel_fixtures.review(), **over}}]))
    if over["identifiability"] >= 2:
        assert not run_novel.auto_publishable(e1)
    else:
        assert novel.fake.val(e1, S.N_RESULT) == "auto:warn:gate1" and run_novel.auto_publishable(e1)
        warn = [t for t in novel.tg.sent if "경고 승인" in t]
        assert len(warn) == 1 and "규칙 경고" in warn[0]


def test_partial_repair_preserves_unaffected_fields(novel):
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_CHECK_FAILED)
    _set(e1, **{S.N_ATTEMPTS: 1, S.N_RESULT: "rules:Q:too_long"})
    draft = _ep(question="기" * 121)
    novel_policy.save_candidate(e1, draft)
    schema = {"type": "object", "properties": {"body": {"type": "string"}, "question": {"type": "string"}},
              "required": ["body", "question"], "additionalProperties": False}
    client = Scripted([{"data": {"question": "기록은?"}}])
    ctx = run_novel.Ctx(RunLog("novel", "t"), kst(2026, 11, 26, 10), [], {}, {}, client=client)
    result = run_novel._llm(ctx, e1, label="N1-E01:write", model=S.MODEL_WRITER, effort="medium",
                            system="repair", user_blocks=[{"text": "repair"}], schema=schema,
                            max_tokens=1000, expected_out=100, stream=True)
    assert result.data["body"] == draft["body"] and result.data["question"] == "기록은?"
    assert client.calls[0]["output_config"]["format"]["schema"]["required"] == ["question"]
    usage = novel_store.json_section(e1, novel_policy.H_USAGE)
    assert usage["calls"] == 1 and usage["thinking_unknown_calls"] == 1


def test_opus_never_requested(novel):
    """DN2026_0119: 허용 모델은 작성·1차 검수·독립 2차 검수뿐. 정상 경로(1차 PASS)에서는 Opus 를 부르지 않는다."""
    client = Scripted()
    with pytest.raises(novel_llm.LLMError, match="model:not_allowed"):
        novel_llm.call(client, label="test", model="claude-haiku-5-5", effort="medium", system="x",
                       user_blocks=[{"text": "x"}], schema={}, max_tokens=10, stream=False)
    assert client.calls == []
    _, client = run(kst(2026, 11, 22, 10, 44))
    assert all(c["model"] == "claude-sonnet-5-5" and "fallbacks" not in c for c in client.calls)
    assert client.calls[0]["max_tokens"] == 16000


def test_api_failure_three_total_and_failure_status(novel):
    class Failing(Scripted):
        def stream(self, **params):
            self.calls.append(params)
            error = RuntimeError("private")
            error.status_code = 500
            raise error
    calls = 0
    for h in (10, 12, 14, 16):
        log, client = run(kst(2026, 11, 22, h, 44), Failing())
        calls += len(client.calls)
        if h != 16:
            assert log.status == "failed"
    assert calls == 3
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_ATTEMPTS) == 3
    assert novel_store.H_BIBLE not in novel_store.sections(bible)


def test_permanent_api_failure_holds_and_saves_private_evidence(novel, caplog):
    class Failing(Scripted):
        def stream(self, **params):
            self.calls.append(params)
            error = RuntimeError("DO-NOT-LOG")
            error.status_code = 400
            error.request_id = "req_1234567890"
            error.body = {"error": {"type": "invalid_request_error", "message":
                "You have reached your specified workspace API usage limits. PRIVATE-DETAIL "
                + FAKE_CLAUDE_KEY + ' "quoted input" ' + params["system"][0]["text"]}}
            raise error
    client = Failing()
    for h in (10, 12, 14, 16):
        log, _ = run(kst(2026, 11, 22, h, 44), client)
        assert log.status == "failed"
    assert len(client.calls) == 1
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_ATTEMPTS) == 1
    assert novel.fake.val(bible, S.N_RESULT).endswith(":hold")
    evidence = novel_store.json_section(bible, "API 오류 진단 (JSON)")
    assert evidence["reason_hint"] == "workspace_spend_limit"
    assert "PRIVATE-DETAIL" in evidence["message_redacted"]
    assert FAKE_CLAUDE_KEY not in evidence["message_redacted"]
    assert "quoted input" not in evidence["message_redacted"]
    assert "표식-NB" not in evidence["message_redacted"]
    assert "PRIVATE-DETAIL" not in caplog.text
    assert all("PRIVATE-DETAIL" not in msg for msg in novel.tg.sent)
    assert any("자동 재시도 보류" in msg for msg in novel.tg.sent)
    assert novel_store.H_BIBLE not in novel_store.sections(bible)


def test_evidence_write_failure_keeps_hold(novel, monkeypatch):
    class Failing(Scripted):
        def stream(self, **params):
            self.calls.append(params)
            error = RuntimeError()
            error.status_code = 400
            raise error
    def fail_append(*args, **kwargs):
        raise RuntimeError("PRIVATE-PERSIST-ERROR")
    monkeypatch.setattr(novel_store, "append", fail_append)
    log, client = run(kst(2026, 11, 22, 10, 44), Failing())
    assert log.status == "failed"
    bible = row(novel.fake, "N1-BIBLE")
    assert not run_novel._retryable(bible)
    assert len(client.calls) == 1


def test_bible_disclaimers_are_scoped_and_other_content_still_blocked():
    b = novel_fixtures.bible()
    b['risk_notes'] = ['금융 업종과 구체 업종·지역은 배경으로 쓰지 않는다.',
                       '사망·자해·신체 폭력·선정 묘사는 어느 화에도 없다.']
    assert novel_rules.check_bible(b) == []
    assert 'R03:banned_term@risk_notes' in novel_rules.check_bible(b, banned_terms=('금융',))
    b['risk_notes'][0] += ' 실제 배경은 은행이다.'
    assert 'R04:industry@risk_notes' in novel_rules.check_bible(b)
    b['risk_notes'][1] += ' 사망 장면을 넣는다.'
    assert 'R07:harm_term@risk_notes' in novel_rules.check_bible(b)
    b['season_title'] = '금융 업종과 구체 업종·지역은 배경으로 쓰지 않는다.'
    assert 'R04:industry@season_title' in novel_rules.check_bible(b)


@pytest.mark.parametrize('trusted', [True, False])
def test_revalidated_bible_skips_writer_and_preserves_provenance(novel, trusted):
    bad = novel_fixtures.bible()
    bad['season_title'] = '은행'
    run(kst(2026, 11, 22, 10, 44), Scripted([{'data': bad}]))
    bible = row(novel.fake, 'N1-BIBLE')
    good = novel_fixtures.bible()
    result = novel_llm.Result(good, 'end_turn', S.MODEL_WRITER, 0, fallback=False) if trusted else None
    novel_policy.save_candidate(bible, good, result)
    _set(bible, **{S.N_RESULT: 'rules:R04:industry,R07:harm_term'})
    _, client = run(kst(2026, 11, 22, 12, 44))
    assert len(client.calls) == 1
    assert 'verdict' in client.calls[0]['output_config']['format']['schema']['required']
    fresh = row(novel.fake, 'N1-BIBLE')
    assert novel.fake.val(fresh, S.N_ATTEMPTS) == 1
    assert novel.fake.val(fresh, S.N_STATE) == S.NS_CANON  # DN2026_0126: 미검증 출처(fallback 취급) = 경고 정본
    assert novel.fake.val(fresh, S.N_RESULT) == ("auto:gate1" if trusted else "auto:warn:gate1")


def test_legacy_bible_codes_become_targeted_repair(novel):
    bad = novel_fixtures.bible()
    bad['risk_notes'] = ['은행이 실제 배경이다.']
    run(kst(2026, 11, 22, 10, 44), Scripted([{'data': bad}]))
    bible = row(novel.fake, 'N1-BIBLE')
    _set(bible, **{S.N_RESULT: 'rules:R04:industry'})
    retry = Scripted([{'data': {'risk_notes': ['일반 사무실 배경.']}}, None])
    # Supply real field definitions; the normal fixture schema is intentionally minimal.
    original = run_novel._prompts
    from unittest.mock import patch
    def prompts(ctx):
        p = original(ctx)
        p.schemas['nb']['properties'] = {'risk_notes': {'type': 'array', 'items': {'type': 'string'}}}
        return p
    with patch.object(run_novel, '_prompts', prompts):
        run(kst(2026, 11, 22, 12, 44), retry)
    assert retry.calls[0]['output_config']['format']['schema']['required'] == ['risk_notes']
    saved = novel_store.json_section(row(novel.fake, 'N1-BIBLE'), novel_policy.H_DRAFT)
    assert saved['arc'] == bad['arc']


@pytest.mark.parametrize('value', [
    '사망·자해·신체 폭력·선정 묘사는 어느 화에도 없다. 사망을 구체화한다.',
    '사망·자해·신체 폭력·선정 묘사는 어느 화에도 없다; 사망을 구체화한다.',
    ['사망·자해·신체 폭력·선정 묘사는 어느 화에도 없다\n자해 장면을 넣는다'],
    ['사망·자해·신체 폭력·선정 묘사는 어느 화에도 없다', {'detail': '사망'}],
])
def test_metadata_exception_cannot_hide_extra_harm(value):
    b = novel_fixtures.bible()
    b['risk_notes'] = value
    assert 'R07:harm_term@risk_notes' in novel_rules.check_bible(b)


def test_candidate_hash_tamper_requires_explicit_review(novel):
    b = novel_fixtures.bible()
    b['season_title'] = '은행'
    run(kst(2026, 11, 22, 10, 44), Scripted([{'data': b}]))
    page = row(novel.fake, 'N1-BIBLE')
    good = novel_fixtures.bible()
    novel_policy.save_candidate(page, good, novel_llm.Result(good, 'end_turn', S.MODEL_WRITER, 0))
    good['season_title'] = '출처 기록 이후 바뀐 제목'
    novel_policy.save_candidate(page, good)
    _, client = run(kst(2026, 11, 22, 12, 44))
    assert len(client.calls) == 1
    page = row(novel.fake, 'N1-BIBLE')
    assert novel.fake.val(page, S.N_STATE) == S.NS_CANON  # DN2026_0126: 출처 불일치 = 경고 정본(텔레그램 경고)
    assert novel.fake.val(page, S.N_RESULT) == 'auto:warn:gate1'
    assert any('경고 승인' in t for t in novel.tg.sent)


def test_changed_prompt_does_not_reuse_saved_bible(novel):
    b = novel_fixtures.bible()
    b['season_title'] = '은행'
    run(kst(2026, 11, 22, 10, 44), Scripted([{'data': b}]))
    page = row(novel.fake, 'N1-BIBLE')
    good = novel_fixtures.bible()
    novel_policy.save_candidate(page, good, novel_llm.Result(good, 'end_turn', S.MODEL_WRITER, 0))
    _set(page, **{S.N_PROMPT_VER: 'different-version'})
    _, client = run(kst(2026, 11, 22, 12, 44))
    assert 'arc' in client.calls[0]['output_config']['format']['schema']['required']
    assert novel.fake.val(row(novel.fake, 'N1-BIBLE'), S.N_ATTEMPTS) == 2


def test_reused_draft_keeps_original_notes_for_ai_and_blocks_refusal(novel):
    b = novel_fixtures.bible()
    b['season_title'] = '은행'
    run(kst(2026, 11, 22, 10, 44), Scripted([{'data': b}]))
    page = row(novel.fake, 'N1-BIBLE')
    good = novel_fixtures.bible()
    good['risk_notes'] = ['사망·자해·신체 폭력·선정 묘사는 어느 화에도 없다.']
    novel_policy.save_candidate(page, good, novel_llm.Result(good, 'end_turn', S.MODEL_WRITER, 0))
    _, client = run(kst(2026, 11, 22, 12, 44), Scripted([{'stop': 'refusal', 'data': None}]))
    assert len(client.calls) == 1
    assert good['risk_notes'][0] in json.dumps(client.calls[0]['messages'], ensure_ascii=False)
    assert novel.fake.val(row(novel.fake, 'N1-BIBLE'), S.N_STATE) == S.NS_CHECK_FAILED  # 검수 거절 = BLOCK → 자동 수정
    assert novel.fake.val(row(novel.fake, 'N1-BIBLE'), S.N_RESULT).startswith('review:BLOCK|')
    assert novel.fake.val(row(novel.fake, 'N1-BIBLE'), S.N_ATTEMPTS) == 1


def test_reuse_does_not_bypass_three_attempt_limit(novel):
    b = novel_fixtures.bible()
    b['season_title'] = '은행'
    run(kst(2026, 11, 22, 10, 44), Scripted([{'data': b}]))
    page = row(novel.fake, 'N1-BIBLE')
    good = novel_fixtures.bible()
    novel_policy.save_candidate(page, good, novel_llm.Result(good, 'end_turn', S.MODEL_WRITER, 0))
    _set(page, **{S.N_ATTEMPTS: 3})
    _, client = run(kst(2026, 11, 22, 12, 44))
    assert client.calls == []
    assert novel.fake.val(row(novel.fake, 'N1-BIBLE'), S.N_STATE) == S.NS_CHECK_FAILED


# ── DN2026_0119 Zero-Touch 생성 측 (DSN-P76-NOVEL-003 §4·§5, run_novel 1.4.0) ─────────────
@pytest.fixture
def reviewer2_allowed():
    """통합 후 novel_llm.call 이 MODEL_REVIEWER_2 를 허용한다(novel_llm 1.2.0) — 실제 호출 경로를 그대로 쓴다."""
    assert S.MODEL_REVIEWER_2 != S.MODEL_REVIEWER


def test_reviewer2_model_allowed_by_llm_gate(novel):
    client = Scripted([{"data": novel_fixtures.review()}])
    try:
        novel_llm.call(client, label="t", model=S.MODEL_REVIEWER_2, effort="medium", system="x",
                       user_blocks=[{"text": "x"}], schema={}, max_tokens=10, stream=False)
    except novel_llm.LLMError as e:
        assert str(e) != "model:not_allowed"
    assert client.calls and client.calls[0]["model"] == S.MODEL_REVIEWER_2


def _flag(**over):
    return {**novel_fixtures.review(), "identifiability": 1, "verdict": "PASS", **over}


def _blk(**over):
    """DN2026_0126: 차단 판정(식별 가능성 3)."""
    return {**novel_fixtures.review(), "identifiability": 3, "verdict": "PASS", **over}


def _ctrl(fake):
    return json.loads(fake.val(row(fake, "CONTROL"), S.N_RESULT) or "{}")


def _add_ctrl(fake, data):
    fake.add(DB12, **{S.N_KEY: "CONTROL", S.N_KIND: S.NK_CONTROL, S.N_RESULT: json.dumps(data)})


def test_reviewer2_is_a_different_model(env, monkeypatch):
    assert S.MODEL_REVIEWER_2 not in (S.MODEL_WRITER, S.MODEL_REVIEWER)
    assert S.MODEL_REVIEWER_2 in S.MODEL_PRICES
    monkeypatch.setattr(S, "MODEL_REVIEWER_2", S.MODEL_REVIEWER)
    ctx = run_novel.Ctx(RunLog("novel", "t"), kst(2026, 11, 26, 10), [], {}, {})
    with pytest.raises(novel_llm.LLMError, match="review2:same_model"):
        run_novel.review2(ctx, {"id": "dry-x", "properties": {}}, "t", "x")


def test_gate2_after_repairs_approves(novel, reviewer2_allowed):
    add_canon_bible(novel.fake)
    run(kst(2026, 11, 26, 10, 44), Scripted([None, {"data": _blk()}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED
    _, c2 = run(kst(2026, 11, 26, 12, 44), Scripted([None, {"data": _blk()}]))
    assert "identifiability:3" in c2.calls[0]["messages"][0]["content"][-1]["text"]  # 검수 코드 = 수정 지시
    assert novel_store.json_section(e1, novel_policy.H_GATE) == {"repairs": 2}
    _, c3 = run(kst(2026, 11, 26, 14, 44), Scripted([None, {"data": _blk()}, None]))
    assert [c["model"] for c in c3.calls] == [S.MODEL_WRITER, S.MODEL_REVIEWER, S.MODEL_REVIEWER_2]
    assert c3.calls[1]["system"] == c3.calls[2]["system"]  # 1차와 같은 시스템 프롬프트·스키마
    assert c3.calls[1]["output_config"]["format"] == c3.calls[2]["output_config"]["format"]
    assert novel.fake.val(e1, S.N_STATE) == S.NS_APPROVED and novel.fake.val(e1, S.N_RESULT) == "auto:gate2"
    assert novel.fake.val(e1, S.N_VERDICT) == "PASS"
    assert novel_store.json_section(e1, run_novel.H_REVIEW2)["stage"] == "review2"
    assert run_novel.auto_publishable(e1)
    assert not any(t for t in novel.tg.sent if "⚠️" in t)  # 정상 경로 경고·조치 요구 0 (AC1)


def test_gate2_fail_postpones_resets_and_creates_notice(novel, glive, reviewer2_allowed):
    add_canon_bible(novel.fake)
    for h in (10, 12):
        run(kst(2026, 11, 26, h, 44), Scripted([None, {"data": _blk()}]))
    run(kst(2026, 11, 26, 14, 44), Scripted([None, {"data": _blk()}, {"data": _blk()}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED
    assert novel.fake.val(e1, S.N_RESULT) == "final:gate2:BLOCK"
    assert novel.fake.val(e1, S.N_ATTEMPTS) == 0 and novel_store.json_section(e1, novel_policy.H_GATE) == {"repairs": 0}
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-30"
    ctrl = _ctrl(novel.fake)
    assert ctrl["shifts"] == ["2026-11-29"] and ctrl["shift_count"] == {"N1-E01": 1}
    notice = row(novel.fake, "N1-NOTICE-20261129")
    assert novel.fake.val(notice, S.N_KIND) == S.NK_NOTICE and novel.fake.val(notice, S.N_STATE) == S.NS_APPROVED
    assert novel.fake.val(notice, S.N_DUE) == "2026-11-29"
    meta = novel_store.json_section(notice, novel_store.H_META)
    assert meta["kind"] == "hiatus_notice" and (meta["season"], meta["episode"]) == (1, 1)
    sec = novel_store.sections(notice)
    assert "휴재 안내" in sec[novel_store.H_BODY] and "11-30(월)" in sec[novel_store.H_BODY]
    assert sec[novel_store.H_QUOTE] and "다음 공개 11-30(월)" in sec[novel_store.H_SUBTITLE]
    assert any("순연" in t and "조치 불필요" in t for t in novel.tg.sent)
    assert run_novel._retryable(e1)  # 재생성 대상으로 돌아감


def test_block_after_repairs_confirmed_by_reviewer2(novel, glive, reviewer2_allowed):
    """DN2026_0126: 수정 뒤에도 1차 BLOCK → 2차(다른 모델) 확인. 2차가 경고면 경고 승인, 차단 아님."""
    add_canon_bible(novel.fake)
    for h in (10, 12):
        run(kst(2026, 11, 26, h, 44), Scripted([None, {"data": _blk()}]))
    _, c = run(kst(2026, 11, 26, 14, 44), Scripted([None, {"data": _blk()}, {"data": _flag()}]))
    assert [x["model"] for x in c.calls][-1] == S.MODEL_REVIEWER_2
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_RESULT) == "auto:warn:gate2" and novel.fake.val(e1, S.N_VERDICT) == "HUMAN_REQUIRED"
    assert run_novel.auto_publishable(e1)
    assert "shifts" not in _ctrl(novel.fake)


def test_bible_gate2_and_final_hold(novel, reviewer2_allowed):
    for h in (10, 12):
        run(kst(2026, 11, 22, h, 44), Scripted([None, {"data": _blk()}]))
    run(kst(2026, 11, 22, 14, 44), Scripted([None, {"data": _blk()}, {"data": _blk()}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_HELD  # 바이블 최종 실패 = 보류 + T5 (게시일 없음)
    assert any("⚠️ N1-BIBLE 보류" in t for t in novel.tg.sent)


def test_shift_limit_holds_with_t5(novel, glive):
    add_canon_bible(novel.fake)
    _add_ctrl(novel.fake, {"shifts": ["2026-11-29", "2026-11-30"], "shift_count": {"N1-E01": 2}})
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_CHECK_FAILED, due=date(2026, 12, 1))
    _set(e1, **{S.N_ATTEMPTS: 3, S.N_RESULT: "rules:R09:cliche"})
    _, client = run(kst(2026, 11, 28, 10, 44))
    assert novel.fake.val(e1, S.N_STATE) == S.NS_HELD
    t5 = [t for t in novel.tg.sent if "자동 복구 한도 초과" in t]
    assert len(t5) == 1 and "사건 소설 시즌 1 제1화" in t5[0] and "연재 일시 정지. 노션 DB-12 확인 필요" in t5[0]
    ctrl = _ctrl(novel.fake)
    assert ctrl["shifts"] == ["2026-11-29", "2026-11-30"]  # 한도 초과분은 순연하지 않는다
    assert (ctrl["paused"]["season"], ctrl["paused"]["episode"]) == (1, 1) and ctrl["paused"]["since"]
    assert client.calls == []


@pytest.fixture
def glive(novel, env):
    """생성 측 live (리뷰 W2: 놓친 게시일 순연은 live 만). 바이블은 테스트가 넣는다."""
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12,
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page, PAGE76_CLAUDE_KEY=FAKE_CLAUDE_KEY)
    return novel


def test_missed_approved_slot_postponed_only_when_too_late(glive):
    novel = glive
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    e2 = add_episode_row(novel.fake, 1, 2, S.NS_APPROVED, due=date(2026, 11, 30))
    run(kst(2026, 11, 28, 23, 0))   # 미래 날짜는 순연하지 않는다
    run(kst(2026, 11, 29, 11, 40))  # T(12:00)까지 20분 ≥ 15분 — 게시 측이 아직 예약 가능
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-29" and not _ctrl(novel.fake).get("shifts")
    run(kst(2026, 11, 29, 11, 50))  # 10분 < 15분 → 놓침(b)
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-30" and novel.fake.val(e1, S.N_STATE) == S.NS_APPROVED
    assert novel.fake.val(e2, S.N_DUE) == "2026-12-01"  # 이후 미게시 회차 재계산
    # R6-02 P4: 이미 지난(등록 하한을 넘긴) 슬롯에는 곧바로 만료될 안내 행을 만들지 않는다 — shifts 에만 반영
    assert not rows(novel.fake, S.NK_NOTICE)
    assert _ctrl(novel.fake)["shift_count"] == {"N1-E01": 1}


def test_missed_several_days_is_one_shift_event(glive):
    novel = glive
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    run(kst(2026, 12, 1, 9, 0))
    ctrl = _ctrl(novel.fake)
    assert ctrl["shifts"] == ["2026-11-29", "2026-11-30"] and ctrl["shift_count"] == {"N1-E01": 1}
    assert novel.fake.val(e1, S.N_DUE) == "2026-12-01"
    assert not rows(novel.fake, S.NK_NOTICE)  # 지난 슬롯 안내 행 없음(R6-02 P4)


def test_posted_rows_never_shifted(glive):
    novel = glive
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_SCHEDULED)
    run(kst(2026, 11, 29, 13, 0))
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-29" and not _ctrl(novel.fake).get("shifts")


def test_migrate_approved_bible_reviewer2_only(novel, reviewer2_allowed):
    page = add_canon_bible(novel.fake)
    novel_store.update(page, {S.N_STATE: {"select": {"name": S.NS_APPROVED}},
                              S.N_VERDICT: {"select": {"name": "PASS"}}, S.N_MODEL: {"rich_text": [
                                  {"type": "text", "text": {"content": "unverified"}}]}})
    _, client = run(kst(2026, 11, 22, 10, 44))
    assert [c["model"] for c in client.calls] == [S.MODEL_REVIEWER_2]
    assert novel.fake.val(page, S.N_STATE) == S.NS_CANON and novel.fake.val(page, S.N_RESULT) == "auto:migrate"


@pytest.mark.parametrize("state", [S.NS_IMPLICIT, S.NS_EXPLICIT])
def test_migrate_waiting_bible_fail_regenerates(novel, reviewer2_allowed, state):
    page = add_canon_bible(novel.fake)
    novel_store.update(page, {S.N_STATE: {"select": {"name": state}}, S.N_ATTEMPTS: {"number": 3}})
    _, client = run(kst(2026, 11, 22, 10, 44), Scripted([{"data": _flag()}]))
    assert [c["model"] for c in client.calls] == [S.MODEL_REVIEWER_2, S.MODEL_WRITER, S.MODEL_REVIEWER]
    assert novel.fake.val(page, S.N_STATE) == S.NS_CANON and novel.fake.val(page, S.N_ATTEMPTS) == 1


def test_migrate_bible_without_json_regenerates(novel):
    page = novel.fake.add(DB12, **{S.N_KEY: "N1-BIBLE", S.N_KIND: S.NK_BIBLE, S.N_SEASON: 1,
                                   S.N_STATE: S.NS_EXPLICIT})
    _, client = run(kst(2026, 11, 22, 10, 44))
    assert [c["model"] for c in client.calls] == [S.MODEL_WRITER, S.MODEL_REVIEWER]
    assert novel.fake.val(page, S.N_STATE) == S.NS_CANON


def test_migrate_approved_bible_fail_regenerates(novel, reviewer2_allowed):
    """통합 후 전이 표에 '승인'→'생성중' 이 있다(novel_store 1.1.0): 2차 검수 실패 바이블은 보류가 아니라 재생성 경로."""
    page = add_canon_bible(novel.fake)
    novel_store.update(page, {S.N_STATE: {"select": {"name": S.NS_APPROVED}}})
    run(kst(2026, 11, 22, 10, 44), Scripted([{"data": _flag()}]))
    assert novel.fake.val(page, S.N_STATE) != S.NS_HELD
    assert not any("N1-BIBLE 보류" in t for t in novel.tg.sent)


def test_migrate_review_episodes(novel):
    add_canon_bible(novel.fake)
    ok = add_episode_row(novel.fake, 1, 1, S.NS_REVIEW)
    flagged = add_episode_row(novel.fake, 1, 2, S.NS_REVIEW, verdict="HUMAN_REQUIRED")
    _set(flagged, **{S.N_REVIEW_CODES: "identifiability:1;method_detail:0", S.N_ATTEMPTS: 3})
    _, client = run(kst(2026, 11, 26, 10, 44))
    assert novel.fake.val(ok, S.N_STATE) == S.NS_APPROVED and novel.fake.val(ok, S.N_RESULT) == "auto:migrate"
    assert "identifiability:1" in client.calls[0]["messages"][0]["content"][-1]["text"]  # 자동 수정 경로
    assert novel.fake.val(flagged, S.N_STATE) == S.NS_APPROVED and novel.fake.val(flagged, S.N_ATTEMPTS) == 1


def test_budget_save_lowers_write_effort(novel):
    _add_ctrl(novel.fake, {"month": "2026-11", "spent": 48.0})  # 60 × 0.8
    _, client = run(kst(2026, 11, 22, 10, 44))
    assert client.calls[0]["output_config"]["effort"] == S.NOVEL_WRITE_EFFORT_SAVE == "low"
    assert client.calls[1]["output_config"]["effort"] == "medium"  # 검수는 낮추지 않음


def test_budget_below_ratio_keeps_effort(novel):
    _add_ctrl(novel.fake, {"month": "2026-11", "spent": 47.0})
    _, client = run(kst(2026, 11, 22, 10, 44))
    assert client.calls[0]["output_config"]["effort"] == S.NOVEL_WRITE_EFFORT == "medium"


def test_notice_rows_kept_and_not_duplicated(glive):
    novel = glive
    add_canon_bible(novel.fake)
    add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    run(kst(2026, 11, 29, 11, 50))
    run(kst(2026, 11, 29, 11, 55))
    keys = [novel.fake.val(r, S.N_KEY) for r in rows(novel.fake, S.NK_NOTICE)]
    assert keys == []  # 놓친 슬롯에는 안내 행을 만들지 않는다(R6-02 P4), 재실행해도 중복 순연 없음
    assert _ctrl(novel.fake)["shift_count"] == {"N1-E01": 1}


def test_zero_touch_logs_clean(novel, reviewer2_allowed, caplog):
    caplog.set_level(logging.DEBUG)
    add_canon_bible(novel.fake)
    for h in (10, 12):
        run(kst(2026, 11, 26, h, 44), Scripted([None, {"data": _blk()}]))
    run(kst(2026, 11, 26, 14, 44), Scripted([None, {"data": _blk()}, {"data": _blk()}]))
    for secret in ("합성 문장", "표식-NB", "표식-NE", "표식-NR", DB12, novel.prompt_page, "휴재 안내"):
        assert secret not in caplog.text, secret
    assert f"[Novel] v{run_novel.VERSION}" in caplog.text and run_novel.VERSION == "1.6.0"


def test_generation_collect_stops_leaves_fb_scheduled_rows_to_publisher(novel):
    """DN2026_0119: 생성 파이프라인은 발행예고·확인필요 행의 중단 체크를 '중단'으로 옮기지 않는다(FB 예약 취소는 게시 측)."""
    add_canon_bible(novel.fake)
    sched = add_episode_row(novel.fake, 1, 1, S.NS_SCHEDULED)
    check = add_episode_row(novel.fake, 1, 2, S.NS_CHECK)
    plain = add_episode_row(novel.fake, 1, 3, S.NS_APPROVED)
    for page in (sched, check, plain):
        novel_store.update(page, {S.N_STOP: {"checkbox": True}})
    ctx = run_novel.Ctx(RunLog("novel", "t"), kst(2026, 11, 26, 10), novel.fake.rows(DB12), {}, {})
    run_novel.collect_stops(ctx)
    assert novel.fake.val(sched, S.N_STATE) == S.NS_SCHEDULED
    assert novel.fake.val(check, S.N_STATE) == S.NS_CHECK
    assert novel.fake.val(plain, S.N_STATE) == S.NS_STOPPED


# ── 리뷰 C2·W2·W3·W5 (run_novel 1.5.0) ─────────────────────
def _stuck(fake, n, m, state=S.NS_GENERATING):
    """'승인' 전에서 멈춘 회차 — 시도 소진(재시도·자동 복구 대상 아님)."""
    page = add_episode_row(fake, n, m, state)
    _set(page, **{S.N_ATTEMPTS: S.NOVEL_MAX_ATTEMPTS, S.N_RESULT: "rules:R09:cliche"})
    return page


def _notices(fake):
    return sorted(fake.val(r, S.N_KEY) for r in rows(fake, S.NK_NOTICE))


def _t5_pause(tg):
    return [t for t in tg.sent if "자동 복구 한도 초과" in t]


def _days(start, k):
    return [start + timedelta(days=i) for i in range(k)]


def test_c2_stuck_episode_pauses_after_shift_limit(glive):
    """C2 재현: 승인 못 하는 회차 + 8일 실행 → 순연 NOVEL_SHIFT_MAX 회 뒤 일시 정지, 안내 행 증가 멈춤, T5 1회."""
    novel = glive
    add_canon_bible(novel.fake)
    e1 = _stuck(novel.fake, 1, 1)
    e2 = add_episode_row(novel.fake, 1, 2, S.NS_APPROVED, due=date(2026, 11, 30))
    counts = []
    for day in _days(date(2026, 11, 28), 8):
        for h, mi in ((10, 44), (22, 15)):
            run(datetime(day.year, day.month, day.day, h, mi, tzinfo=KST))
        counts.append(len(_notices(novel.fake)))
    ctrl = _ctrl(novel.fake)
    assert ctrl["shift_count"]["N1-E01"] == S.NOVEL_SHIFT_MAX + 1
    assert len(ctrl["shifts"]) == S.NOVEL_SHIFT_MAX == 2
    assert counts[-1] == S.NOVEL_SHIFT_MAX and counts[2:] == [counts[-1]] * 6  # 정지 뒤 안내 행이 늘지 않는다
    assert len(_t5_pause(novel.tg)) == 1 and "시즌 1 제1화" in _t5_pause(novel.tg)[0]
    assert novel.fake.val(e1, S.N_STATE) == S.NS_HELD
    assert (ctrl["paused"]["season"], ctrl["paused"]["episode"]) == (1, 1)
    assert novel.fake.val(e1, S.N_DUE) is None and novel.fake.val(e2, S.N_DUE) is None  # 게시 측 등록 대상 아님
    assert novel.fake.val(e2, S.N_STATE) == S.NS_APPROVED and "paused" not in (novel.fake.val(e2, S.N_RESULT) or "")
    assert sum("순연 —" in t for t in novel.tg.sent) == S.NOVEL_SHIFT_MAX  # T8 은 순연 횟수만큼만


def test_c2_legacy_held_row_never_shifted_pauses_once(glive):
    """v1.4.0 이 남긴 보류 회차(순연 3회, paused 없음) — 순연하지 않고 정지 1회, 8일 동안 안내 행 0."""
    novel = glive
    add_canon_bible(novel.fake)
    _add_ctrl(novel.fake, {"shifts": ["2026-11-29", "2026-11-30"], "shift_count": {"N1-E01": 3}})
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_HELD, due=date(2026, 12, 1))
    for day in _days(date(2026, 12, 1), 8):
        run(datetime(day.year, day.month, day.day, 13, 0, tzinfo=KST))
    ctrl = _ctrl(novel.fake)
    assert ctrl["shifts"] == ["2026-11-29", "2026-11-30"] and ctrl["shift_count"] == {"N1-E01": 3}
    assert _notices(novel.fake) == []
    assert len(_t5_pause(novel.tg)) == 1 and ctrl["paused"]["episode"] == 1
    assert novel.fake.val(e1, S.N_STATE) == S.NS_HELD


@pytest.mark.parametrize("state", [S.NS_HELD, S.NS_REFUSED, S.NS_DISCARDED])
def test_c2_postpone_skips_human_and_discarded_states(glive, state):
    novel = glive
    e1 = add_episode_row(novel.fake, 1, 1, state)
    ctx = run_novel.Ctx(RunLog("novel", "t"), kst(2026, 11, 30, 13), novel.fake.rows(DB12), {}, {})
    run_novel.postpone(ctx, e1, "missed")
    assert not ctx.ctrl.get("shifts") and _notices(novel.fake) == []


def test_c2_paused_stops_generation_for_that_season(glive):
    novel = glive
    add_canon_bible(novel.fake)
    _add_ctrl(novel.fake, {"paused": {"season": 1, "episode": 1, "since": "2026-11-28T13:00:00+00:00"}})
    add_episode_row(novel.fake, 1, 1, S.NS_HELD)
    _, client = run(kst(2026, 11, 28, 10, 44))  # 제2·3화 버퍼 생성 시점이지만 멈춘다
    assert client.calls == [] and not [r for r in rows(novel.fake, S.NK_EPISODE) if novel.fake.val(r, S.N_EPISODE) > 1]


@pytest.mark.parametrize("state", [S.NS_APPROVED, S.NS_GENERATING, S.NS_CHECK_FAILED])
def test_c2_paused_released_when_human_changes_state(glive, state):
    novel = glive
    add_canon_bible(novel.fake)
    _add_ctrl(novel.fake, {"shifts": ["2026-11-29", "2026-11-30"], "shift_count": {"N1-E01": 3},
                           "paused": {"season": 1, "episode": 1, "since": "2026-11-30T13:00:00+00:00"}})
    e1 = add_episode_row(novel.fake, 1, 1, state, due=date(2026, 12, 1))
    novel_store.update(e1, {S.N_DUE: notion.v_date(None)})
    e2 = add_episode_row(novel.fake, 1, 2, S.NS_APPROVED)
    novel_store.update(e2, {S.N_DUE: notion.v_date(None)})
    run(kst(2026, 12, 2, 9, 0), Scripted([{"data": None, "stop": "refusal"}] * 4))
    ctrl = _ctrl(novel.fake)
    assert "paused" not in ctrl and ctrl["shift_count"].get("N1-E01", 0) <= 1  # 해제 + 횟수 초기화 → 정상 흐름
    assert novel.fake.val(e2, S.N_DUE) is not None  # 게시예정일 다시 계산
    assert not _t5_pause(novel.tg)


def test_c2_paused_kept_while_held(glive):
    novel = glive
    _add_ctrl(novel.fake, {"paused": {"season": 1, "episode": 1, "since": "2026-11-30T13:00:00+00:00"}})
    add_episode_row(novel.fake, 1, 1, S.NS_HELD)
    log, _ = run(kst(2026, 12, 2, 9, 0))
    assert _ctrl(novel.fake)["paused"]["episode"] == 1 and "N1-E01:paused" in log.codes


def test_w2_shadow_never_shifts_missed_slots(novel):
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    for day in _days(date(2026, 11, 29), 5):
        run(datetime(day.year, day.month, day.day, 22, 15, tzinfo=KST))
    assert not _ctrl(novel.fake).get("shifts") and _notices(novel.fake) == []
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-29"


def test_w3_preempt_at_d_minus_1_2200(glive):
    novel = glive
    add_canon_bible(novel.fake)
    e1 = _stuck(novel.fake, 1, 1, S.NS_CHECK_FAILED)
    _set(e1, **{S.N_RESULT: "api:x:hold"})  # 재시도·자동 복구 대상 아님
    run(kst(2026, 11, 28, 21, 59))
    assert not _ctrl(novel.fake).get("shifts")
    run(kst(2026, 11, 28, 22, 15))  # 게시일(11-29) D-1 22:00 이후 · 아직 승인 전 → 선제 순연
    assert _ctrl(novel.fake)["shifts"] == ["2026-11-29"]
    notice = row(novel.fake, "N1-NOTICE-20261129")
    assert novel.fake.val(notice, S.N_DUE) == "2026-11-29"  # 안내 행 = 원래 게시일(게시 측이 당일 아침 등록)
    assert novel.fake.val(notice, S.N_STATE) == S.NS_APPROVED
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-30"


def test_w3_approved_not_preempted(glive):
    novel = glive
    add_canon_bible(novel.fake)
    add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    run(kst(2026, 11, 28, 22, 15))
    assert not _ctrl(novel.fake).get("shifts")  # '승인' 은 T-15분 판정만


def test_w5_novel_yml_passes_publish_hhmm():
    doc = yaml.safe_load((ROOT / ".github/workflows/p76_novel.yml").read_text(encoding="utf-8"))
    env = doc["jobs"]["run"]["steps"][-1]["env"]
    assert env["PAGE76_NOVEL_PUBLISH_HHMM"] == "${{ vars.PAGE76_NOVEL_PUBLISH_HHMM }}"



def test_final_fail_not_postponed_in_shadow(novel):
    """통합 결정(W2 확장): shadow 에서는 최종 실패여도 순연·휴재 안내를 만들지 않는다."""
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_CHECK_FAILED)
    ctx = run_novel.Ctx(RunLog("novel", "t"), kst(2026, 11, 26, 10), novel.fake.rows(DB12), {}, {})
    run_novel.postpone(ctx, e1, "final:test")
    assert "shifts" not in ctx.ctrl and not any("NOTICE" in novel.fake.val(r, S.N_KEY) for r in novel.fake.rows(DB12))


def test_pause_withdraws_future_notice_rows(glive):
    """R6-02 N3: 정지 시 이 시즌의 휴재 안내(승인) 행은 중단, 예약된(발행예고) 안내 행은 N_DUE 를 비워 게시 측이 취소."""
    novel = glive
    add_canon_bible(novel.fake)
    a = novel.fake.add(DB12, **{S.N_KEY: "N1-NOTICE-20261205", S.N_KIND: S.NK_NOTICE, S.N_SEASON: 1, S.N_EPISODE: 3,
                                S.N_STATE: S.NS_APPROVED, S.N_DUE: "2026-12-05"})
    b = novel.fake.add(DB12, **{S.N_KEY: "N1-NOTICE-20261206", S.N_KIND: S.NK_NOTICE, S.N_SEASON: 1, S.N_EPISODE: 4,
                                S.N_STATE: S.NS_SCHEDULED, S.N_DUE: "2026-12-06"})
    keep = novel.fake.add(DB12, **{S.N_KEY: "N1-NOTICE-20261203", S.N_KIND: S.NK_NOTICE, S.N_SEASON: 1,
                                   S.N_EPISODE: 2, S.N_STATE: S.NS_APPROVED, S.N_DUE: "2026-12-03"})
    ctx = run_novel.Ctx(RunLog("novel", "t"), kst(2026, 12, 1, 10), novel.fake.rows(DB12),
                        {}, {"paused": {"season": 1, "episode": 3, "since": "2026-12-01T00:00:00+00:00"}})
    run_novel._clear_paused_due(ctx)
    assert novel.fake.val(a, S.N_STATE) == S.NS_STOPPED and novel.fake.val(a, S.N_RESULT) == "paused_notice"
    assert novel.fake.val(b, S.N_STATE) == S.NS_SCHEDULED and not novel.fake.val(b, S.N_DUE)
    assert novel.fake.val(keep, S.N_STATE) == S.NS_APPROVED  # 정지 범위 밖(앞 회차) 안내는 그대로(R6-03)


def test_sync_due_and_stops_skip_claimed_rows(glive):
    """R6-02 N2 / R6-03: 선점 중(posting:) 회차는 생성 측이 날짜·중단을 건드리지 않는다."""
    novel = glive
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_APPROVED, due=date(2026, 11, 28))  # 계산값(11-29)과 다름
    _set(e1, **{S.N_RESULT: "posting:9|h:abcd"})
    novel_store.update(e1, {S.N_STOP: {"checkbox": True}})
    ctx = run_novel.Ctx(RunLog("novel", "t"), kst(2026, 11, 26, 10), novel.fake.rows(DB12), {}, {})
    run_novel.sync_due(ctx)
    run_novel.collect_stops(ctx)
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-28" and novel.fake.val(e1, S.N_STATE) == S.NS_APPROVED


# ── DN2026_0126 R6-03 보강 ───────────────────────────────
@pytest.mark.parametrize("over", [{"graphic_harm": None}, {"graphic_harm": "FAIL"}, {"meta_policy": "HIGH"},
                                  {"meta_policy": None}, {"identifiability": "x"}])
def test_missing_or_unknown_safety_values_block(over):
    r = {**novel_fixtures.review(), **over}
    if over.get("graphic_harm", "x") is None:
        r.pop("graphic_harm")
    assert run_novel.computed_verdict(r) == "BLOCK"
    assert run_novel.computed_verdict({**novel_fixtures.review(), "meta_policy": "MED"}) == "HUMAN_REQUIRED"


def test_warn_notice_failure_is_logged(novel, monkeypatch):
    add_canon_bible(novel.fake)
    monkeypatch.setattr(novel.tg, "post", lambda url, json=None, timeout=None: FakeResp(500, {"ok": False}))
    log, _ = run(kst(2026, 11, 26, 10, 44), Scripted([None, {"data": {**novel_fixtures.review(), "identifiability": 1}}]))
    assert "tg:warn_notice_failed" in log.codes
    assert novel.fake.val(row(novel.fake, "N1-E01"), S.N_RESULT) == "auto:warn:gate1"


def test_bible_non_blocking_rule_is_warn_canon(novel):
    b = novel_fixtures.bible()
    b["risk_notes"] = ["2026-11-29 오후 기준 점검"]  # R02 실제 날짜 = 경고(차단 아님)
    errs = novel_rules.check_bible(b)
    assert errs and not novel_policy.blocking(errs)
    log, _ = run(kst(2026, 11, 22, 10, 44), Scripted([{"data": b}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CANON
    assert "N1-BIBLE:rules_warn" in log.codes

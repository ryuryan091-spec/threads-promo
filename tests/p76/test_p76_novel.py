"""사건 소설(B형) 자동 작성·게시 — TN-01~18 (DSN-P76-NOVEL-002 §10 · DN2026_0072). 합성 데이터만 쓴다."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from p76_fake import DB05, DB12, FAKE_PAGE_ID, FakeResp, png_bytes

from p76 import (
    novel_fixtures,
    novel_llm,
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
    _, errors = novel_rules.fill_laws("{{LAW_1350}}", laws, date(2026, 11, 29))
    assert errors == ["LAW:unconfirmed:LAW_1350"]
    _, errors = novel_rules.fill_laws("{{LAW_76_2}}", laws, date(2027, 3, 1))  # 90일 초과
    assert errors == ["LAW:unconfirmed:LAW_76_2"]
    foot = novel_rules.footer(1, 2, "질문?")
    for must in ("※ 여러 사례를 재구성한 픽션입니다.", "법률 자문이 아닙니다.", "AI의 도움", "#사건파일76", "💬 질문?"):
        assert must in foot


def test_verdict_recomputed():
    clean = novel_fixtures.review()
    assert run_novel.computed_verdict(clean) == "PASS"
    assert run_novel.computed_verdict({**clean, "identifiability": 1}) == "HUMAN_REQUIRED"
    assert run_novel.computed_verdict({**clean, "method_detail": 2}) == "BLOCK"
    assert run_novel.computed_verdict({**clean, "victim_blame": "fail"}) == "BLOCK"
    assert run_novel._worse("PASS", "HUMAN_REQUIRED") == "HUMAN_REQUIRED"


def test_prompt_page_parse_and_draft_refused(novel, env):
    p = novel_prompts.load()
    assert p.systems["nb"].endswith("표식-NB") and p.schemas["ne"] == NE_SCHEMA
    assert p.laws["LAW_76_2"][1] == date(2026, 11, 1) and p.laws["LAW_1350"][1] is None
    assert p.cliches[0] == "숨이 멎었다" and p.tag("nb").startswith("NB-1.0/")
    env(PAGE76_NOVEL_PROMPT_PAGE_ID=add_prompt_page(novel.fake, title="⚙️ 소설 프롬프트 정본 v1 (초안)"))
    with pytest.raises(novel_prompts.PromptError):
        novel_prompts.load()


# ── TN-01·04 묵시 승인 ───────────────────────────────────
def test_tn01_implicit_registration_after_2h(novel):
    log, client = run(kst(2026, 11, 9, 10, 44))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_IMPLICIT
    assert any("묵시 승인 대기" in t and "12:44 이후" in t and "중단'을 체크" in t for t in novel.tg.sent)
    assert "표식-NB" in json.dumps(client.calls[0]["system"], ensure_ascii=False)
    assert client.calls[0]["fallbacks"] == "default" and client.calls[0]["model"] == S.MODEL_WRITER
    run(kst(2026, 11, 9, 12, 40))
    assert novel.fake.val(bible, S.N_STATE) == S.NS_IMPLICIT  # 2시간 전
    run(kst(2026, 11, 9, 12, 44))
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CANON
    assert any("정본 등록" in t for t in novel.tg.sent)
    assert all(t.startswith("[사건파일76·소설] ") for t in novel.tg.sent)


def test_tn04_night_bible_waits_for_daytime_notice(novel):
    run(kst(2026, 11, 9, 22, 15))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_NOTIFIED_AT) is None and novel.tg.sent == []
    run(kst(2026, 11, 10, 8, 50))
    assert novel.fake.val(bible, S.N_NOTIFIED_AT) is None
    run(kst(2026, 11, 10, 10, 44))
    assert novel.fake.val(bible, S.N_NOTIFIED_AT) is not None
    assert novel.fake.val(bible, S.N_STATE) == S.NS_IMPLICIT
    run(kst(2026, 11, 10, 12, 44))
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CANON


def test_tn05_stale_run_defers(novel):
    run(kst(2026, 11, 9, 10, 44))
    run(kst(2026, 11, 9, 17, 0))  # 직전 실행 6시간 16분 전
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_IMPLICIT
    assert any("연기" in t for t in novel.tg.sent)


def test_tn03_telegram_is_send_only(novel):
    """같은 봇을 쓰는 다른 파이프라인의 update 를 건드리지 않는다 — getUpdates·웹훅 호출 없음."""
    run(kst(2026, 11, 9, 10, 44))
    run(kst(2026, 11, 9, 12, 44))
    assert novel.tg.urls and all(u.endswith("/sendMessage") for u in novel.tg.urls)
    source = "\n".join(p.read_text(encoding="utf-8") for p in (Path(run_novel.__file__).parent).glob("*.py"))
    for method in ("getUpdates", "setWebhook", "deleteWebhook"):
        assert f'"{method}"' not in source and f"'{method}'" not in source  # 호출 인자로 쓰이지 않음


def test_tn01_notice_not_sent_means_no_timer(novel, env):
    env(PAGE76_NOVEL_TG_BOT_TOKEN="")
    run(kst(2026, 11, 9, 10, 44))
    run(kst(2026, 11, 9, 16, 44))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_NOTIFIED_AT) is None
    assert novel.fake.val(bible, S.N_STATE) == S.NS_IMPLICIT


# ── TN-02 중단 (노션 체크·스위치) ───────────────────────
def test_tn02_notion_checkbox_stop(novel):
    run(kst(2026, 11, 9, 10, 44))
    bible = row(novel.fake, "N1-BIBLE")
    bible["properties"][S.N_STOP]["checkbox"] = True
    run(kst(2026, 11, 9, 12, 44))
    assert novel.fake.val(bible, S.N_STATE) == S.NS_STOPPED
    assert any("중단 접수" in t for t in novel.tg.sent)


def test_tn02_stop_after_canon_halts_episodes(novel):
    bible = add_canon_bible(novel.fake)
    bible["properties"][S.N_STOP]["checkbox"] = True
    log, client = run(kst(2026, 11, 15, 10, 44))
    assert novel.fake.val(bible, S.N_STATE) == S.NS_STOPPED
    assert rows(novel.fake, S.NK_EPISODE) == [] and client.calls == []


def test_tn02_global_switch(novel, env):
    env(PAGE76_NOVEL_ENABLED="false")
    run(kst(2026, 11, 9, 10, 44))
    assert not any(DB12 in c[1] for c in novel.fake.calls)


# ── TN-06·08·09 판정·거절 ────────────────────────────────
def test_tn06_human_required_bible_needs_explicit(novel):
    flagged = {**novel_fixtures.review(), "identifiability": 1, "verdict": "PASS"}
    run(kst(2026, 11, 9, 10, 44), Scripted([None, {"data": flagged}, {"data": flagged}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_EXPLICIT
    run(kst(2026, 11, 9, 16, 44))
    assert novel.fake.val(bible, S.N_STATE) == S.NS_EXPLICIT


def test_tn06_fallback_model_forces_human(novel):
    run(kst(2026, 11, 9, 10, 44), Scripted([{"data": novel_fixtures.bible(), "model": "claude-opus-4-8"}]))
    assert novel.fake.val(row(novel.fake, "N1-BIBLE"), S.N_STATE) == S.NS_EXPLICIT


def test_tn08_refusal_no_retry(novel):
    log, client = run(kst(2026, 11, 9, 10, 44), Scripted([{"data": None, "stop": "refusal", "category": "cyber"}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_REFUSED
    assert len(client.calls) == 1


def test_tn09_max_tokens_not_saved(novel):
    run(kst(2026, 11, 9, 10, 44), Scripted([{"data": None, "stop": "max_tokens"}]))
    bible = row(novel.fake, "N1-BIBLE")
    assert novel.fake.val(bible, S.N_STATE) == S.NS_CHECK_FAILED
    assert novel.fake.blocks.get(bible["id"]) is None


def test_review_block_holds_episode(novel):
    add_canon_bible(novel.fake)
    blocked = {**novel_fixtures.review(), "method_detail": 3, "verdict": "BLOCK"}
    run(kst(2026, 11, 15, 10, 44), Scripted([None, {"data": blocked}]))
    assert novel.fake.val(row(novel.fake, "N1-E01"), S.N_STATE) == S.NS_HELD


# ── 회차 생성 ────────────────────────────────────────────
def test_episode_generation_buffer(novel):
    add_canon_bible(novel.fake)
    run(kst(2026, 11, 14, 10, 44))
    assert rows(novel.fake, S.NK_EPISODE) == []  # 제1화 D-14(11-15) 전
    for hour in (10, 12, 14, 16):
        run(kst(2026, 11, 15, hour, 44))
    keys = sorted(novel.fake.val(r, S.N_KEY) for r in rows(novel.fake, S.NK_EPISODE))
    assert keys == ["N1-E01", "N1-E02", "N1-E03"]  # 버퍼 3화, 4화는 D-14 전
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_REVIEW
    assert novel.fake.val(e1, S.N_DUE) == "2026-11-29"
    assert "제1화 합성 문장" in novel.fake.page_text(e1["id"])
    assert any("검토대기" in t for t in novel.tg.sent)


def test_episode_rule_retry_then_fail(novel):
    add_canon_bible(novel.fake)
    bad = {**novel_fixtures.episode({"messages": [{"content": [{"text": "제1화"}]}]}), "body": "D+1 짧은 원고"}
    log, client = run(kst(2026, 11, 15, 10, 44), Scripted([{"data": bad}, {"data": bad}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED
    assert "직전 원고가 다음 규칙에 걸렸다" in client.calls[1]["messages"][0]["content"][-1]["text"]
    run(kst(2026, 11, 15, 12, 44))  # 다음 실행에서 재시도(시도 2/3)
    assert novel.fake.val(e1, S.N_STATE) == S.NS_REVIEW
    assert novel.fake.val(e1, S.N_ATTEMPTS) == 2


def test_tn14_banned_terms_never_sent(novel, env):
    env(PAGE76_BANNED_TERMS="비공개금칙어")
    add_canon_bible(novel.fake)
    _, client = run(kst(2026, 11, 15, 10, 44))
    assert client.calls and "비공개금칙어" not in json.dumps(client.calls, ensure_ascii=False)


# ── TN-12 예산 ───────────────────────────────────────────
def test_tn12_month_budget_blocks_calls(novel):
    novel.fake.add(DB12, **{S.N_KEY: "CONTROL", S.N_KIND: S.NK_CONTROL,
                            S.N_RESULT: json.dumps({"month": "2026-11", "spent": 19.5})})
    log, client = run(kst(2026, 11, 9, 10, 44))
    assert client.calls == [] and "budget:month" in log.codes
    assert any("예산" in t for t in novel.tg.sent)


def test_tn12_run_cap(novel):
    log, client = run(kst(2026, 11, 9, 10, 44), Scripted([{"data": novel_fixtures.bible(), "out": 160_000}]))
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
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page, PAGE76_CLAUDE_KEY=FAKE_CLAUDE_KEY)
    add_canon_bible(novel.fake)
    novel.graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = FakeResp(200, {"id": "p1", "post_id": "61_99"})
    return novel


def pub(when):
    log = RunLog("novel_publish", "t")
    run_novel_publish.body(log, now=when)
    return log


def test_tn17_notice_then_publish_after_2h(live):
    e1 = add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    pub(kst(2026, 11, 29, 7, 50))
    assert live.fake.val(e1, S.N_STATE) == S.NS_APPROVED and live.tg.sent == []  # 08시 전 예고 없음
    pub(kst(2026, 11, 29, 8, 17))
    assert live.fake.val(e1, S.N_STATE) == S.NS_SCHEDULED
    assert any("제1화 — 10:17 이후 발행됩니다." in t and "중단'을 체크" in t for t in live.tg.sent)
    assert live.graph.writes() == []
    pub(kst(2026, 11, 29, 10, 10))
    assert live.graph.writes() == []  # 예고 + 2시간 전
    pub(kst(2026, 11, 29, 10, 21))
    assert live.fake.val(e1, S.N_STATE) == S.NS_PUBLISHED
    assert live.fake.val(e1, S.N_FB_ID) == "61_99"
    caption = live.graph.writes()[0][2]["caption"]
    assert "제1화 합성 문장" in caption and "#사건소설" in caption and "AI의 도움" in caption
    pub(kst(2026, 11, 29, 11, 25))
    assert len(live.graph.writes()) == 1  # 하루 1건·중복 없음
    assert sum("발행됩니다" in t for t in live.tg.sent) == 1


def test_publish_at_not_before_10():
    ctx = SimpleNamespace(now=kst(2026, 11, 29, 7, 0), today=date(2026, 11, 29))
    assert run_novel_publish.publish_at(ctx) == kst(2026, 11, 29, 10, 0)
    ctx = SimpleNamespace(now=kst(2026, 11, 29, 10, 21), today=date(2026, 11, 29))
    assert run_novel_publish.publish_at(ctx) == kst(2026, 11, 29, 12, 21)


def test_tn07_auto_publish_pass_only(live):
    add_episode_row(live.fake, 1, 1, S.NS_REVIEW, verdict="HUMAN_REQUIRED")
    pub(kst(2026, 11, 29, 8, 17))
    assert live.tg.sent == []  # 검수 플래그 회차는 승인 없이 예고하지 않는다
    add_episode_row(live.fake, 1, 2, S.NS_REVIEW, due=date(2026, 11, 29), model="claude-opus-4-8")
    pub(kst(2026, 11, 29, 8, 20))
    assert live.tg.sent == []  # 요청 모델이 아닌 응답(fallback)도 자동 대상 아님
    e3 = add_episode_row(live.fake, 1, 3, S.NS_REVIEW, due=date(2026, 11, 29))
    pub(kst(2026, 11, 29, 8, 25))
    assert live.fake.val(e3, S.N_STATE) == S.NS_SCHEDULED  # PASS + 요청 모델 → 승인 없이 예고
    pub(kst(2026, 11, 29, 10, 25))
    assert live.fake.val(e3, S.N_STATE) == S.NS_PUBLISHED


def test_tn07_code_cannot_approve_or_skip_notice(live):
    e1 = add_episode_row(live.fake, 1, 1, S.NS_REVIEW)
    with pytest.raises(novel_store.TransitionError):
        novel_store.transition(e1, S.NS_APPROVED)
    with pytest.raises(novel_store.TransitionError):
        novel_store.transition(e1, S.NS_PUBLISHED)  # 예고 없이 게시 불가


def test_tn02_stop_after_notice_blocks_publish(live):
    e1 = add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    pub(kst(2026, 11, 29, 8, 17))
    e1["properties"][S.N_STOP]["checkbox"] = True  # 회장이 2시간 안에 중단
    pub(kst(2026, 11, 29, 10, 21))
    assert live.graph.writes() == []
    assert live.fake.val(e1, S.N_STATE) == S.NS_STOPPED


def test_tn02_stop_rechecked_right_before_post(live, monkeypatch):
    """목록을 읽은 뒤 게시 직전 사이에 체크한 중단도 막는다(행을 다시 읽음)."""
    import copy
    e1 = add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    pub(kst(2026, 11, 29, 8, 17))
    stale = copy.deepcopy(live.fake.rows(DB12))  # 실행 시작 때 읽은 목록(중단 전)
    monkeypatch.setattr(novel_store, "all_rows", lambda: copy.deepcopy(stale))
    e1["properties"][S.N_STOP]["checkbox"] = True  # 그 뒤 회장이 체크
    pub(kst(2026, 11, 29, 10, 21))
    assert live.graph.writes() == []


def test_tn01_notice_not_sent_means_no_publish(live, env):
    env(PAGE76_MODE="live", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_TG_BOT_TOKEN="", PAGE76_NOVEL_PROMPT_PAGE_ID=live.prompt_page)
    e1 = add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    log = pub(kst(2026, 11, 29, 8, 17))
    assert "tg:notice_failed" in log.codes and live.fake.val(e1, S.N_STATE) == S.NS_APPROVED
    pub(kst(2026, 11, 29, 12, 34))
    assert live.graph.writes() == []


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


def test_tn17_edited_violation_holds(live):
    e1 = add_episode_row(live.fake, 1, 1, S.NS_APPROVED, body=_body("\n2026-11-29 오후였다."))
    pub(kst(2026, 11, 29, 8, 17))
    pub(kst(2026, 11, 29, 10, 21))
    assert live.graph.writes() == [] and live.fake.val(e1, S.N_STATE) == S.NS_HELD
    assert "R02:real_date" in live.fake.val(e1, S.N_RESULT)
    pub(kst(2026, 11, 29, 11, 25))
    assert sum("게시 전 검사 실패" in t for t in live.tg.sent) == 1  # 재시도 실행마다 반복하지 않음
    e1["properties"][S.N_STATE]["select"] = {"name": S.NS_APPROVED}  # 회장이 고친 뒤 다시 승인
    pub(kst(2026, 11, 29, 12, 34))
    assert live.fake.val(e1, S.N_STATE) == S.NS_SCHEDULED
    assert sum("이후 발행됩니다" in t for t in live.tg.sent) == 2


def test_tn17_unconfirmed_law_blocks(live):
    add_episode_row(live.fake, 1, 1, S.NS_APPROVED, body=_body("\n{{LAW_1350}}"))
    pub(kst(2026, 11, 29, 8, 17))
    pub(kst(2026, 11, 29, 10, 21))
    assert live.graph.writes() == []
    assert any("LAW:unconfirmed:LAW_1350" in t for t in live.tg.sent)


def test_tn17_confirmed_law_substituted(live):
    add_episode_row(live.fake, 1, 1, S.NS_APPROVED, body=_body("\n{{LAW_76_2}}"))
    pub(kst(2026, 11, 29, 8, 17))
    pub(kst(2026, 11, 29, 10, 21))
    caption = live.graph.writes()[0][2]["caption"]
    assert "법률 블록 76의2 합성 문안" in caption and "{{" not in caption


def test_publish_failure_retries_next_slot(live):
    e1 = add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    live.graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = FakeResp(400, {"error": {"code": 100, "message": "합성 오류"}})
    pub(kst(2026, 11, 29, 8, 17))
    pub(kst(2026, 11, 29, 10, 21))
    assert live.fake.val(e1, S.N_STATE) == S.NS_SCHEDULED
    live.graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = FakeResp(200, {"id": "p1", "post_id": "61_99"})
    pub(kst(2026, 11, 29, 11, 25))
    assert live.fake.val(e1, S.N_STATE) == S.NS_PUBLISHED


def test_remind_day_before(live):
    add_episode_row(live.fake, 1, 1, S.NS_REVIEW)
    add_episode_row(live.fake, 1, 2, S.NS_REVIEW, due=date(2026, 11, 29), verdict="HUMAN_REQUIRED")
    run(kst(2026, 11, 28, 20, 25))
    assert any("자동 발행됩니다" in t and "N1-E01" in t and "N1-E02" not in t for t in live.tg.sent)
    assert any("자동 발행 대상이 아님" in t and "N1-E02" in t for t in live.tg.sent)


# ── TN-13·15·16 로그·멱등·모드 ───────────────────────────
def test_tn16_dry_run_writes_nothing(novel, env):
    env(PAGE76_MODE="dry_run", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12,
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page)
    log = RunLog("novel", "t")
    run_novel.body(log, now=kst(2026, 11, 9, 10, 44))  # client=None → FixtureClient (키 없이)
    assert novel.fake.writes() == [] and novel.tg.sent == []


def test_tn16_shadow_never_posts(novel, env):
    env(PAGE76_MODE="shadow", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page)
    add_canon_bible(novel.fake)
    add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    pub(kst(2026, 11, 29, 8, 17))
    pub(kst(2026, 11, 29, 10, 21))
    assert novel.graph.writes() == []
    assert any("shadow — 실제 게시 없음" in t for t in novel.tg.sent)


def test_shadow_notice_never_counts_for_live(novel, env):
    """shadow 예고("실제 게시 없음")만 받은 회차를 같은 날 live 로 바꾸면 live 예고 + 새 2시간 뒤에 게시."""
    base = {"PAGE76_NOVEL_ENABLED": "true", "PAGE76_DB12_ID": DB12, "PAGE76_NOVEL_PUBLISH_ENABLED": "true",
            "PAGE76_NOVEL_TG_BOT_TOKEN": FAKE_NOVEL_BOT, "PAGE76_NOVEL_TG_CHAT_ID": CHAT,
            "PAGE76_NOVEL_PROMPT_PAGE_ID": novel.prompt_page}
    env(PAGE76_MODE="shadow", **base)
    add_canon_bible(novel.fake)
    e1 = add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    novel.graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = FakeResp(200, {"id": "p1", "post_id": "61_99"})
    pub(kst(2026, 11, 29, 8, 17))
    assert novel.fake.val(e1, S.N_STATE) == S.NS_SCHEDULED
    env(PAGE76_MODE="live", **base)
    pub(kst(2026, 11, 29, 10, 21))
    assert novel.graph.writes() == []  # shadow 타이머로 게시하지 않음
    assert any("12:21 이후 발행됩니다." in t for t in novel.tg.sent)  # live 예고 다시
    pub(kst(2026, 11, 29, 11, 25))
    assert novel.graph.writes() == []  # 새 2시간 전
    pub(kst(2026, 11, 29, 12, 34))
    assert novel.fake.val(e1, S.N_STATE) == S.NS_PUBLISHED


def test_tn16_publish_dry_run_writes_nothing(novel, env):
    env(PAGE76_MODE="dry_run", PAGE76_NOVEL_ENABLED="true", PAGE76_DB12_ID=DB12, PAGE76_NOVEL_PUBLISH_ENABLED="true",
        PAGE76_NOVEL_TG_BOT_TOKEN=FAKE_NOVEL_BOT, PAGE76_NOVEL_TG_CHAT_ID=CHAT,
        PAGE76_NOVEL_PROMPT_PAGE_ID=novel.prompt_page)
    add_canon_bible(novel.fake)
    add_episode_row(novel.fake, 1, 1, S.NS_APPROVED)
    pub(kst(2026, 11, 29, 8, 17))
    assert novel.fake.writes() == [] and novel.tg.sent == [] and novel.graph.writes() == []


def test_tn15_rerun_same_slot_is_idempotent(novel):
    run(kst(2026, 11, 9, 10, 44))
    run(kst(2026, 11, 9, 10, 44))
    assert len(rows(novel.fake, S.NK_BIBLE)) == 1
    assert len([r for r in rows(novel.fake) if novel.fake.val(r, S.N_KEY) == "CONTROL"]) == 1


def test_tn13_logs_clean(live, caplog):
    caplog.set_level(logging.DEBUG)
    add_episode_row(live.fake, 1, 1, S.NS_APPROVED)
    run(kst(2026, 11, 15, 12, 44))  # 생성(제2화)
    pub(kst(2026, 11, 29, 8, 17))   # 예고
    pub(kst(2026, 11, 29, 10, 21))  # 게시
    text = caplog.text
    for secret in ("합성 문장", "표식-NB", "표식-NE", "표식-NR", DB12, live.prompt_page, FAKE_CLAUDE_KEY,
                   FAKE_NOVEL_BOT, CHAT, "법률 블록 76의2"):
        assert secret not in text, secret


def test_season_dates():
    assert run_novel.season_start(1) == S.NOVEL_START
    assert run_novel.due_date(1, 7) == S.NOVEL_START + timedelta(weeks=6)
    assert run_novel.season_start(2) == S.NOVEL_START + timedelta(weeks=7)


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
    _, client = run(kst(2026, 11, 15, 10, 44), Scripted([{"data": _ep(card_quote="비공개금칙어 한 줄")}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_HELD
    assert novel.fake.val(e1, S.N_RESULT) == "rules:R03"
    assert len(client.calls) == 1  # 재작성·검수 호출 없음


def test_public_field_violation_retries_then_fails(novel):
    add_canon_bible(novel.fake)
    bad = _ep(subtitle="서울의 밤")
    log, client = run(kst(2026, 11, 15, 10, 44), Scripted([{"data": bad}, {"data": bad}]))
    e1 = row(novel.fake, "N1-E01")
    assert novel.fake.val(e1, S.N_STATE) == S.NS_CHECK_FAILED
    assert "R04:place@subtitle" in novel.fake.val(e1, S.N_RESULT)
    assert "R04:place@subtitle" in client.calls[1]["messages"][0]["content"][-1]["text"]


def test_review_input_includes_public_fields(novel):
    add_canon_bible(novel.fake)
    ep = _ep(card_quote="합성 표지 인용 표식", subtitle="합성 부제 표식", question="합성 질문 표식?")
    _, client = run(kst(2026, 11, 15, 10, 44), Scripted([{"data": ep}]))
    review_call = json.dumps(client.calls[1], ensure_ascii=False)
    for text in ("합성 표지 인용 표식", "합성 부제 표식", "합성 질문 표식?"):
        assert text in review_call
    assert novel.fake.val(row(novel.fake, "N1-E01"), S.N_STATE) == S.NS_REVIEW


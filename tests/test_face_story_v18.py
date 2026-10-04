"""v1.8.0 Facebook 숏폼 스토리 연속성(Notion 회차 원장) 테스트 — 상세설계 v1.0 (2026-10-04).

conftest 의 legacy 프로필을 적용하지 않는다(SAFETY_MODULES). 네트워크는 쓰지 않는다(Notion·Facebook 가짜).

  A. 순수 함수 — 복사 판정 · 연속성 계약 · 기록 변환 · 원장 행 → 맥락
  B. Ledger — 질의 payload · 429 · 오류 · upsert 멱등 · 재조정 조회 · load_context
  C. 대본 계약 — validate · write_script (계약 없음 = v1.7.0 과 같음)
  D. build 러너 — 원장 읽기 · 연속성 전달 · manifest
  E. publish 러너 — 원장 기록 4경로 · 재조정 · 원장 장애 격리 · DRY_RUN · 스위치
  F. 구성 — config · 워크플로 · verify_repo
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import pathlib
import sys
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import requests
import yaml

from src import config, face_client, face_story, mood_source, safety, shorts_plan
from src.video import hooks, script_writer

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
sys.path.insert(0, str(ROOT / "scripts"))
KST = ZoneInfo("Asia/Seoul")
REAL_LEDGER = face_story.Ledger   # 픽스처가 가짜로 바꾸기 전 원본

SUMMARY = "GOC 가 성벽 위에서 흔들리는 도시를 내려다보며 지켜야 할 것이 무엇인지 조용히 되묻는 하루였습니다"
THREAD = "성벽 아래에서 들려온 낯선 종소리의 정체는 무엇일까"
NEW_CLUE = "종소리는 오래된 금고 쪽에서 울렸다는 단서가 나왔다"
RESOLUTION = "종소리는 금고를 지키던 경보였고 도시는 무사했다"


@pytest.fixture
def cfg(monkeypatch):
    base = {
        "AUTOMATION_ENABLED": False, "WARMUP_UNTIL": "", "DAILY_POST_BUDGET": 2,
        "SHORTS_BUILD_ENABLED": False, "SHORTS_THREADS_ENABLED": False, "FACE_ENABLED": False,
        "FACE_RAMP_START": "", "FACE_DAILY_MAX": 3, "SHORTS_WEEKLY_REST_DAYS": 0,
        "PUBLISH_WEEKLY_REST_DAYS": 0, "FACE_STORY_ENABLED": False, "FACE_NOTION_DB_ID": "",
        "FACE_STORY_LOOKBACK": 3, "FACE_THREAD_MAX_EPISODES": 5,
    }
    for key, value in base.items():
        monkeypatch.setattr(config, key, value)

    def apply(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setattr(config, key, value)

    return apply


# ---------------------------------------------------------------------------
# Notion 행 가짜
# ---------------------------------------------------------------------------


def _txt(t):
    return {"type": "rich_text", "rich_text": [{"plain_text": t}] if t else []}


def _row(cid, *, status="게시완료", fmt="F1", series=None, summary=SUMMARY, state="없음",
         thread="", tid="", age=None, page="p"):
    return {
        "id": f"{page}-{cid}",
        "properties": {
            "회차ID": {"type": "title", "title": [{"plain_text": cid}]},
            "상태": {"type": "select", "select": {"name": status}},
            "포맷": {"type": "select", "select": {"name": fmt}},
            "시리즈회차": {"type": "number", "number": series},
            "줄거리요약": _txt(summary),
            "떡밥상태": {"type": "select", "select": {"name": state} if state else None},
            "떡밥": _txt(thread),
            "떡밥ID": _txt(tid),
            "떡밥경과": {"type": "number", "number": age},
            "FB영상ID": _txt("v-" + cid),
        },
    }


# ---------------------------------------------------------------------------
# A. 순수 함수
# ---------------------------------------------------------------------------


class TestPure:
    def test_is_copy(self):
        assert face_story.is_copy(THREAD, THREAD)
        assert face_story.is_copy(THREAD + " 결국 밝혀졌다", THREAD)       # 포함 = 복사
        assert face_story.is_copy("성벽 아래에서 들려온, 낯선 종소리의 정체는 무엇일까?", THREAD)
        assert not face_story.is_copy(NEW_CLUE, THREAD)
        assert not face_story.is_copy("", THREAD)
        # 리뷰 #1·#2-7: 앞부분만 잘라 쓴 부분 복사, 어미만 바꾼 근접 복사도 복사로 본다
        assert face_story.is_copy("성벽 아래에서 들려온 낯선 종소리의 정체는", THREAD)
        assert face_story.is_copy("성벽 아래에서 들려온 낯선 종소리의 정체는 무엇이었을까", THREAD)
        assert not face_story.is_copy(RESOLUTION, THREAD)

    def test_caption_hash_stable(self):
        assert face_story.caption_hash(" a ") == face_story.caption_hash("a")
        assert len(face_story.caption_hash("a")) == 16

    def test_content_date(self):
        assert face_story.content_date("sv-20261005-2") == dt.date(2026, 10, 5)
        assert face_story.content_date("sv-20261399-1") is None
        assert face_story.content_date("x") is None

    def test_request_none_when_story_off(self):
        assert face_story.request_for(None, "F1") is None

    def test_request_unavailable_only_summary(self):
        req = face_story.request_for(face_story.StoryContext.unavailable("x"), "F1")
        assert req.allowed_actions() == ("NONE",) and not req.allow_thread
        assert "지난 이야기" not in req.prompt_block and "continuity" in req.prompt_block

    def test_request_f2_only_summary(self):
        ctx = face_story.StoryContext(True, open_thread=face_story.OpenThread("th-a", THREAD, 1))
        req = face_story.request_for(ctx, "F2")
        assert req.allowed_actions() == ("NONE",) and req.open_thread is None

    def test_request_first_episode(self):
        req = face_story.request_for(face_story.StoryContext(True, next_series_no=1), "F1")
        assert req.allowed_actions() == ("OPEN", "NONE")
        assert "시리즈 첫 화" in req.prompt_block and "OPEN|NONE" in req.prompt_block

    def test_request_with_thread_and_force(self, cfg):
        eps = (face_story.PastEpisode("sv-20261004-1", 4, SUMMARY),)
        ctx = face_story.StoryContext(True, eps, face_story.OpenThread("th-x", THREAD, 2), 5)
        req = face_story.request_for(ctx, "F1")
        assert req.allowed_actions() == ("PROGRESS", "RESOLVE")
        assert "시리즈 5화 예정" in req.prompt_block and "4화: " + SUMMARY in req.prompt_block
        assert "[th-x] (2회차 경과)" in req.prompt_block
        cfg(FACE_THREAD_MAX_EPISODES=2)
        forced = face_story.request_for(ctx, "F1")
        assert forced.force_resolve and forced.allowed_actions() == ("RESOLVE",)
        assert "반드시 회수" in forced.prompt_block

    @pytest.mark.parametrize("action,thread_text,resolution,open_thread,force,ok", [
        ("OPEN", THREAD, "", None, False, True),
        ("NONE", "", "", None, False, True),
        ("OPEN", "짧다", "", None, False, False),                 # 길이
        ("PROGRESS", NEW_CLUE, "", None, False, False),           # 열린 떡밥 없음 → 불가
        ("PROGRESS", NEW_CLUE, "", THREAD, False, True),
        ("PROGRESS", THREAD, "", THREAD, False, False),           # 같은 문장
        ("RESOLVE", "", RESOLUTION, THREAD, False, True),
        ("RESOLVE", "", THREAD, THREAD, False, False),            # 복사형 회수
        ("RESOLVE", "", "", THREAD, False, False),                # 회수 내용 없음
        ("NONE", "", "", THREAD, False, False),                   # 열린 떡밥 방치
        ("OPEN", THREAD, "", THREAD, False, False),               # 열린 떡밥 있는데 새로 열기
        ("PROGRESS", NEW_CLUE, "", THREAD, True, False),          # 강제 회수
        ("RESOLVE", "", RESOLUTION, THREAD, True, True),
        ("BOGUS", "", "", None, False, False),
    ])
    def test_validate_matrix(self, action, thread_text, resolution, open_thread, force, ok):
        ot = face_story.OpenThread("th-x", open_thread, 1) if open_thread else None
        req = face_story.ContinuityRequest("F1", "", ot, force, True)
        raw = {"summary": SUMMARY, "thread_action": action, "thread_text": thread_text,
               "resolution": resolution}
        assert (face_story.validate_continuity(raw, req) == []) is ok

    def test_validate_missing_and_summary_length(self):
        req = face_story.ContinuityRequest("F2", "", None, False, False)
        assert face_story.validate_continuity(None, req) == ["continuity 객체 없음"]
        issues = face_story.validate_continuity({"summary": "짧음", "thread_action": "NONE"}, req)
        assert any("summary" in i for i in issues)

    def test_build_continuity_mapping(self):
        ctx = face_story.StoryContext(True, open_thread=face_story.OpenThread("th-a", THREAD, 3),
                                      next_series_no=0)
        req = face_story.request_for(ctx, "F1")
        prog = face_story.build_continuity({"summary": SUMMARY, "thread_action": "progress",
                                            "thread_text": NEW_CLUE}, req, ctx, "sv-20261005-1")
        assert prog["thread_state"] == "PROGRESSED" and prog["thread_id"] == "th-a"
        assert prog["thread_age"] == 3 and prog["series_no"] == 0          # 0 보존
        res = face_story.build_continuity({"summary": SUMMARY, "thread_action": "RESOLVE",
                                           "resolution": RESOLUTION}, req, ctx, "sv-20261005-1")
        assert res["thread_state"] == "RESOLVED" and res["thread_text"] == THREAD
        assert res["resolution"] == RESOLUTION
        fresh = face_story.StoryContext(True, next_series_no=1)
        op = face_story.build_continuity({"summary": SUMMARY, "thread_action": "OPEN", "thread_text": THREAD},
                                         face_story.request_for(fresh, "F1"), fresh, "sv-20261005-1")
        assert op["thread_id"] == "th-sv-20261005-1" and op["thread_age"] == 0 and op["series_no"] == 1
        f2 = face_story.build_continuity({"summary": SUMMARY, "thread_action": "NONE"},
                                         face_story.request_for(fresh, "F2"), fresh, "sv-20261005-2")
        assert f2["series_no"] is None and f2["thread_state"] == "없음"

    def test_record_properties(self):
        item = {"content_id": "sv-20261005-1", "fmt": "F1", "hook_type": "A", "themes": ["금리", "a,b"],
                "caption": "c", "continuity": {"summary": SUMMARY, "series_no": 0, "thread_id": "th-x",
                                               "thread_text": THREAD, "thread_state": "OPEN", "thread_age": 0,
                                               "resolution": ""}}
        rec = face_story.record_from_item(item, "게시완료", video_id="v1",
                                          error="token EAA" + "x" * 40)
        props = face_story.record_properties(rec)
        assert props["회차ID"]["title"][0]["text"]["content"] == "sv-20261005-1"
        assert props["날짜"]["date"]["start"] == "2026-10-05"
        assert props["시리즈회차"]["number"] == 0 and props["떡밥경과"]["number"] == 0
        assert props["테마"]["multi_select"][1]["name"] == "a b"
        assert props["FB영상ID"]["rich_text"][0]["text"]["content"] == "v1"
        assert "xxxxxxxxxx" not in json.dumps(props["오류"])            # 토큰 마스킹
        assert set(props) <= set(face_story.SCHEMA)
        bare = face_story.record_properties(face_story.EpisodeRecord("sv-20261005-2", "실패"))
        assert "줄거리요약" not in bare and bare["상태"]["select"]["name"] == "실패"


class TestRound3:
    def test_short_containment_not_copy(self):
        """2차 QC-N2: 짧은 떡밥 단어를 언급한 정상 회수문을 복사로 보지 않는다."""
        assert not face_story.is_copy("종소리는 금고를 지키던 경보였고 도시는 무사했다", "종소리")
        assert face_story.is_copy("종소리", "종소리")

    def test_short_thread_in_ledger_dropped(self):
        rows = [_row("sv-20261003-1", series=3, state="OPEN", thread="종소리", tid="th-sv-20261003-1", age=4)]
        assert face_story.context_from_rows(rows, 3).open_thread is None

    def test_hold_request(self):
        ctx = face_story.StoryContext(True, open_thread=None, next_series_no=3, thread_hold=True)
        req = face_story.request_for(ctx, "F1")
        assert req.hold and req.allowed_actions() == ("NONE",) and "떡밥 확인 대기" in req.prompt_block
        raw = {"summary": SUMMARY, "thread_action": "OPEN", "thread_text": THREAD}
        assert face_story.validate_continuity(raw, req)
        cont = face_story.build_continuity({"summary": SUMMARY, "thread_action": "NONE"}, req, ctx, "sv-20261006-1")
        assert cont["series_no"] == 3 and cont["thread_state"] == "없음"

    def test_403_hint(self):
        with pytest.raises(face_story.LedgerError, match="통합 권한"):
            face_story.Ledger("t", "d", session=_Session(_Resp(403))).memory_rows(3)


class TestContextFromRows:
    def test_empty_is_first_episode(self):
        ctx = face_story.context_from_rows([], 3)
        assert ctx.available and ctx.next_series_no == 1 and ctx.open_thread is None and not ctx.episodes

    def test_summaries_and_series(self):
        rows = [_row("sv-20261005-1", series=7), _row("sv-20261004-1", series=6),
                _row("sv-20261003-1", series=5), _row("sv-20261002-1", series=4)]
        ctx = face_story.context_from_rows(rows, 3)
        assert [e.series_no for e in ctx.episodes] == [7, 6, 5] and ctx.next_series_no == 8

    def test_missing_number_counts_newer_rows(self):
        rows = [_row("sv-20261006-1", series=None), _row("sv-20261005-1", series=None),
                _row("sv-20261004-1", series=3)]
        assert face_story.context_from_rows(rows, 3).next_series_no == 6
        # 번호 있는 행이 하나도 없으면 대상 행 수 + 1 (리뷰 #1-D3·#2-6)
        rows = [_row("sv-20261003-1", series=None), _row("sv-20261002-1", series=None)]
        assert face_story.context_from_rows(rows, 3).next_series_no == 3

    def test_zero_series_preserved(self):
        assert face_story.context_from_rows([_row("sv-20261005-1", series=0)], 3).next_series_no == 1

    def test_ignores_other_formats_and_status(self):
        rows = [_row("sv-20261006-2", fmt="F2", series=99), _row("sv-20261006-1", status="실패", series=50),
                _row("sv-20261004-1", series=2)]
        ctx = face_story.context_from_rows(rows, 3)
        assert ctx.next_series_no == 3 and [e.content_id for e in ctx.episodes] == ["sv-20261004-1"]

    def test_pending_with_video_occupies_number_not_memory(self):
        """리뷰 #2-1: 확인필요(FB영상ID 있음)는 번호·떡밥 계산에 넣고 요약은 기억에 넣지 않는다."""
        pend = _row("sv-20261006-1", status="확인필요", series=3, state="RESOLVED", thread=THREAD, tid="th-a",
                    age=2)
        rows = [pend, _row("sv-20261005-1", series=2, state="OPEN", thread=THREAD, tid="th-a", age=0)]
        ctx = face_story.context_from_rows(rows, 3)
        assert ctx.next_series_no == 4 and ctx.open_thread is None
        assert [e.content_id for e in ctx.episodes] == ["sv-20261005-1"]
        pend["properties"]["FB영상ID"] = _txt("")            # 영상ID 없는 확인필요는 빼고 계산
        ctx = face_story.context_from_rows(rows, 3)
        assert ctx.next_series_no == 3 and ctx.open_thread.thread_id == "th-a"

    def test_today_rows_excluded(self):
        """리뷰 #2-8: 같은 날 build 재실행이 오늘 회차를 지난 이야기로 읽지 않는다."""
        rows = [_row("sv-20261005-1", series=2), _row("sv-20261004-1", series=1)]
        ctx = face_story.context_from_rows(rows, 3, today=dt.date(2026, 10, 5))
        assert ctx.next_series_no == 2 and [e.content_id for e in ctx.episodes] == ["sv-20261004-1"]

    def test_lint_drops_bad_summary(self):
        rows = [_row("sv-20261005-1", series=2, summary="오늘 지수가 3% 올랐다는 이야기를 길게 이어서 전합니다"),
                _row("sv-20261004-1", series=1)]
        ctx = face_story.context_from_rows(rows, 3)
        assert [e.content_id for e in ctx.episodes] == ["sv-20261004-1"]

    def test_thread_age_counts_none_days(self):
        rows = [_row("sv-20261007-1", series=3),                                 # 원장 장애일(NONE)
                _row("sv-20261006-1", series=2, state="PROGRESSED", thread=NEW_CLUE, tid="th-sv-20261005-1",
                     age=1),
                _row("sv-20261005-1", series=1, state="OPEN", thread=THREAD, tid="th-sv-20261005-1", age=0)]
        ctx = face_story.context_from_rows(rows, 3)
        assert ctx.open_thread == face_story.OpenThread("th-sv-20261005-1", NEW_CLUE, 3)

    def test_resolved_closes_thread(self):
        rows = [_row("sv-20261006-1", state="RESOLVED", thread=THREAD, tid="th-a", age=2),
                _row("sv-20261005-1", state="OPEN", thread=THREAD, tid="th-a", age=0)]
        assert face_story.context_from_rows(rows, 3).open_thread is None

    def test_bad_thread_row_dropped(self):
        rows = [_row("sv-20261005-1", state="OPEN", thread="삼성 이야기는 어디로 갈까 궁금하다", tid="th-a", age=0)]
        assert face_story.context_from_rows(rows, 3).open_thread is None
        rows = [_row("sv-20261005-1", state="OPEN", thread=THREAD, tid="", age=0)]
        assert face_story.context_from_rows(rows, 3).open_thread is None

    def test_missing_age_treated_zero(self):
        rows = [_row("sv-20261005-1", state="OPEN", thread=THREAD, tid="th-a", age=None)]
        assert face_story.context_from_rows(rows, 3).open_thread.age == 1


# ---------------------------------------------------------------------------
# B. Ledger
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TestLedger:
    def test_requires_config(self):
        with pytest.raises(face_story.LedgerError):
            face_story.Ledger("", "db")

    def test_published_rows_payload(self):
        s = _Session(_Resp(payload={"results": [{"id": "1"}]}))
        rows = face_story.Ledger("tok", "db1", session=s).memory_rows(12)
        method, url, kw = s.calls[0]
        assert method == "POST" and url.endswith("/databases/db1/query")
        assert kw["headers"]["Notion-Version"] == "2022-06-28"
        flt = kw["json"]["filter"]["and"]
        assert {"or": [{"property": "상태", "select": {"equals": "게시완료"}},
                       {"property": "상태", "select": {"equals": "확인필요"}}]} in flt
        assert {"property": "포맷", "select": {"equals": "F1"}} in flt
        assert kw["json"]["sorts"][0] == {"property": "날짜", "direction": "descending"}
        assert rows == [{"id": "1"}]

    def test_429_retry_after(self):
        slept = []
        s = _Session(_Resp(429, headers={"Retry-After": "7"}), _Resp(payload={"results": []}))
        face_story.Ledger("t", "d", session=s, sleep=slept.append).memory_rows(3)
        assert slept == [7.0] and len(s.calls) == 2

    def test_retry_after_over_cap_fails_without_sleep(self):
        slept = []
        s = _Session(_Resp(429, headers={"Retry-After": "999"}))
        with pytest.raises(face_story.LedgerError, match="상한"):
            face_story.Ledger("t", "d", session=s, sleep=slept.append).memory_rows(3)
        assert slept == []

    @pytest.mark.parametrize("value", ["nan", "inf", "-5", "x"])
    def test_retry_after_garbage(self, value):
        """리뷰 #1-D4: NaN 등 비정상 Retry-After 가 예외로 새지 않는다."""
        slept = []
        s = _Session(_Resp(503, headers={"Retry-After": value}), _Resp(payload={"results": []}))
        face_story.Ledger("t", "d", session=s, sleep=slept.append).memory_rows(3)
        assert slept == [1.0]

    def test_429_twice_fails(self):
        s = _Session(_Resp(429, headers={"Retry-After": "x"}), _Resp(429))
        with pytest.raises(face_story.LedgerError):
            face_story.Ledger("t", "d", session=s, sleep=lambda x: None).memory_rows(3)

    def test_write_not_retried_on_5xx(self):
        s = _Session(_Resp(payload={"results": []}), _Resp(503))
        with pytest.raises(face_story.LedgerError, match="503"):
            face_story.Ledger("t", "d", session=s, sleep=lambda x: None).upsert(
                face_story.EpisodeRecord("sv-20261005-1", "게시완료"))
        assert len(s.calls) == 2                                    # 생성 POST 는 재시도하지 않음

    @pytest.mark.parametrize("resp", [
        _Resp(200, text="<html>"),                                   # json() 실패
        _Resp(200, payload=None),
    ])
    def test_malformed_200_wrapped(self, resp, monkeypatch):
        """리뷰 #1-D1·#2-4·#3-F2: 비정상 200 응답도 LedgerError 로만 올라간다."""
        if resp.text == "<html>":
            monkeypatch.setattr(resp, "json", mock.Mock(side_effect=ValueError("bad json")))
        with pytest.raises(face_story.LedgerError):
            face_story.Ledger("t", "d", session=_Session(resp)).memory_rows(3)

    def test_non_dict_and_missing_id_wrapped(self, monkeypatch):
        listy = _Resp(200)
        monkeypatch.setattr(listy, "json", lambda: [1, 2])
        with pytest.raises(face_story.LedgerError, match="형식"):
            face_story.Ledger("t", "d", session=_Session(listy)).memory_rows(3)
        with pytest.raises(face_story.LedgerError, match="id"):
            face_story.Ledger("t", "d", session=_Session(_Resp(payload={"results": [{"x": 1}]}))).find("sv-1")
        with pytest.raises(face_story.LedgerError, match="results"):
            face_story.Ledger("t", "d", session=_Session(_Resp(payload={"results": "x"}))).memory_rows(3)

    @pytest.mark.parametrize("status,hint", [(401, "NOTION_TOKEN"), (404, "FACE_NOTION_DB_ID"), (400, "§3")])
    def test_error_hints(self, status, hint):
        s = _Session(_Resp(status, text="secret_" + "a" * 40))
        with pytest.raises(face_story.LedgerError, match=hint):
            face_story.Ledger("t", "d", session=s).memory_rows(3)

    def test_network_error(self):
        s = _Session(requests.ConnectionError("down"))
        with pytest.raises(face_story.LedgerError, match="네트워크"):
            face_story.Ledger("t", "d", session=s).memory_rows(3)

    def test_upsert_create_then_update(self):
        rec = face_story.EpisodeRecord("sv-20261005-1", "게시완료", video_id="v1")
        s = _Session(_Resp(payload={"results": []}), _Resp(payload={"id": "new"}))
        assert face_story.Ledger("t", "db", session=s).upsert(rec) == ("new", "게시완료")
        assert s.calls[0][2]["json"]["filter"] == {"property": "회차ID", "title": {"equals": "sv-20261005-1"}}
        assert s.calls[1][0] == "POST" and s.calls[1][2]["json"]["parent"] == {"database_id": "db"}
        s2 = _Session(_Resp(payload={"results": [{"id": "old"}, {"id": "dup"}]}), _Resp(payload={}))
        assert face_story.Ledger("t", "db", session=s2).upsert(rec) == ("old", "게시완료")
        assert s2.calls[1][0] == "PATCH" and s2.calls[1][1].endswith("/pages/old")
        s3 = _Session(_Resp(payload={}))                                # page_id 를 알면 조회 없이 갱신
        assert face_story.Ledger("t", "db", session=s3).upsert(rec, page_id="pg") == ("pg", "게시완료")
        assert len(s3.calls) == 1 and s3.calls[0][0] == "PATCH"

    @pytest.mark.parametrize("new", ["실패", "확인필요"])
    def test_no_downgrade_from_published(self, new):
        """리뷰 #2-2: 게시완료 행은 재실행의 실패·확인필요로 덮어쓰지 않는다."""
        s = _Session(_Resp(payload={"results": [_row("sv-20261005-1", page="x")]}))
        led = face_story.Ledger("t", "db", session=s)
        assert led.upsert(face_story.EpisodeRecord("sv-20261005-1", new)) == ("x-sv-20261005-1", "게시완료")
        assert len(s.calls) == 1                                        # PATCH 없음
        s2 = _Session(_Resp(payload={"results": [_row("sv-20261005-1", status="실패", page="x")]}),
                      _Resp(payload={}))
        assert face_story.Ledger("t", "db", session=s2).upsert(
            face_story.EpisodeRecord("sv-20261005-1", "게시완료"))[1] == "게시완료"

    def test_pending_and_set_status(self):
        rows = [_row(f"sv-2026100{i}-1", status="확인필요") for i in range(1, 8)]
        s = _Session(_Resp(payload={"results": rows, "has_more": True}), _Resp(payload={}))
        led = face_story.Ledger("t", "db", session=s)
        got = led.pending()
        assert got[0] == ("p-sv-20261001-1", "sv-20261001-1", "v-sv-20261001-1", "")
        assert len(got) == face_story.RECONCILE_MAX_ROWS and led.pending_overflow == 3
        assert s.calls[0][2]["json"]["sorts"] == [{"property": "날짜", "direction": "ascending"}]
        led.set_status("pg", "sv-20261004-1", "게시완료", video_id="v9")
        props = s.calls[1][2]["json"]["properties"]
        assert props["상태"] == {"select": {"name": "게시완료"}} and "FB영상ID" in props

    def test_load_context(self, cfg):
        assert not face_story.load_context("", "db").available
        bad = face_story.load_context("t", "db", session=_Session(_Resp(500, text="x")))
        assert not bad.available and "500" in bad.error
        ok = face_story.load_context("t", "db", session=_Session(_Resp(payload={"results": [
            _row("sv-20261004-1", series=1)]})))
        assert ok.available and ok.next_series_no == 2
        # 응답 내용이 이상해도(파싱 단계 예외) build 는 원장 없이 진행한다
        weird = face_story.load_context("t", "db", session=_Session(_Resp(payload={"results": [
            {"properties": {"포맷": {"type": "select", "select": {"name": "F1"}}, "상태": "깨진값"}}]})))
        assert not weird.available


# ---------------------------------------------------------------------------
# C. 대본 계약
# ---------------------------------------------------------------------------


def _goc_raw():
    return {
        "hook": "시장에 경고등이 켜졌다",
        "body": [
            "금리라는 무게가 오늘도 다시 시장의 어깨를 천천히 누르기 시작했습니다",
            "GOC 는 높은 성벽 위에서 흔들리는 도시의 불빛을 조용히 내려다봅니다",
            "지키는 일은 크게 외치는 일이 아니라 끝까지 자리를 지키는 일입니다",
            "지금 중요한 건 큰 소리가 아니라 끝까지 버티는 자세라고 그녀는 말합니다",
            "물가와 고용 이야기가 같은 날 한꺼번에 겹치면서 시장의 소음이 커집니다",
            "흔들릴수록 지금 무엇을 보고 있는지 차분히 적어 두는 편이 낫다고 그녀는 본다",
            "날개를 접은 GOC 는 아직 지켜야 할 것이 남았다며 다시 앞을 바라봅니다",
        ],
        "closing": "오늘도 성벽은 그대로 서 있습니다",
        "image_prompts": ["guardian heroine watching a city from a wall at dusk"] * config.SHORTS_IMAGE_COUNT,
        "post_caption": "금리 이야기로 무거웠던 하루를 GOC 의 시선으로 정리했습니다. 소음보다 자세를 보자는 이야기입니다.",
    }


def _open_req():
    return face_story.request_for(face_story.StoryContext(True, next_series_no=1), "F1")


class TestScriptContract:
    def test_base_raw_valid(self):
        assert script_writer.validate(_goc_raw(), hooks.HOOK_A, character="GOC") == []

    def test_continuity_required_when_requested(self):
        issues = script_writer.validate(_goc_raw(), hooks.HOOK_A, character="GOC", continuity=_open_req())
        assert "continuity 객체 없음" in issues

    def test_continuity_valid(self):
        raw = _goc_raw() | {"continuity": {"summary": SUMMARY, "thread_action": "OPEN", "thread_text": THREAD}}
        assert script_writer.validate(raw, hooks.HOOK_A, character="GOC", continuity=_open_req()) == []

    def test_continuity_policy(self):
        raw = _goc_raw() | {"continuity": {"summary": SUMMARY.replace("하루였습니다", "하루, 지수는 3% 올랐습니다"),
                                           "thread_action": "OPEN", "thread_text": "EDT 는 어디로 사라졌을까 궁금하다"}}
        issues = script_writer.validate(raw, hooks.HOOK_A, character="GOC", continuity=_open_req())
        assert any("연속성 요약 정책 위반" in i for i in issues)
        assert any("'EDT'" in i for i in issues)

    def test_write_script_without_contract_is_v17(self, monkeypatch):
        seen = {}

        def fake(api_key, prompt, character="EDT"):
            seen["prompt"] = prompt
            return _goc_raw()

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        mood = mood_source.Mood("rss", ("금리",), "관망")
        s = script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1", mood=mood, character="GOC")
        expected = script_writer._user_prompt("F1", None, s.hook_type, mood, [], "GOC", dt.date(2026, 10, 5))
        assert seen["prompt"] == expected
        assert s.continuity is None and "continuity" not in s.to_dict()

    def test_write_script_with_contract(self, monkeypatch):
        prompts = []
        outputs = [_goc_raw(), _goc_raw() | {"continuity": {"summary": SUMMARY, "thread_action": "OPEN",
                                                            "thread_text": THREAD}}]

        def fake(api_key, prompt, character="EDT"):
            prompts.append(prompt)
            return outputs.pop(0)

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        req = _open_req()
        s = script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1",
                                       mood=mood_source.Mood("none"), character="GOC", continuity=req)
        assert req.prompt_block in prompts[0]
        assert "continuity 객체 없음" in prompts[1]                    # 재시도에 위반 사유 전달
        assert s.continuity["thread_action"] == "OPEN" and s.to_dict()["continuity"]["thread_text"] == THREAD


# ---------------------------------------------------------------------------
# D. build 러너
# ---------------------------------------------------------------------------


class TestBuildRunner:
    @pytest.fixture
    def rb(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_build as rb
        cfg(SHORTS_BUILD_ENABLED=True, AUTOMATION_ENABLED=True, FACE_ENABLED=True, FACE_RAMP_START="2025-01-01")
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_AI_KEY", "k")
        monkeypatch.setenv("GEMINI_API_SUB_PAY_KEY", "g")
        monkeypatch.setenv("NOTION_TOKEN", "secret_x")
        self.notes = []
        monkeypatch.setattr(rb, "_notify", self.notes.append)
        monkeypatch.setattr(rb.notifier, "send_video", lambda *a, **k: True)
        monkeypatch.setattr(rb.mood_source, "collect", lambda *a, **k: mood_source.Mood("none"))
        return rb

    def _capture_build(self, rb, monkeypatch):
        seen = []

        def fake_build(item, mood, **kw):
            seen.append((item, kw.get("story")))
            return {"content_id": item.content_id, "fmt": item.fmt, "channels": list(item.channels),
                    "caption": "c", "video": f"{item.content_id}/video.mp4", "duration": 57.0}

        monkeypatch.setattr(rb, "build_one", fake_build)
        return seen

    def test_story_off_no_ledger(self, rb, monkeypatch):
        boom = mock.Mock(side_effect=AssertionError("원장 조회 금지"))
        monkeypatch.setattr(rb.face_story, "load_context", boom)
        seen = self._capture_build(rb, monkeypatch)
        assert rb.run() == 0 and seen and all(story is None for _, story in seen)

    def test_story_on_loads_once(self, rb, monkeypatch, cfg):
        cfg(FACE_STORY_ENABLED=True, FACE_NOTION_DB_ID="db")
        ctx = face_story.StoryContext(True, next_series_no=1)
        load = mock.Mock(return_value=ctx)
        monkeypatch.setattr(rb.face_story, "load_context", load)
        seen = self._capture_build(rb, monkeypatch)
        assert rb.run() == 0
        load.assert_called_once()
        assert load.call_args.args == ("secret_x", "db") and isinstance(load.call_args.kwargs["today"], dt.date)
        assert all(story is ctx for _, story in seen) and not self.notes

    def test_story_unavailable_notifies(self, rb, monkeypatch, cfg):
        cfg(FACE_STORY_ENABLED=True, FACE_NOTION_DB_ID="db")
        monkeypatch.setattr(rb.face_story, "load_context",
                            lambda *a, **k: face_story.StoryContext.unavailable("Notion 404"))
        self._capture_build(rb, monkeypatch)
        assert rb.run() == 0 and any("원장을 읽지 못해" in n for n in self.notes)

    def test_threads_only_plan_skips_ledger(self, rb, monkeypatch, cfg):
        cfg(FACE_STORY_ENABLED=True, FACE_ENABLED=False, SHORTS_THREADS_ENABLED=True)
        boom = mock.Mock(side_effect=AssertionError("Facebook 편이 없는데 원장 조회"))
        monkeypatch.setattr(rb.face_story, "load_context", boom)
        self._capture_build(rb, monkeypatch)
        assert rb.run() == 0

    def _build_one(self, rb, monkeypatch, tmp_path, item, story, script_cont):
        calls = []

        def fake_write(api_key, **kw):
            calls.append(kw)
            return script_writer.Script(
                content_id=item.content_id, fmt=item.fmt, villain=None, hook_type="A",
                beats=(script_writer.Beat("훅", is_hook=True),), image_prompts=("p",) * 5, caption="캡션",
                themes=("금리",), character="GOC", continuity=script_cont)

        monkeypatch.setattr(rb.script_writer, "write_script", fake_write)
        monkeypatch.setattr(rb.image_gen, "generate_scenes", lambda *a, **k: [tmp_path / "i.png"] * 5)
        monkeypatch.setattr(rb, "_voice_and_render", lambda *a: mock.Mock(total=57.0))
        monkeypatch.setattr(rb.validator, "check", lambda v: [])
        entry = rb.build_one(item, mood_source.Mood("none"), claude_key="k", gemini_key="g", used_types=set(),
                             used_hooks=[], base=tmp_path, story=story)
        return entry, calls

    def test_build_one_face_f1_gets_contract(self, rb, monkeypatch, tmp_path):
        item = shorts_plan.PlannedVideo("sv-20261005-1", 0, "F1", ("face",), "GOC")
        ctx = face_story.StoryContext(True, next_series_no=4)
        cont = {"summary": SUMMARY, "thread_action": "OPEN", "thread_text": THREAD}
        entry, calls = self._build_one(rb, monkeypatch, tmp_path, item, ctx, cont)
        assert calls[0]["continuity"].allow_thread
        assert entry["continuity"]["series_no"] == 4 and entry["continuity"]["thread_id"] == "th-sv-20261005-1"
        assert entry["themes"] == ["금리"]

    def test_build_one_no_story_is_v17(self, rb, monkeypatch, tmp_path):
        item = shorts_plan.PlannedVideo("sv-20261005-1", 0, "F1", ("face",), "GOC")
        entry, calls = self._build_one(rb, monkeypatch, tmp_path, item, None, None)
        assert calls[0]["continuity"] is None and "continuity" not in entry and "themes" not in entry

    def test_build_one_threads_only_no_contract(self, rb, monkeypatch, tmp_path):
        item = shorts_plan.PlannedVideo("sv-20261005-1", 0, "F1", ("threads",), "EDT")
        entry, calls = self._build_one(rb, monkeypatch, tmp_path, item, face_story.StoryContext(True), None)
        assert calls[0]["continuity"] is None and "continuity" not in entry


# ---------------------------------------------------------------------------
# E. publish 러너
# ---------------------------------------------------------------------------


def _manifest(tmp_path, today, n=1, channels=("face",)):
    items = []
    for i in range(1, n + 1):
        cid = f"sv-{today:%Y%m%d}-{i}"
        (tmp_path / cid).mkdir(parents=True, exist_ok=True)
        (tmp_path / cid / "video.mp4").write_bytes(b"v")
        items.append({"content_id": cid, "fmt": "F1" if i == 1 else "F2", "channels": list(channels),
                      "caption": f"캡션 {cid}\n\n{config.SHORTS_AI_NOTICE}", "video": f"{cid}/video.mp4",
                      "hook_type": "A", "themes": ["금리"],
                      "continuity": {"summary": SUMMARY, "series_no": 1, "thread_id": "", "thread_text": "",
                                     "thread_state": "없음", "thread_age": None, "resolution": ""}})
    (tmp_path / "manifest.json").write_text(json.dumps({"items": items}, ensure_ascii=False), "utf-8")
    return items


DONE = face_client.ReelStatus("ready", "complete", "complete", "complete", "")
BUSY = face_client.ReelStatus("processing", "complete", "in_progress", "not_started", "")
ERR = face_client.ReelStatus("error", "complete", "error", "not_started", "bad")


class TestPublishRunner:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path, cfg):
        from src import run_shorts_publish as rp
        cfg(AUTOMATION_ENABLED=True, FACE_ENABLED=True, FACE_STORY_ENABLED=True, FACE_NOTION_DB_ID="db")
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        monkeypatch.setenv("FACE_PAGE_ID", "123")
        monkeypatch.setenv("FACE_PAGE_TOKEN", "EAA" + "t" * 30)
        monkeypatch.setenv("NOTION_TOKEN", "secret_x")
        monkeypatch.setenv("DRY_RUN", "false")
        self.notes = []
        monkeypatch.setattr(rp, "_notify", self.notes.append)
        monkeypatch.setattr(rp, "_sleep_until", lambda t: None)
        fixed = dt.datetime.combine(dt.datetime.now(KST).date(), dt.time(11, 0), tzinfo=KST)

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz is None or tz == KST else fixed.astimezone(tz)

        monkeypatch.setattr(rp.dt, "datetime", _DT)
        self.ledger = mock.Mock()
        self.ledger.pending.return_value = []
        self.ledger.pending_overflow = 0
        self.ledger.upsert.side_effect = lambda rec, **kw: (f"pg-{rec.content_id}", rec.status)
        self.ledger_cls = mock.Mock(return_value=self.ledger)
        monkeypatch.setattr(rp.face_story, "Ledger", self.ledger_cls)
        self.face = mock.Mock()
        self.face.recent_reels.return_value = []
        monkeypatch.setattr(rp, "FaceClient", mock.Mock(return_value=self.face))
        return rp, tmp_path, fixed.date()

    def _recs(self):
        return [c.args[0] for c in self.ledger.upsert.call_args_list]

    def _statuses(self):
        return [(r.status, r.video_id) for r in self._recs()]

    def test_published_recorded(self, env):
        rp, tmp, today = env
        items = _manifest(tmp, today, n=2)
        self.face.publish_reel.side_effect = [("v1", DONE), ("v2", DONE)]
        assert rp.run() == 0
        recs = self._recs()
        assert [(r.content_id, r.status, r.video_id) for r in recs] == [
            (items[0]["content_id"], "확인필요", ""), (items[0]["content_id"], "게시완료", "v1"),   # 선기록 → 결과
            (items[1]["content_id"], "확인필요", ""), (items[1]["content_id"], "게시완료", "v2")]
        assert recs[1].continuity["series_no"] == 1
        # 두 번째 기록부터는 첫 기록의 page_id 로 갱신(재조회 없음 — 리뷰 #3-F6)
        kw = self.ledger.upsert.call_args_list[1].kwargs
        assert kw == {"page_id": f"pg-{items[0]['content_id']}", "current_status": "확인필요"}
        self.ledger_cls.assert_called_once_with("secret_x", "db")
        self.face.recent_descriptions.assert_not_called()

    def test_pending_then_rechecked(self, env):
        rp, tmp, today = env
        _manifest(tmp, today)
        self.face.publish_reel.return_value = ("v1", None)
        self.face.status.return_value = DONE
        assert rp.run() == 0
        assert self._statuses() == [("확인필요", ""), ("확인필요", "v1"), ("게시완료", "v1")]
        assert "재확인" in self.notes[-1] and "게시완료" in self.notes[-1]      # 리뷰 #1-D5

    def test_pending_still_busy(self, env):
        rp, tmp, today = env
        _manifest(tmp, today)
        self.face.publish_reel.return_value = ("v1", None)
        self.face.status.return_value = BUSY
        assert rp.run() == 0
        assert self._statuses() == [("확인필요", ""), ("확인필요", "v1")]

    def test_failure_recorded_exit_4(self, env):
        rp, tmp, today = env
        _manifest(tmp, today)
        self.face.publish_reel.return_value = ("v1", ERR)
        assert rp.run() == rp.FAIL_EXIT_CODE
        rec = self._recs()[-1]
        assert (rec.status, rec.video_id) == ("실패", "v1") and "v1" in rec.error

    @pytest.mark.parametrize("status,video_id,expected", [
        (0, "v1", "확인필요"),       # 업로드 세션 뒤 네트워크 오류 — 이미 게시됐을 수 있음
        (500, "v1", "확인필요"),     # finish 단계 서버 오류(재시도 없음)
        (0, "", "실패"),             # 세션 전 재시도 소진 — finish 를 안 불렀으므로 게시 안 됨(3차 R3-1)
        (400, "", "실패"),           # 세션 전 거부 — 게시 안 됨
    ])
    def test_unknown_outcome_not_marked_failed(self, env, status, video_id, expected):
        """리뷰 #2-3: 결과를 모르는 오류는 확인필요로 남긴다."""
        rp, tmp, today = env
        _manifest(tmp, today)
        exc = face_client.FaceApiError(status, "boom")
        exc.video_id = video_id
        self.face.publish_reel.side_effect = exc
        self.face.status.return_value = BUSY
        assert rp.run() == rp.FAIL_EXIT_CODE                       # 종료코드는 v1.7.0 과 같음(실패 집계)
        assert self._statuses()[1] == (expected, video_id)

    def test_publish_reel_attaches_video_id(self, monkeypatch, tmp_path):
        client = face_client.FaceClient("123", "EAA" + "t" * 30)
        monkeypatch.setattr(face_client.safety, "guard_write", lambda: None)
        calls = iter([{"video_id": "v77"}])
        monkeypatch.setattr(face_client, "_send", lambda *a, **k: next(calls))
        monkeypatch.setattr(client, "_upload_and_finish", mock.Mock(side_effect=face_client.FaceApiError(0, "x")))
        with pytest.raises(face_client.FaceApiError) as info:
            client.publish_reel(tmp_path / "v.mp4", "d")
        assert info.value.video_id == "v77" and not info.value.definitive

    def test_duplicate_records_existing_id(self, env):
        rp, tmp, today = env
        items = _manifest(tmp, today)
        self.face.recent_reels.return_value = [("old-v", items[0]["caption"])]
        self.face.status.return_value = DONE
        assert rp.run() == 0
        self.face.publish_reel.assert_not_called()
        assert self._statuses()[-1] == ("게시완료", "old-v")

    def test_duplicate_still_processing_is_pending(self, env):
        """리뷰 #3-F1: 같은 설명의 릴스가 있어도 처리 확인 전에는 게시완료로 쓰지 않는다."""
        rp, tmp, today = env
        items = _manifest(tmp, today)
        self.face.recent_reels.return_value = [("old-v", items[0]["caption"])]
        self.face.status.return_value = BUSY
        assert rp.run() == 0
        assert self._statuses()[-1] == ("확인필요", "old-v")

    def test_non_ledger_exception_isolated(self, env):
        rp, tmp, today = env
        _manifest(tmp, today, n=2)
        self.face.publish_reel.side_effect = [("v1", DONE), ("v2", DONE)]
        self.ledger.upsert.side_effect = ValueError("unexpected")
        assert rp.main() == 0 and self.face.publish_reel.call_count == 2
        assert "원장 기록 실패" in self.notes[-1]

    def test_postloop_fatal_sends_results_first(self, env):
        """리뷰 #2-5: 루프 뒤 재확인의 계정 오류 — 결과 알림을 먼저 보내고 종료코드 7."""
        rp, tmp, today = env
        _manifest(tmp, today)
        self.face.publish_reel.return_value = ("v1", None)
        self.face.status.side_effect = face_client.FaceApiError(400, "x", code=190)
        assert rp.main() == safety.FATAL_EXIT_CODE
        assert any(n.startswith("[Shorts] 게시 결과") for n in self.notes)
        assert self.notes[-1].startswith("[Facebook][최우선]")

    def test_ledger_failure_isolated(self, env):
        rp, tmp, today = env
        _manifest(tmp, today)
        self.face.publish_reel.return_value = ("v1", DONE)
        self.ledger.upsert.side_effect = face_story.LedgerError("Notion 500")
        assert rp.run() == 0                                       # 게시 결과·종료코드 불변
        msg = self.notes[-1]
        assert "원장 기록 실패(수동 입력 필요)" in msg and "FB영상ID=v1" in msg and SUMMARY in msg
        assert self.face.publish_reel.call_count == 1               # 재게시 없음

    def test_ledger_config_missing(self, env, monkeypatch):
        rp, tmp, today = env
        _manifest(tmp, today)
        monkeypatch.setattr(rp.face_story, "Ledger", REAL_LEDGER)
        monkeypatch.delenv("NOTION_TOKEN")
        self.face.recent_descriptions.return_value = []
        self.face.publish_reel.return_value = ("v1", DONE)
        assert rp.run() == 0
        assert "원장 미사용" in self.notes[-1]
        self.face.recent_descriptions.assert_called_once()          # 원장 없으면 v1.7.0 경로

    def test_reconcile_pending_rows(self, env):
        rp, tmp, today = env
        _manifest(tmp, today)
        cap = "지난 회차 캡션"
        recent = f"sv-{today - dt.timedelta(days=1):%Y%m%d}"            # 1일 경과 — 미확인이면 보류
        old = f"sv-{today - dt.timedelta(days=3):%Y%m%d}"               # 3일 경과 — 미확인이면 실패로 닫음
        self.ledger.pending.return_value = [
            ("pg1", f"{recent}-1", "v1a", ""), ("pg2", f"{recent}-2", "v2a", ""),
            ("pg3", f"{recent}-3", "v3a", ""), ("pg4", f"{recent}-4", "", "nomatch"),
            ("pg5", f"{recent}-5", "", face_story.caption_hash(cap)),
            ("pg6", f"{old}-1", "v6a", ""), ("pg7", f"{old}-2", "", "nomatch")]
        self.face.recent_reels.return_value = [("found5", cap)]
        self.face.status.side_effect = [DONE, ERR, BUSY, DONE, BUSY]
        self.face.publish_reel.return_value = ("v1", DONE)
        assert rp.run() == 0
        assert self.ledger.set_status.call_args_list == [
            mock.call("pg1", f"{recent}-1", "게시완료", video_id="v1a"),
            mock.call("pg2", f"{recent}-2", "실패", video_id="v2a"),
            mock.call("pg5", f"{recent}-5", "게시완료", video_id="found5"),
            mock.call("pg6", f"{old}-1", "실패", "확인 불가(2일 경과): 아직 처리 중", video_id="v6a"),
            mock.call("pg7", f"{old}-2", "실패", "확인 불가(2일 경과): 게시 흔적 없음(최근 릴스에 같은 설명 없음)",
                      video_id="")]
        msg = self.notes[-1]
        assert "결번 가능" in msg and "게시완료로 고쳐" in msg
        assert self.face.recent_reels.call_count == 2                  # 재조정 1회(행이 여럿이어도) + 게시 전 중복확인 1회

    def test_reconcile_bad_content_id_closed(self, env):
        """3차 OPS-R3-2: 회차ID 형식이 틀린 확인필요 행은 바로 닫는다."""
        rp, tmp, today = env
        _manifest(tmp, today)
        self.ledger.pending.return_value = [("pg1", "수동입력", "", "")]
        self.face.publish_reel.return_value = ("v1", DONE)
        assert rp.run() == 0
        assert self.ledger.set_status.call_args_list == [
            mock.call("pg1", "수동입력", "실패", "회차ID 형식 오류(sv-YYYYMMDD-N)")]

    def test_reconcile_status_error_expires(self, env):
        """리뷰 2차 CR-C: 영상이 지워져 상태 조회가 계속 실패하는 오래된 행은 닫혀 자리를 비운다."""
        rp, tmp, today = env
        _manifest(tmp, today)
        old = f"sv-{today - dt.timedelta(days=5):%Y%m%d}-1"
        new = f"sv-{today - dt.timedelta(days=1):%Y%m%d}-1"
        self.ledger.pending.return_value = [("pg1", old, "gone", ""), ("pg2", new, "gone2", "")]
        self.face.status.side_effect = [face_client.FaceApiError(400, "no such video", code=100),
                                        face_client.FaceApiError(400, "no such video", code=100), DONE]
        self.face.publish_reel.return_value = ("v1", DONE)
        assert rp.run() == 0
        calls = self.ledger.set_status.call_args_list
        assert len(calls) == 1 and calls[0].args[:3] == ("pg1", old, "실패")

    def test_reconcile_overflow_noted(self, env):
        rp, tmp, today = env
        _manifest(tmp, today)
        self.ledger.pending_overflow = 4
        self.face.publish_reel.return_value = ("v1", DONE)
        assert rp.run() == 0 and "4건 이상 남음" in self.notes[-1]

    def test_reconcile_query_error_isolated(self, env):
        rp, tmp, today = env
        _manifest(tmp, today)
        self.ledger.pending.side_effect = KeyError("x")
        self.face.publish_reel.return_value = ("v1", DONE)
        assert rp.run() == 0 and "재조정 조회 실패" in self.notes[-1]

    def test_reconcile_fatal_exits_7(self, env):
        rp, tmp, today = env
        _manifest(tmp, today)
        self.ledger.pending.return_value = [("pg1", "sv-20261001-1", "old1", "")]
        self.face.status.side_effect = face_client.FaceApiError(400, "x", code=190)
        assert rp.main() == safety.FATAL_EXIT_CODE
        self.face.publish_reel.assert_not_called()

    def test_dry_run_no_ledger(self, env, monkeypatch):
        rp, tmp, today = env
        _manifest(tmp, today)
        monkeypatch.setenv("DRY_RUN", "true")
        assert rp.run() == 0
        self.ledger_cls.assert_not_called()

    def test_story_off_no_ledger(self, env, cfg):
        rp, tmp, today = env
        cfg(FACE_STORY_ENABLED=False)
        _manifest(tmp, today)
        self.face.recent_descriptions.return_value = []
        self.face.publish_reel.return_value = ("v1", DONE)
        assert rp.run() == 0
        self.ledger_cls.assert_not_called()
        self.face.recent_reels.assert_not_called()
        assert "[원장]" not in self.notes[-1]

    def test_kill_switch_no_ledger(self, env, cfg):
        rp, tmp, today = env
        cfg(AUTOMATION_ENABLED=False)
        _manifest(tmp, today)
        assert rp.run() == 0
        self.ledger_cls.assert_not_called()

    def test_threads_only_no_ledger(self, env, cfg, monkeypatch):
        rp, tmp, today = env
        cfg(FACE_ENABLED=False, SHORTS_THREADS_ENABLED=False)
        _manifest(tmp, today, channels=("threads",))
        assert rp.run() == 0
        self.ledger_cls.assert_not_called()

    def test_dedupe_status_error_is_skip_not_failure(self, env):
        """2차 QC-N1·CR-D: 건너뛴 편의 상태 확인 실패는 실패가 아니다(종료코드 0, 영상ID 유지)."""
        rp, tmp, today = env
        items = _manifest(tmp, today)
        self.face.recent_reels.return_value = [("old-v", items[0]["caption"])]
        self.face.status.side_effect = face_client.FaceApiError(0, "재시도 소진")
        assert rp.run() == 0
        assert self._statuses()[-1] == ("확인필요", "old-v")
        assert "건너뜀" in self.notes[-1] and "상태 확인 실패" in self.notes[-1]

    def test_session_callback_records_video_id(self, env):
        """2차 OPS-N1: 세션 video_id 를 받는 즉시 원장에 남긴다."""
        rp, tmp, today = env
        _manifest(tmp, today)

        def fake_publish(video, caption, on_session=None):
            on_session("v-sess")
            exc = face_client.FaceApiError(0, "끊김")
            exc.video_id = "v-sess"          # 실제 publish_reel 은 세션 뒤 오류에 video_id 를 붙인다
            raise exc

        self.face.publish_reel.side_effect = fake_publish
        self.face.status.return_value = BUSY
        assert rp.run() == rp.FAIL_EXIT_CODE
        assert ("확인필요", "v-sess") in self._statuses()
        assert "결과 미확인(확인필요 — 재게시 금지" in self.notes[-1]       # 2차 CR-E

    def test_prerecord_failure_quiet(self, env):
        """2차 OPS-N5: 선기록 실패는 알림에 싣지 않고 결과 기록 실패만 싣는다."""
        rp, tmp, today = env
        _manifest(tmp, today)
        self.face.publish_reel.return_value = ("v1", DONE)
        calls = {"n": 0}

        def flaky(rec, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise face_story.LedgerError("선기록 실패")
            return (f"pg-{rec.content_id}", rec.status)

        self.ledger.upsert.side_effect = flaky
        assert rp.run() == 0 and "[원장]" not in self.notes[-1]

    def test_manual_values_include_caption_hash(self, env):
        rp, tmp, today = env
        items = _manifest(tmp, today)
        self.face.publish_reel.return_value = ("v1", DONE)
        self.ledger.upsert.side_effect = face_story.LedgerError("down")
        assert rp.run() == 0
        assert f"캡션해시={face_story.caption_hash(items[0]['caption'])}" in self.notes[-1]

    def test_recheck_skipped_near_timeout(self, env, monkeypatch):
        rp, tmp, today = env
        _manifest(tmp, today)
        self.face.publish_reel.return_value = ("v1", None)
        start = dt.datetime.combine(today, dt.time(11, 0), tzinfo=KST)
        times = iter([start, start + dt.timedelta(minutes=340)])

        class _DT2(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return next(times, start + dt.timedelta(minutes=340))

        monkeypatch.setattr(rp.dt, "datetime", _DT2)
        assert rp.run() == 0
        self.face.status.assert_not_called()
        assert "재확인 생략" in self.notes[-1]

    def test_post_face_compat_string(self, env):
        rp, tmp, today = env
        items = _manifest(tmp, today)
        self.face.recent_descriptions.return_value = []
        self.face.publish_reel.return_value = ("v9", DONE)
        assert rp.post_face(self.face, items[0], tmp).endswith("video_id=v9 게시 완료")


# ---------------------------------------------------------------------------
# F. 구성
# ---------------------------------------------------------------------------


class TestConfigAndWorkflow:
    def test_defaults_off(self):
        assert config.SAFETY_VARIABLE_DEFAULTS["FACE_STORY_ENABLED"] == "false"
        assert config.VERSION == "1.8.5"
        assert config.FACE_STORY_SCAN_ROWS > 10 >= 2

    def test_clamps(self, monkeypatch):
        monkeypatch.setenv("FACE_STORY_LOOKBACK", "99")
        monkeypatch.setenv("FACE_THREAD_MAX_EPISODES", "abc")
        try:
            importlib.reload(config)
            assert config.FACE_STORY_LOOKBACK == 5 and config.FACE_THREAD_MAX_EPISODES == 5
            monkeypatch.setenv("FACE_THREAD_MAX_EPISODES", "0")
            importlib.reload(config)
            assert config.FACE_THREAD_MAX_EPISODES == 2
        finally:
            monkeypatch.delenv("FACE_STORY_LOOKBACK")
            monkeypatch.delenv("FACE_THREAD_MAX_EPISODES")
            importlib.reload(config)

    def test_scan_rows_cover_forced_resolve(self):
        assert config.FACE_STORY_SCAN_ROWS > 10   # FACE_THREAD_MAX_EPISODES 상한(10)보다 커야 떡밥을 놓치지 않음

    def test_workflow_env(self):
        wf = yaml.safe_load((WF / "shorts.yml").read_text("utf-8"))
        build = {k: v for st in wf["jobs"]["build"]["steps"] for k, v in (st.get("env") or {}).items()}
        pub = {k: v for st in wf["jobs"]["publish"]["steps"] for k, v in (st.get("env") or {}).items()}
        for env in (build, pub):
            assert env["FACE_STORY_ENABLED"] == "${{ vars.FACE_STORY_ENABLED || 'false' }}"
            assert env["NOTION_TOKEN"] == "${{ secrets.NOTION_TOKEN }}"
            assert env["FACE_NOTION_DB_ID"] == "${{ secrets.FACE_NOTION_DB_ID }}"
            assert "NOTION_DB_ID" not in env                       # 기존 Tracker DB 는 넘기지 않는다
        assert "FACE_PAGE_TOKEN" not in build                       # 승인 없는 job 에 게시 토큰 없음

    def test_verify_repo_registers(self):
        import verify_repo
        assert "face_story" in verify_repo.REQUIRED_MODULES
        assert "FACE_STORY_ENABLED" in verify_repo.SAFETY_WORKFLOW_KEYS["shorts.yml"]

    def test_notion_source_untouched(self):
        from src import notion_source
        assert notion_source.VERSION == "1.0.0"


# ---------------------------------------------------------------------------
# G. 여러 날 시뮬레이션 — 실제 Ledger 쓰기 형식 → 실제 읽기(가짜 Notion 저장소)
# ---------------------------------------------------------------------------


class _FakeNotion:
    """databases/query(select·title equals, and, 날짜·created_time 정렬) · pages 생성/갱신만 흉내 낸다.
    저장 시 Notion 처럼 type 키와 plain_text 를 붙인다."""

    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.seq = 0

    @staticmethod
    def _stored(props: dict) -> dict:
        out = {}
        for name, value in props.items():
            ptype = face_story.SCHEMA[name]
            v = dict(value)
            if ptype in ("title", "rich_text"):
                v[ptype] = [{"plain_text": p["text"]["content"], **p} for p in v[ptype]]
            out[name] = {"type": ptype, **v}
        return out

    def _match(self, page, flt):
        if not flt:
            return True
        if "and" in flt:
            return all(self._match(page, f) for f in flt["and"])
        if "or" in flt:
            return any(self._match(page, f) for f in flt["or"])
        prop = page["properties"].get(flt["property"], {})
        if "select" in flt:
            return (prop.get("select") or {}).get("name") == flt["select"]["equals"]
        if "title" in flt:
            return "".join(p["plain_text"] for p in prop.get("title", [])) == flt["title"]["equals"]
        raise AssertionError(flt)

    def request(self, method, url, json=None, **kw):
        path = url.split("/v1/", 1)[1]
        if method == "POST" and path.endswith("/query"):
            rows = [p for p in self.pages.values() if self._match(p, json.get("filter"))]
            for sort in reversed(json.get("sorts") or []):
                if sort.get("property") == "날짜":
                    rows.sort(key=lambda p: (p["properties"].get("날짜") or {}).get("date", {}).get("start", ""),
                              reverse=sort["direction"] == "descending")
                else:
                    rows.sort(key=lambda p: p["created"], reverse=sort["direction"] == "descending")
            return _Resp(payload={"results": rows[: json.get("page_size", 100)]})
        if method == "POST" and path == "pages":
            self.seq += 1
            pid = f"page{self.seq}"
            self.pages[pid] = {"id": pid, "created": self.seq, "properties": self._stored(json["properties"])}
            return _Resp(payload={"id": pid})
        if method == "PATCH" and path.startswith("pages/"):
            pid = path.split("/", 1)[1]
            self.pages[pid]["properties"].update(self._stored(json["properties"]))
            return _Resp(payload={"id": pid})
        raise AssertionError((method, path))


class TestMultiDaySimulation:
    def _publish(self, ledger, cid, raw_cont, ctx, status="게시완료"):
        req = face_story.request_for(ctx, "F1")
        assert face_story.validate_continuity(raw_cont, req) == []
        item = {"content_id": cid, "fmt": "F1", "hook_type": "A", "themes": ["금리"], "caption": "c " + cid,
                "continuity": face_story.build_continuity(raw_cont, req, ctx, cid)}
        ledger.upsert(face_story.record_from_item(item, status, video_id="v-" + cid,
                                                  ))
        return item

    def test_seven_days(self, cfg):
        cfg(FACE_THREAD_MAX_EPISODES=3)
        store = _FakeNotion()
        led = face_story.Ledger("t", "db", session=store)
        load = lambda: face_story.load_context("t", "db", session=store)   # noqa: E731

        d1 = load()
        assert d1.next_series_no == 1 and d1.open_thread is None
        self._publish(led, "sv-20261005-1", {"summary": SUMMARY, "thread_action": "OPEN", "thread_text": THREAD}, d1)
        # 같은 날 F2 는 연속성 입력에서 빠진다
        led.upsert(face_story.record_from_item({"content_id": "sv-20261005-2", "fmt": "F2", "caption": "x"},
                                               "게시완료", video_id="v2"))

        d2 = load()
        assert d2.next_series_no == 2 and d2.open_thread.thread_id == "th-sv-20261005-1"
        assert d2.open_thread.age == 1 and [e.series_no for e in d2.episodes] == [1]
        self._publish(led, "sv-20261006-1", {"summary": SUMMARY, "thread_action": "PROGRESS",
                                             "thread_text": NEW_CLUE}, d2)

        d3 = load()
        assert d3.open_thread.text == NEW_CLUE and d3.open_thread.age == 2 and d3.next_series_no == 3
        # 확인필요(FB영상ID 있음)는 번호·떡밥에는 넣고 요약은 기억에 넣지 않는다(리뷰 #2-1)
        third = "종을 울린 손은 금고 안쪽에서 나왔다는 이야기가 돌았다"
        self._publish(led, "sv-20261007-1", {"summary": SUMMARY.replace("하루였습니다", "밤이었습니다"),
                                             "thread_action": "PROGRESS", "thread_text": third},
                      d3, status="확인필요")
        d4 = load()
        assert d4.next_series_no == 4 and [e.series_no for e in d4.episodes] == [2, 1]
        # 최근 떡밥 변화가 미확정 회차에 있으면 떡밥 문장을 쓰지 않고 이번 화는 떡밥을 다루지 않는다(2차 CR-B)
        assert d4.thread_hold and d4.open_thread is None
        assert face_story.request_for(d4, "F1").allowed_actions() == ("NONE",)
        # 재조정으로 게시완료가 되면 요약도 기억에 들어간다
        pid, cid, vid, _ = led.pending()[0]
        led.set_status(pid, cid, "게시완료")
        d5 = load()
        assert d5.next_series_no == 4 and d5.episodes[0].series_no == 3
        assert not d5.thread_hold and d5.open_thread.text == third and d5.open_thread.age == 3
        req5 = face_story.request_for(d5, "F1")
        assert req5.force_resolve and req5.allowed_actions() == ("RESOLVE",)
        self._publish(led, "sv-20261008-1", {"summary": SUMMARY, "thread_action": "RESOLVE",
                                             "resolution": RESOLUTION}, d5)

        d6 = load()
        assert d6.open_thread is None and d6.next_series_no == 5
        # 같은 회차 재기록은 1행(멱등)
        self._publish(led, "sv-20261008-1", {"summary": SUMMARY, "thread_action": "RESOLVE",
                                             "resolution": RESOLUTION}, d5)
        ids = [face_story._plain(p["properties"]["회차ID"]) for p in store.pages.values()]
        assert ids.count("sv-20261008-1") == 1 and len(ids) == 5
        # 원장 장애일: 원장 없이 만든 F1 은 NONE · 시리즈회차 없음 → 다음 날 번호·경과가 이어진다
        down = face_story.StoryContext.unavailable("Notion 503")
        req_down = face_story.request_for(down, "F1")
        item = {"content_id": "sv-20261009-1", "fmt": "F1", "caption": "c",
                "continuity": face_story.build_continuity({"summary": SUMMARY, "thread_action": "NONE"},
                                                          req_down, down, "sv-20261009-1")}
        led.upsert(face_story.record_from_item(item, "게시완료", video_id="v9"))
        d7 = load()
        assert d7.next_series_no == 6 and d7.open_thread is None
        # 재실행의 실패 기록이 게시완료를 덮어쓰지 않는다(리뷰 #2-2)
        led.upsert(face_story.EpisodeRecord("sv-20261009-1", "실패", error="recent_reels 재시도 소진"))
        assert load().next_series_no == 6
        # 같은 날 build 재실행은 오늘 행을 지난 이야기로 읽지 않는다(리뷰 #2-8)
        same_day = face_story.load_context("t", "db", today=dt.date(2026, 10, 9), session=store)
        assert same_day.next_series_no == 5


# ---------------------------------------------------------------------------
# H. 운영 베타 회귀 — 운영 출력 경로(out/shorts)는 상대 경로다
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not (__import__("shutil").which("ffmpeg") and __import__("shutil").which("ffprobe")),
                    reason="ffmpeg 없음")
class TestBetaRelativeOutPath:
    def test_concat_line_absolute_and_quoted(self, tmp_path, monkeypatch):
        from src.video import renderer
        monkeypatch.chdir(tmp_path)
        line = renderer._concat_line(pathlib.Path("out/shorts/a'b/seg.mp4"))
        assert line.startswith(f"file '{tmp_path.as_posix()}/out/shorts/a")
        assert "a'\\''b" in line

    def test_render_with_relative_out_dir(self, tmp_path, monkeypatch, cfg):
        """v1.8.0 운영 베타에서 발견: 상대 경로 출력이면 concat 이 경로를 겹쳐 렌더가 실패했다."""
        import random
        import subprocess

        from src.video import renderer, validator
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "X_URL", "https://x.com/tiger18272")
        work = pathlib.Path("out/shorts/sv-20261005-1")
        work.mkdir(parents=True)
        img = work / "img.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x336699:s=1024x1536",
                        "-frames:v", "1", str(img)], check=True)
        scenes = []
        for i in range(9):
            wav = work / f"n{i}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            f"sine=frequency={300 + i * 30}:sample_rate=24000:duration={2.6 if i == 0 else 5.2}",
                            "-ac", "1", str(wav)], check=True)
            scenes.append(renderer.SceneInput(img, wav, "시장에 경고등이 켜졌다", i == 0, None))
        out = work / "video.mp4"
        renderer.render(scenes, None, out, rng=random.Random(1))
        assert validator.check(out) == []

    def test_cover_by_character(self, tmp_path):
        from src.video import assets
        brand = tmp_path / "brand"
        brand.mkdir()
        for name in ("logo.png", "EDT_UNIVERS_cover.png", "goc_cover.png"):
            (brand / name).write_bytes(b"x")
        assert assets.find_cover(root=tmp_path, character="GOC").name == "goc_cover.png"
        assert assets.find_cover(root=tmp_path, character="EDT").name == "EDT_UNIVERS_cover.png"
        (brand / "goc_cover.png").unlink()
        assert assets.find_cover(root=tmp_path, character="GOC") is None      # → 마지막 장면 사용
        assert assets.find_cover(root=tmp_path).name == "EDT_UNIVERS_cover.png"  # 기존 동작

    def test_goc_outro_no_edt_cover_and_logo_keyed(self, tmp_path, monkeypatch, cfg):
        """운영 베타 발견: GOC 영상 아웃트로에 EDT 표지가 나오고, 로고 마젠타 배경이 그대로 보였다."""
        import subprocess

        from src.video import assets, renderer
        root = tmp_path / "assets"
        (root / "brand").mkdir(parents=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x00FF00:s=1024x1536",
                        "-frames:v", "1", str(root / "brand" / "EDT_cover.png")], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0xFF00FF:s=600x300",
                        "-vf", "drawbox=x=250:y=100:w=100:h=100:color=0xFFAA00:t=fill,format=rgba",
                        "-frames:v", "1", str(root / "brand" / "logo.png")], check=True)
        real_files = assets._files
        monkeypatch.setattr(assets, "_files", lambda sub, ext, r=None: real_files(sub, ext, r or root))
        img = tmp_path / "goc.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x2040A0:s=1024x1536",
                        "-frames:v", "1", str(img)], check=True)
        out = tmp_path / "seg_outro.mp4"
        renderer._render_outro(assets.find_cover(character="GOC") or img, "", out, tmp_path, None,
                               tmp_path / "ffmpeg.log")
        frame = tmp_path / "f.png"
        subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1", "-vf",
                        "scale=1080:1920,format=rgb24", "-f", "rawvideo", str(frame)], check=True)
        raw = frame.read_bytes()

        def px(x, y):
            i = (y * 1080 + x) * 3
            return raw[i], raw[i + 1], raw[i + 2]

        r, g, b = px(540, 960)                       # 화면 중앙 = 표지(배경)
        assert g < 150 and b > 100                   # EDT 표지(초록)가 아니라 GOC 장면(파랑)
        lr, lg_, lb = px(1080 - 60 - 10, 60 + 5)     # 로고 영역 모서리 = 마젠타가 아니어야 함
        assert not (lr > 200 and lg_ < 80 and lb > 200)


# ---------------------------------------------------------------------------
# I. 운영 베타 2차 — 대본 응답에 text 블록이 없던 문제(v1.8.2)
# ---------------------------------------------------------------------------


class TestBetaScriptThinking:
    def _resp(self, body):
        r = mock.Mock(status_code=200)
        r.json.return_value = body
        return r

    def test_payload_leaves_room_for_thinking(self, monkeypatch):
        seen = {}

        def fake_post(url, headers, json, timeout):
            seen["json"], seen["timeout"] = json, timeout
            return self._resp({"content": [{"type": "thinking", "thinking": "..."},
                                           {"type": "text", "text": '{"hook": "x"}'}]})

        monkeypatch.setattr(script_writer.requests, "post", fake_post)
        assert script_writer._call_claude("k", "p", "GOC") == {"hook": "x"}
        assert seen["json"]["max_tokens"] == config.SHORTS_SCRIPT_MAX_TOKENS >= 8000
        assert seen["timeout"] == config.SHORTS_SCRIPT_TIMEOUT_SEC
        assert "thinking" not in seen["json"]          # 사고 설정은 모델 기본값 유지(모델별 허용값이 다름)

    def test_empty_text_reports_stop_reason(self, monkeypatch):
        body = {"content": [{"type": "thinking", "thinking": "..."}], "stop_reason": "max_tokens",
                "usage": {"output_tokens": 1500}, "model": "claude-sonnet-5"}
        monkeypatch.setattr(script_writer.requests, "post", lambda *a, **k: self._resp(body))
        with pytest.raises(script_writer.ScriptError) as info:
            script_writer._call_claude("k", "p", "GOC")
        msg = str(info.value)
        assert "stop_reason=max_tokens" in msg and "'thinking'" in msg and "output_tokens=1500" in msg


# ---------------------------------------------------------------------------
# J. 운영 베타 3차 — 빠른 훅 · 분위기 훅 · 휴장일 표현 · 장면/움직임 다양화 (v1.8.3)
# ---------------------------------------------------------------------------


class TestBetaContentFixes:
    def test_calm_mood_excludes_alarm_hooks(self):
        """C1: 낙관·안도 날에 경고형(A)·긴급형(D)을 고르지 않는다."""
        for mood in ("낙관", "안도"):
            t = hooks.select_hook_type("F1", None, allowed=hooks.NO_VILLAIN_HOOK_TYPES, mood_word=mood)
            assert t == hooks.HOOK_C
            t = hooks.select_hook_type("F3", None, allowed=hooks.NO_VILLAIN_HOOK_TYPES, mood_word=mood)
            assert t not in hooks.ALARM_HOOK_TYPES
        assert hooks.select_hook_type("F1", None, allowed=hooks.NO_VILLAIN_HOOK_TYPES, mood_word="긴장") == "A"
        # 허용 유형이 경고형뿐이면 비우지 않고 그대로 쓴다(빈 선택 방지)
        assert hooks.select_hook_type("F1", None, allowed=("A",), mood_word="낙관") == "A"

    def test_hook_length_fast(self):
        assert (hooks.HOOK_MIN_CHARS, hooks.HOOK_MAX_CHARS) == (8, 14)
        for spec in hooks.HOOK_SPECS.values():
            assert hooks.HOOK_MIN_CHARS <= len(spec["example"]) <= hooks.HOOK_MAX_CHARS

    def test_day_context(self):
        sunday, monday = dt.date(2026, 10, 4), dt.date(2026, 10, 5)
        assert "주말" in script_writer.day_context(sunday) and "일요일" in script_writer.day_context(sunday)
        assert "결과는 아직 없다" in script_writer.day_context(monday)
        assert not any(c.isdigit() for c in script_writer.day_context(sunday))   # 숫자 금지 규칙과 충돌 방지
        assert script_writer.day_context(None) == ""

    def test_weekend_phrase_rejected(self):
        """C2: 휴장일(주말)에 '오늘 시장'을 말하면 거부한다. 평일은 규칙 대상 아님(결과 단정은 프롬프트 규칙)."""
        raw = _goc_raw()
        raw["body"][0] = "오늘 시장은 고요한 바람 속에서 천천히 숨을 고르고 있었습니다 정말로"
        sunday = dt.date(2026, 10, 4)
        issues = script_writer.validate(raw, hooks.HOOK_C, character="GOC", day=sunday)
        assert any("주말인데 '오늘 시장'" in i for i in issues)
        weekday = script_writer.validate(raw, hooks.HOOK_C, character="GOC", day=dt.date(2026, 10, 5))
        assert not any("주말" in i for i in weekday)

    def test_system_prompt_forbids_result_assertion(self):
        sp = script_writer.system_prompt("GOC")
        assert "결과를 사실로 단정하지 않습니다" in sp and "오늘 시장은 잔잔했다" in sp
        assert "서 있는 정면 전신을 반복하지 않는다" in sp

    def test_write_script_passes_mood_and_day(self, monkeypatch):
        seen = {}

        def fake(api_key, prompt, character="EDT"):
            seen["prompt"] = prompt
            return _goc_raw() | {"hook": "모두가 놓친 신호"}

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        s = script_writer.write_script("k", content_id="sv-20261004-1", fmt="F1",
                                       mood=mood_source.Mood("web", ("금리",), "낙관"), character="GOC")
        assert s.hook_type == hooks.HOOK_C
        assert "주말, 시장이 열리지 않는 날" in seen["prompt"]

    def test_image_shot_directives_distinct(self):
        """C3: 장면마다 다른 구도·자세, 참조 이미지는 외형만."""
        from src.video import image_gen
        prompts = [image_gen.build_prompt("guardian over the city", None, True, "GOC", shot_index=i)
                   for i in range(5)]
        assert len(set(prompts)) == 5
        for i, p in enumerate(prompts):
            assert image_gen.SHOT_DIRECTIVES[i] in p
            assert "do NOT copy the reference's pose" in p and "only character" in p
            assert "tiger" not in p.lower()
        edt = image_gen.build_prompt("x", "Debt Titan", True, "EDT", shot_index=2)
        assert image_gen.SHOT_DIRECTIVES[2] in edt and "do NOT copy" in edt
        assert image_gen.build_prompt("x", None, False, "GOC") == image_gen.build_prompt("x", None, False, "GOC",
                                                                                          shot_index=None)

    def test_generate_scenes_passes_shot_index(self, monkeypatch, tmp_path):
        from src.video import image_gen
        asked = []

        class _Models:
            def generate_content(self, model, contents):
                asked.append(contents[-1])
                part = mock.Mock(inline_data=mock.Mock(data=b"png"))
                return mock.Mock(candidates=[mock.Mock(content=mock.Mock(parts=[part]))])

        monkeypatch.setattr(image_gen.assets, "reference_images", lambda c: [])
        image_gen.generate_scenes("k", ["a"] * 5, None, tmp_path, client=mock.Mock(models=_Models()),
                                  character="GOC")
        assert [image_gen.SHOT_DIRECTIVES[i] in asked[i] for i in range(5)] == [True] * 5

    def test_body_motions_vary(self):
        from src.video import renderer
        assert len(set(renderer.BODY_MOTIONS)) >= 5
        exprs = {renderer.motion_expr(m, 150) for m in renderer.BODY_MOTIONS}
        assert len(exprs) == len(renderer.BODY_MOTIONS)
        # 같은 이미지를 쓰는 연속 비트(BEAT_IMAGE_SLOT 1,1 / 2,2 …)는 움직임이 서로 다르다
        motions = [renderer.BODY_MOTIONS[i % len(renderer.BODY_MOTIONS)] for i in range(8)]
        assert all(motions[i] != motions[i + 1] for i in range(7))
        assert renderer.HOOK_MIN_SEC == 2.0

    def test_weekend_regex_no_false_positive(self):
        """리뷰 v1.8.3: '오늘 장면·오늘 장마·오늘 장기전'은 막지 않고, '오늘은 시장'·'오늘 장이'는 막는다."""
        hits = {label for label, pat in script_writer.WEEKEND_BANNED_PATTERNS
                for t in ("오늘은 시장이 쉰다",) if pat.search(t)}
        assert hits == {"오늘 시장"}
        for ok in ("오늘 장면은 조용하다", "오늘 장마처럼 흐리다", "오늘 장기전의 시작"):
            assert not any(p.search(ok) for _, p in script_writer.WEEKEND_BANNED_PATTERNS), ok
        for bad in ("오늘 장이 조용했다", "오늘 장 마감 뒤", "오늘의 증시는", "오늘 장."):
            assert any(p.search(bad) for _, p in script_writer.WEEKEND_BANNED_PATTERNS), bad

    def test_f1_guides_no_today_market(self):
        for profile in script_writer.CHARACTER_PROFILES.values():
            assert "오늘 시장" not in profile["formats"]["F1"][1]

    def test_calm_day_hooks_vary(self):
        """리뷰 v1.8.3: 차분한 날 GOC 훅이 C 하나로 고정되지 않게 질문형(E)을 둔다."""
        used: set[str] = set()
        picked = []
        for fmt in ("F1", "F2"):
            t = hooks.select_hook_type(fmt, None, used, hooks.NO_VILLAIN_HOOK_TYPES, "낙관")
            used.add(t)
            picked.append(t)
        assert picked == ["C", "E"]
        assert hooks.HOOK_SPECS["E"]["sfx"] == "hook_c"          # 존재하는 효과음 파일 재사용


# ---------------------------------------------------------------------------
# K. 운영 베타 4차 — 훅 낭독 가속 · 이미지 7장 교차 · 3인칭 · 전언형 금지 (v1.8.4)
# ---------------------------------------------------------------------------


class TestBetaQualityV184:
    def test_hook_tones_all_fast(self):
        assert all(spec["tts_tone"].startswith("빠르고 또렷하게") for spec in hooks.HOOK_SPECS.values())

    def test_hook_tempo_applies_only_when_long(self):
        from src.video import renderer
        slow = renderer.plan_timing([3.0] + [5.0] * 8)
        assert slow.hook_tempo == round(3.0 / renderer.HOOK_FAST_SEC, 4)          # 1.2배 → 2.5초
        assert slow.durations[0] == round(max(renderer.HOOK_MIN_SEC,
                                              3.0 / slow.hook_tempo + renderer.SEG_PAD_SEC), 3)
        beta = renderer.plan_timing([3.4] + [5.0] * 8)            # 운영 베타 사례(훅 낭독 약 3.4초)
        assert beta.hook_tempo == renderer.HOOK_TEMPO_MAX and beta.durations[0] < 3.4
        assert slow.scene_tempo(True) > slow.scene_tempo(False) == slow.tempo
        fast = renderer.plan_timing([1.8] + [5.0] * 8)
        assert fast.hook_tempo == 1.0 and fast.scene_tempo(True) == fast.tempo
        capped = renderer.plan_timing([5.0] + [5.0] * 8)
        assert capped.hook_tempo == renderer.HOOK_TEMPO_MAX
        assert renderer.TARGET_MIN_SEC <= slow.total <= config.VIDEO_MAX_SEC

    def test_render_passes_scene_tempo(self, monkeypatch, tmp_path):
        from src.video import renderer
        seen = []
        monkeypatch.setattr(renderer, "find_kr_font", lambda: "/f.ttc")
        monkeypatch.setattr(renderer, "probe_duration", lambda p: 3.4 if "h" in p.name else 5.0)
        monkeypatch.setattr(renderer, "_render_scene",
                            lambda scene, dur, tempo, motion, out, tmp, font, log: seen.append((scene.is_hook, tempo)))
        monkeypatch.setattr(renderer, "_render_outro", lambda *a, **k: None)
        monkeypatch.setattr(renderer, "_run", lambda cmd, log: None)
        monkeypatch.setattr(renderer.assets, "find_bgm", lambda *a, **k: None)
        scenes = [renderer.SceneInput(tmp_path / "i.png", tmp_path / ("h.wav" if i == 0 else f"b{i}.wav"),
                                      "x", i == 0, None) for i in range(9)]
        try:
            renderer.render(scenes, None, tmp_path / "out" / "v.mp4")
        except Exception:  # noqa: BLE001 — 뒤 단계(파일 없음)는 이 테스트 범위 밖
            pass
        hook_tempo = [t for h, t in seen if h][0]
        body_tempo = {t for h, t in seen if not h}
        assert len(body_tempo) == 1 and hook_tempo > body_tempo.pop()

    def test_image_slots_interleaved(self):
        slots = script_writer.BEAT_IMAGE_SLOT
        assert config.SHORTS_IMAGE_COUNT == 7
        assert len(slots) == script_writer.BEAT_COUNT and set(slots) == set(range(config.SHORTS_IMAGE_COUNT))
        assert all(slots[i] != slots[i + 1] for i in range(len(slots) - 1))   # 비트마다 그림이 바뀐다

    def test_seven_distinct_shot_directives(self):
        from src.video import image_gen
        assert len(set(image_gen.SHOT_DIRECTIVES)) == config.SHORTS_IMAGE_COUNT
        sp = script_writer.system_prompt("GOC")
        assert f"정확히 {config.SHORTS_IMAGE_COUNT}개" in sp

    def test_first_person_rejected_quotes_allowed(self):
        raw = _goc_raw()
        raw["body"][4] = "낮게 내려와 거리의 불빛을 하나하나 눈에 담는 것 그것이 나의 일이다"
        issues = script_writer.validate(raw, hooks.HOOK_C, character="GOC")
        assert any("본문5 1인칭" in i for i in issues)
        raw["body"][4] = "GOC 는 낮게 내려와 \"지키는 것이 나의 일이다\" 하고 조용히 말한다"
        assert not any("1인칭" in i for i in script_writer.validate(raw, hooks.HOOK_C, character="GOC"))
        raw["body"][4] = "빛나는 거리 위로 내일의 바람이 천천히 불어오기 시작하는 밤입니다"
        assert not any("1인칭" in i for i in script_writer.validate(raw, hooks.HOOK_C, character="GOC"))

    def test_hearsay_rejected(self):
        for line in ("지난주 고용 소식에 연준의 선택이 가볍게 느껴졌다는 이야기가 들린다",
                     "반도체 실적 소식에 어깨가 가벼워졌다는 말이 들려온다 요즘 거리마다",
                     "국채 시장 쪽에서 조용한 소식이 들려온다 바람이 잦아드는 밤에"):   # v1.8.5: '~다고 한다'는 범위 밖
            raw = _goc_raw()
            raw["body"][2] = line
            issues = script_writer.validate(raw, hooks.HOOK_C, character="GOC")
            assert any("전언형" in i for i in issues), line
        raw = _goc_raw()
        assert not any("전언형" in i for i in script_writer.validate(raw, hooks.HOOK_C, character="GOC"))

    def test_system_prompt_rules(self):
        sp = script_writer.system_prompt("GOC")
        assert "전해 들은 말로 시장 반응" in sp and "3인칭 관찰자" in sp
        assert "결과를 정리하지 말고" in script_writer.day_context(dt.date(2026, 10, 4))



# ─────────────────────────────────────────────────────────────────────────────
# L. v1.8.5 사전 점검 — 문체 검사 오탐 축소 · 마지막 시도 경고 통과 · 사용량 로그
# ─────────────────────────────────────────────────────────────────────────────
class TestL_StyleRulePrecheck:
    # 반드시 잡아야 하는 문장(운영 베타 실제 위반 + 대표형)
    FIRST_PERSON_TRUE = (
        "그것이 나의 일이다",
        "나는 알고 있다 이 거리가 다시 밝아진다는 것을",
        "내가 지켜야 할 것은 사람들의 하루다",
        "오늘도 나를 기다리는 불빛이 있다",
        "저는 조용히 거리를 내려다봅니다",
    )
    # 걸리면 안 되는 문장(동사 '나다'·낱말 안)
    FIRST_PERSON_FALSE = (
        "빛이 나는 거리 위로 바람이 분다",
        "소리가 나는 쪽으로 GOC 가 고개를 돌린다",
        "생각이 나는 대로 발걸음을 옮긴다",
        "땀이 나도 GOC 는 멈추지 않는다",
        "빛나는 거리 위로 내일의 바람이 분다",
        "빛이  나는 거리",                      # 공백 두 칸
    )
    HEARSAY_TRUE = (
        "가볍게 느껴졌다는 이야기가 들린다",
        "어깨가 가벼워졌다는 말이 들려온다",
        "멀리서 조용한 소식이 들려온다",
        "곧 바뀐다는 소문이 돈다",
        "그렇게 끝났다는 후문",
    )
    HEARSAY_FALSE = (
        "GOC 는 끝까지 거리를 지키겠다고 한다",
        "GOC 는 서두르지 않겠다고 한다",
        "새로운 소식이 나온다 해도 GOC 는 흔들리지 않는다",
        "바람 소리가 들린다 GOC 가 고개를 든다",
    )

    def test_first_person_pattern(self):
        for line in self.FIRST_PERSON_TRUE:
            assert script_writer.FIRST_PERSON_PATTERN.search(line), line
        for line in self.FIRST_PERSON_FALSE:
            assert not script_writer.FIRST_PERSON_PATTERN.search(line), line

    def test_hearsay_pattern(self):
        for line in self.HEARSAY_TRUE:
            assert script_writer.HEARSAY_PATTERN.search(line), line
        for line in self.HEARSAY_FALSE:
            assert not script_writer.HEARSAY_PATTERN.search(line), line

    def test_sentence_start_first_person_still_caught(self):
        # 문장 시작 '나는'은 앞에 '이/가 '가 없으므로 1인칭으로 잡는다
        assert script_writer.FIRST_PERSON_PATTERN.search("나는 이 거리를 지킨다")
        assert script_writer.FIRST_PERSON_PATTERN.search("오늘도, 나도 함께 걷는다")
        # 실제 운영 대본(10/04)의 1인칭 — '도' 뒤의 '나는'은 놓치면 안 된다
        assert script_writer.FIRST_PERSON_PATTERN.search("다음 한 주에도 나는 이 자리에서 조용히 지켜볼 것이다")
        assert script_writer.FIRST_PERSON_PATTERN.search("결이 있다는 걸, 나는 조용히 알고 있다")

    def test_split_issues(self):
        issues = ["본문2 1인칭 서술 — x", "본문3 전언형 암시('…') — y", "본문1 3자 — 20~40자 필요",
                  "주말인데 '오늘 시장' 표현 — z"]
        hard, soft = script_writer.split_issues(issues)
        assert soft == issues[:2] and hard == issues[2:]

    def _raw_with(self, idx, line):
        raw = _goc_raw()
        raw["body"][idx] = line
        return raw

    def test_soft_only_final_attempt_passes_with_warning(self, monkeypatch):
        bad = self._raw_with(4, "낮게 내려와 거리의 불빛을 하나하나 눈에 담는 것 그것이 나의 일이다")
        calls = []

        def fake(api_key, prompt, character="EDT"):
            calls.append(prompt)
            return bad

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        mood = mood_source.Mood("rss", ("금리",), "관망")
        s = script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1", mood=mood, character="GOC")
        assert len(calls) == config.SHORTS_SCRIPT_ATTEMPTS          # 앞 시도는 재시도했다
        assert s.warnings and all("1인칭" in w for w in s.warnings)
        assert s.to_dict()["warnings"] == list(s.warnings)

    def test_soft_violation_retried_before_final(self, monkeypatch):
        bad = self._raw_with(4, "낮게 내려와 거리의 불빛을 하나하나 눈에 담는 것 그것이 나의 일이다")
        seq = [bad, _goc_raw()]

        def fake(api_key, prompt, character="EDT"):
            return seq.pop(0)

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        mood = mood_source.Mood("rss", ("금리",), "관망")
        s = script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1", mood=mood, character="GOC")
        assert s.warnings == () and "warnings" not in s.to_dict()

    def test_soft_fallback_survives_later_api_failure(self, monkeypatch):
        # 리뷰 v1.8.5: 2회차가 문체 위반만, 3회차가 API 실패여도 편을 버리지 않는다(2회차 결과 사용)
        soft_raw = self._raw_with(4, "낮게 내려와 거리의 불빛을 하나하나 눈에 담는 것 그것이 나의 일이다")
        hard_raw = self._raw_with(0, "짧다")
        seq = [hard_raw, soft_raw, script_writer.ScriptError("Claude API 529: overloaded")]

        def fake(api_key, prompt, character="EDT"):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        mood = mood_source.Mood("rss", ("금리",), "관망")
        used = set()
        s = script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1", mood=mood,
                                       character="GOC", used_captions=used)
        assert s.warnings and "나의 일이다" in s.beats[5].narration
        assert used == {soft_raw["post_caption"].strip()}           # 쓰인 캡션만 기록

    def test_first_soft_candidate_kept(self, monkeypatch):
        a = self._raw_with(4, "낮게 내려와 거리의 불빛을 하나하나 눈에 담는 것 그것이 나의 일이다")
        b = self._raw_with(2, "반도체 실적 소식에 어깨가 가벼워졌다는 말이 들려온다 요즘 거리마다")
        seq = [a, b, b]
        monkeypatch.setattr(script_writer, "_call_claude", lambda *a_, **k: seq.pop(0))
        mood = mood_source.Mood("rss", ("금리",), "관망")
        s = script_writer.write_script("k", content_id="sv-20261005-1", fmt="F1", mood=mood, character="GOC")
        assert all("1인칭" in w for w in s.warnings)

    def test_hard_violation_still_raises(self, monkeypatch):
        # 소프트 + 하드(주말 '오늘 시장')가 함께 남으면 끝까지 막는다
        bad = self._raw_with(4, "그것이 나의 일이다 오늘 시장은 조용히 숨을 고르는 하루였다고")

        def fake(api_key, prompt, character="EDT"):
            return bad

        monkeypatch.setattr(script_writer, "_call_claude", fake)
        mood = mood_source.Mood("rss", ("금리",), "관망")
        with pytest.raises(script_writer.ScriptError):
            script_writer.write_script("k", content_id="sv-20261004-1", fmt="F1", mood=mood, character="GOC")

    def test_usage_logged_on_success(self, monkeypatch, caplog):
        class R:
            status_code = 200
            text = ""

            def json(self):
                return {"content": [{"type": "text", "text": '{"hook": "x"}'}],
                        "usage": {"input_tokens": 1200, "output_tokens": 3400}, "stop_reason": "end_turn"}

        monkeypatch.setattr(script_writer.requests, "post", lambda *a, **k: R())
        with caplog.at_level("INFO", logger=script_writer.log.name):
            assert script_writer._call_claude("k", "p", "GOC") == {"hook": "x"}
        assert "output_tokens=3400" in caplog.text and "input_tokens=1200" in caplog.text

    def test_preview_caption(self):
        from src import run_shorts_build as rb
        entry = {"content_id": "sv-20261005-1", "character": "GOC", "fmt": "F1", "channels": ["face"],
                 "caption": "가" * 990, "video": "sv-20261005-1/video.mp4",
                 "script_warnings": ["본문5 1인칭 서술 — 3인칭 관찰자로 쓴다"]}
        text = rb.preview_caption(entry)
        assert "⚠ 문체 검토 필요" in text[:1000]                       # 텔레그램 1000자 절단에도 보인다
        assert text.index("⚠") < text.index("가" * 10)
        many = dict(entry, caption=script_writer.build_caption("가" * script_writer.CAPTION_BODY_MAX),
                    script_warnings=[f"본문{i} 1인칭 서술 — " + "x" * 300 for i in range(1, 18)])
        text = rb.preview_caption(many)
        assert len(text) < 1000 and "외 14건" in text
        assert text.endswith("승인: Actions › 📘🧵 Meta Shorts › Review deployments")
        assert text.count("\n- 본문") == rb.PREVIEW_WARN_MAX
        plain = rb.preview_caption({k: v for k, v in entry.items() if k != "script_warnings"})
        assert "⚠" not in plain and plain.startswith("[Shorts 미리보기] sv-20261005-1 GOC F1 → face\n가")
        assert plain.endswith("승인: Actions › 📘🧵 Meta Shorts › Review deployments")

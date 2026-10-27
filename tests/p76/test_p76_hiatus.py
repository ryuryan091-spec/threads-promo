"""T1-06~11 휴재 콘텐츠: 검사·카드 렌더·게시 계약·상태 전이."""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pytest
import requests
from p76_fake import DB09, FAKE_PAGE_ID, FakeResp, err, jpeg_bytes, kst, png_bytes

from p76 import card_render, graph, hiatus_guard, run_hiatus_publish, settings
from p76.runlog import RunLog

TODAY = date(2026, 11, 10)
GOOD = (
    "시즌 2 정리 — 남겨진 흔적 세 가지\n"
    "본문 요약 문장입니다.\n"
    "※ 여러 사례를 재구성한 픽션입니다.\n"
    "#사건파일76"
)
VOTE = (
    "다음 시즌, 누구의 시점이 궁금하신가요?\n"
    "A. 신입 사원\n"
    "B. 팀장\n"
    "※ 여러 사례를 재구성한 픽션입니다.\n#사건파일76"
)


# ── T1-06 hiatus_guard ───────────────────────────────────
def test_t1_06_guard_pass():
    assert hiatus_guard.check(GOOD, settings.CONTENT_SUMMARY) == []
    assert hiatus_guard.check(VOTE, settings.CONTENT_VOTE) == []


@pytest.mark.parametrize("text,content,code", [
    (GOOD.replace("※ 여러 사례를 재구성한 픽션입니다.", ""), settings.CONTENT_SUMMARY, "hiatus:fiction_notice_missing"),
    (GOOD.replace("#사건파일76", ""), settings.CONTENT_SUMMARY, "hiatus:hashtag_missing"),
    (GOOD + "\n2026.11.09 사건", settings.CONTENT_SUMMARY, "hiatus:real_date_found"),
    (GOOD + "\n11월 9일 밤", settings.CONTENT_SUMMARY, "hiatus:real_date_found"),
    (GOOD + "\n댓글로 남겨 주세요", settings.CONTENT_SUMMARY, "hiatus:pushy_phrase"),
    (VOTE.replace("B. 팀장", ""), settings.CONTENT_VOTE, "hiatus:vote_options"),
    (VOTE + "\nA. 또 하나", settings.CONTENT_VOTE, "hiatus:vote_options"),
    ("", settings.CONTENT_SUMMARY, "hiatus:empty"),
    (GOOD, "", "hiatus:content_type"),
    (GOOD + "\n가나다상사", settings.CONTENT_SUMMARY, "hiatus:banned_term"),
])
def test_t1_06_guard_violations(text, content, code):
    assert code in hiatus_guard.check(text, content, banned_terms=("가나다상사",))


def test_guard_exempts_episode_rules():
    """3요건 줄·D+n 은 본편 전용 — 휴재 콘텐츠에는 요구하지 않는다."""
    assert "⚖️" not in GOOD and "D+" not in GOOD
    assert hiatus_guard.check(GOOD, settings.CONTENT_TEASER) == []


def test_headline_skips_tags_and_notice():
    assert hiatus_guard.headline("#태그\n※ 고지\n\n첫 문장") == "첫 문장"
    assert hiatus_guard.headline("가" * 80).endswith("…")


# ── T1-07 카드 렌더 ──────────────────────────────────────
def fake_chrome(size):
    def runner(cmd, **kwargs):
        out = next(a.split("=", 1)[1] for a in cmd if a.startswith("--screenshot="))
        Path(out).write_bytes(png_bytes(*size))

        class P:
            returncode = 0
        return P()
    return runner


def test_t1_07_render_ok(env, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CHROME_BIN", "chrome")
    path = card_render.render("휴재 1일차", "<b>제목</b>", "시즌 2", str(tmp_path), runner=fake_chrome((1080, 1350)))
    assert card_render.png_size(path) == (1080, 1350)
    html_src = (tmp_path / "card.html").read_text(encoding="utf-8")
    assert "&lt;b&gt;제목&lt;/b&gt;" in html_src and "<script" not in html_src


def test_t1_07_render_wrong_size(env, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CHROME_BIN", "chrome")
    with pytest.raises(card_render.RenderError, match="render:size"):
        card_render.render("a", "b", "c", str(tmp_path), runner=fake_chrome((800, 600)))


def test_render_chrome_missing(env, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CHROME_BIN", "")
    monkeypatch.setattr(card_render.shutil, "which", lambda name: None)
    with pytest.raises(card_render.RenderError, match="chrome_missing"):
        card_render.render("a", "b", "c", str(tmp_path))


@pytest.mark.parametrize("data,ok", [
    (png_bytes(1080, 1350), True), (jpeg_bytes(1080, 1350), True),
    (jpeg_bytes(1080, 1080), False), (png_bytes(1080, 1920), False),
])
def test_attachment_check(env, tmp_path, data, ok):
    path = tmp_path / "img"
    path.write_bytes(data)
    if ok:
        card_render.check_attachment(str(path))
    else:
        with pytest.raises(card_render.RenderError, match="aspect"):
            card_render.check_attachment(str(path))


# ── T1-08 Graph photos 계약 ──────────────────────────────
@pytest.mark.parametrize("response,status,tripped", [
    (FakeResp(200, {"id": "1", "post_id": "61_1"}), "ok", False),
    (err(100), "failed", False),
    (err(190, 401), "failed", True),
    (err(368), "failed", True),
    (err(200, 403), "failed", True),
    (FakeResp(500, {}), "unknown", False),
    (requests.exceptions.ReadTimeout("t"), "unknown", False),
    (requests.exceptions.ConnectTimeout("t"), "failed", False),
])
def test_t1_08_photo_contract(fake_graph, tmp_path, response, status, tripped):
    from p76 import safety
    img = tmp_path / "a.png"
    img.write_bytes(png_bytes(1080, 1350))
    fake_graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = response
    res = graph.post_photo(str(img), "본문")
    assert res["status"] == status
    assert bool(safety.tripped()) is tripped
    assert len(fake_graph.writes()) == 1  # 쓰기 재시도 없음


def test_photo_blocked_after_trip(fake_graph, tmp_path):
    from p76 import safety
    safety.trip("test")
    with pytest.raises(safety.Tripped):
        graph.post_photo(str(tmp_path / "x.png"), "본문")
    assert fake_graph.writes() == []


# ── T1-09~11 게시 흐름 ───────────────────────────────────
@pytest.fixture
def publish_env(fake_notion, fake_graph, monkeypatch, tmp_path):
    def render(label, headline, meta, work_dir, runner=None):
        out = Path(work_dir) / "card.png"
        out.write_bytes(png_bytes(1080, 1350))
        return str(out)
    monkeypatch.setattr(card_render, "render", render)
    fake_graph.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = FakeResp(200, {"id": "9", "post_id": "61_9"})
    return fake_notion, fake_graph


def add_row(fake, day, state=settings.S_APPROVED, reviewed=True, body=GOOD, content=settings.CONTENT_SUMMARY, **extra):
    return fake.add(DB09, **{
        settings.C_KEY: day.isoformat(), settings.C_DATE: day.isoformat(), settings.C_KIND: settings.KIND_HIATUS_CONTENT,
        settings.C_STATE: state, settings.C_REVIEWED: reviewed, settings.C_BODY: body, settings.C_CONTENT: content,
        settings.C_HIATUS_DAY: 1, settings.C_SEASON: 2, **extra,
    })


def run(today=TODAY):
    log = RunLog("hiatus_publish", "t")
    run_hiatus_publish.body(log, now=datetime.fromisoformat(kst(today, 20, 30)))
    return log


def test_t1_09_happy_path(publish_env):
    fake, g = publish_env
    row = add_row(fake, TODAY)
    log = run()
    assert fake.val(row, settings.C_STATE) == settings.S_DONE
    assert fake.val(row, settings.C_FEED_ID) == "61_9"
    assert fake.val(row, settings.C_PUBLISHED_AT)
    assert len(g.writes()) == 1 and g.writes()[0][2]["caption"] == GOOD
    assert log.status == "ok"
    # 같은 날 다시 실행해도 중복 게시 0
    add_row(fake, TODAY)
    run()
    assert len(g.writes()) == 1


def test_t1_10_claim_lost(publish_env, monkeypatch):
    fake, g = publish_env
    row = add_row(fake, TODAY)
    original = fake.request

    def racing(method, path, body=None, retry=True):
        if method == "GET" and path == f"/pages/{row['id']}":
            row["properties"][settings.C_STATE]["select"] = {"name": settings.S_RUNNING}
        return original(method, path, body, retry)
    monkeypatch.setattr("p76.notion._request", racing)
    log = run()
    assert g.writes() == [] and "claim_lost" in log.codes


def test_t1_11_selection_rules(publish_env):
    fake, g = publish_env
    add_row(fake, TODAY, reviewed=False)
    add_row(fake, date(2026, 11, 13))  # 미래
    assert run().status == "ok" and g.writes() == []
    a = add_row(fake, date(2026, 11, 9))
    b = add_row(fake, TODAY)
    run()
    assert fake.val(a, settings.C_STATE) == settings.S_DONE
    assert fake.val(b, settings.C_STATE) == settings.S_APPROVED
    assert len(g.writes()) == 1


def test_guard_failure_marks_failed(publish_env):
    fake, g = publish_env
    row = add_row(fake, TODAY, body=GOOD.replace("#사건파일76", ""))
    log = run()
    assert fake.val(row, settings.C_STATE) == settings.S_FAILED
    assert "hiatus:hashtag_missing" in fake.val(row, settings.C_RESULT)
    assert g.writes() == [] and log.status == "failed"


def test_unknown_marks_check_needed(publish_env):
    fake, g = publish_env
    g.handlers[("POST", f"{FAKE_PAGE_ID}/photos")] = requests.exceptions.ReadTimeout("t")
    row = add_row(fake, TODAY)
    run()
    assert fake.val(row, settings.C_STATE) == settings.S_CHECK


def test_existing_feed_id_not_reposted(publish_env):
    fake, g = publish_env
    row = add_row(fake, TODAY, **{settings.C_FEED_ID: "61_1"})
    run()
    assert g.writes() == [] and fake.val(row, settings.C_STATE) == settings.S_DONE


@pytest.mark.parametrize("mode", ["dry_run", "shadow"])
def test_non_live_modes_do_not_publish(publish_env, env, mode):
    fake, g = publish_env
    env(PAGE76_MODE=mode)
    row = add_row(fake, TODAY)
    run()
    assert g.writes() == [] and fake.writes() == []
    assert fake.val(row, settings.C_STATE) == settings.S_APPROVED


def test_switch_off(publish_env, env):
    fake, g = publish_env
    env(PAGE76_HIATUS_PUBLISH_ENABLED="false")
    add_row(fake, TODAY)
    run()
    assert g.writes() == [] and fake.writes() == []


def test_daily_cap(publish_env):
    fake, g = publish_env
    add_row(fake, date(2026, 11, 9), state=settings.S_DONE, **{settings.C_PUBLISHED_AT: kst(TODAY, 20, 10)})
    add_row(fake, TODAY)
    run()
    assert g.writes() == []


def test_stuck_rows_alert(publish_env, caplog):
    caplog.set_level(logging.INFO)
    fake, _ = publish_env
    add_row(fake, date(2026, 11, 9), state=settings.S_RUNNING)
    log = run()
    assert "stuck_rows" in log.codes
    assert "발행중 잔류 1건" in caplog.text


def test_attachment_preferred(publish_env, monkeypatch, tmp_path):
    fake, g = publish_env
    row = add_row(fake, TODAY, **{settings.C_IMAGE: [{"type": "external", "external": {"url": "https://example.test/a.jpg"}}]})
    downloaded = []

    def download(url, dest):
        downloaded.append(url)
        Path(dest).write_bytes(jpeg_bytes(1080, 1350))
        return str(dest)
    monkeypatch.setattr(run_hiatus_publish, "download", download)
    run()
    assert downloaded and fake.val(row, settings.C_STATE) == settings.S_DONE

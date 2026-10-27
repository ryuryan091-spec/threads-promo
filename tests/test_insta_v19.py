"""v1.9.0 Instagram 릴스 — 클라이언트 · 게시 러너 · 사전 점검 · 워크플로.

네트워크는 requests.request 를 가짜로 바꿔 (메서드, URL) 별로 응답한다. 실제 호출 없음.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import yaml

from src import config, face_client, insta_client, safety
from src.insta_client import InstaApiError, InstaClient

KST = ZoneInfo("Asia/Seoul")
ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN = "EAA" + "t" * 30
GRAPH = config.INSTA_GRAPH_BASE


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeGraph:
    """(메서드, URL 접미) → 응답 목록. 같은 키를 여러 번 부르면 차례대로 꺼낸다(마지막은 반복)."""

    def __init__(self, routes: dict[tuple[str, str], list[_Resp]]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), queue in self.routes.items():
            if m == method and url.endswith(suffix):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"예상하지 않은 요청 {method} {url}")

    def called(self, method: str, suffix: str) -> list[dict]:
        return [kw for m, u, kw in self.calls if m == method and u.endswith(suffix)]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    safety.reset_circuit()
    monkeypatch.setattr(insta_client.time, "sleep", lambda s: None)
    monkeypatch.setattr(face_client.time, "sleep", lambda s: None)
    yield
    safety.reset_circuit()


def _graph(monkeypatch, routes) -> FakeGraph:
    fake = FakeGraph(routes)
    monkeypatch.setattr(face_client.requests, "request", fake)
    return fake


def _ok_routes(status_codes=("IN_PROGRESS", "FINISHED"), create=None):
    return {
        ("GET", "/123"): [_Resp(200, {"instagram_business_account": {"id": "17841", "username": "tiger"}})],
        ("POST", "/17841/media"): [_Resp(200, create or {"id": "c1"})],
        ("POST", "/c1"): [_Resp(200, {"success": True, "message": "Upload successful."})],
        ("GET", "/c1"): [_Resp(200, {"status_code": s, "status": ""}) for s in status_codes],
        ("POST", "/17841/media_publish"): [_Resp(200, {"id": "m1"})],
    }


# ===========================================================================
# 클라이언트
# ===========================================================================


class TestClient:
    def test_account_lookup_and_cache(self, monkeypatch):
        g = _graph(monkeypatch, _ok_routes())
        client = InstaClient("123", TOKEN)
        assert client.account() == ("17841", "tiger")
        assert client.account() == ("17841", "tiger")
        assert len(g.called("GET", "/123")) == 1
        assert "instagram_business_account" in g.called("GET", "/123")[0]["params"]["fields"]

    def test_not_linked(self, monkeypatch):
        _graph(monkeypatch, {("GET", "/123"): [_Resp(200, {"id": "123"})]})
        with pytest.raises(InstaApiError, match="Instagram 프로페셔널 계정이 없습니다"):
            InstaClient("123", TOKEN).account()

    def test_user_id_given_skips_lookup(self, monkeypatch):
        routes = _ok_routes()
        del routes[("GET", "/123")]
        _graph(monkeypatch, routes)
        assert InstaClient("123", TOKEN, "17841").ig_id == "17841"

    def test_requires_page(self):
        with pytest.raises(ValueError):
            InstaClient("", "")

    def test_publish_happy_path(self, monkeypatch, tmp_path):
        g = _graph(monkeypatch, _ok_routes())
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x" * 1234)
        result = InstaClient("123", TOKEN).publish_reel(video, "캡션")
        assert result == insta_client.InstaResult("c1", "m1", insta_client.STATUS_PUBLISHED)

        body = g.called("POST", "/17841/media")[0]["json"]
        assert body["media_type"] == "REELS" and body["upload_type"] == "resumable"
        assert body["caption"] == "캡션"
        assert body["is_ai_generated"] is True and body["share_to_feed"] is True

        up_url = next(u for m, u, _ in g.calls if m == "POST" and u.endswith("/c1"))
        assert up_url == f"{config.INSTA_RUPLOAD_BASE}/c1"
        headers = g.called("POST", "/c1")[0]["headers"]
        assert headers["Authorization"] == f"OAuth {TOKEN}"
        assert headers["offset"] == "0" and headers["file_size"] == "1234"
        assert g.called("POST", "/17841/media_publish")[0]["json"]["creation_id"] == "c1"

    def test_uses_returned_upload_uri(self, monkeypatch, tmp_path):
        routes = _ok_routes(create={"id": "c1", "uri": "https://rupload.example/ig-api-upload/v99/c1"})
        g = _graph(monkeypatch, routes)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        InstaClient("123", TOKEN).publish_reel(video, "캡션")
        assert any(u == "https://rupload.example/ig-api-upload/v99/c1" for m, u, _ in g.calls)

    @pytest.mark.parametrize("code", ["ERROR", "EXPIRED"])
    def test_processing_failure_is_definitive_and_not_published(self, monkeypatch, tmp_path, code):
        g = _graph(monkeypatch, _ok_routes(status_codes=("IN_PROGRESS", code)))
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        with pytest.raises(InstaApiError) as info:
            InstaClient("123", TOKEN).publish_reel(video, "캡션")
        assert info.value.definitive and info.value.video_id == "c1"
        assert g.called("POST", "/17841/media_publish") == []

    def test_timeout_does_not_publish(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "INSTA_STATUS_MAX_SEC", 0)
        g = _graph(monkeypatch, _ok_routes(status_codes=("IN_PROGRESS",)))
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        result = InstaClient("123", TOKEN).publish_reel(video, "캡션")
        assert result.media_id == "" and result.container_id == "c1"
        assert g.called("POST", "/17841/media_publish") == []

    def test_token_invalid_trips_circuit(self, monkeypatch, tmp_path):
        routes = _ok_routes()
        routes[("POST", "/17841/media")] = [_Resp(400, {"error": {"code": 190, "message": "bad"}})]
        _graph(monkeypatch, routes)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        with pytest.raises(InstaApiError) as info:
            InstaClient("123", TOKEN).publish_reel(video, "캡션")
        assert info.value.is_auth_error and info.value.before_session
        assert "Instagram API" in str(info.value)
        assert safety.tripped() is not None
        with pytest.raises(face_client.FaceApiError):
            safety.guard_write()          # 같은 토큰 — 이후 Facebook 쓰기도 멈춘다

    def test_token_not_in_error_text(self, monkeypatch, tmp_path):
        routes = _ok_routes()
        routes[("POST", "/17841/media")] = [_Resp(400, {"error": {"code": 100, "message": f"x {TOKEN}"}})]
        _graph(monkeypatch, routes)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        with pytest.raises(InstaApiError) as info:
            InstaClient("123", TOKEN).publish_reel(video, "캡션")
        assert TOKEN not in str(info.value)

    def test_publishing_limit(self, monkeypatch):
        _graph(monkeypatch, {
            ("GET", "/content_publishing_limit"): [
                _Resp(200, {"data": [{"quota_usage": 3, "config": {"quota_total": 100}}]})],
        })
        assert InstaClient("123", TOKEN, "17841").publishing_limit() == (3, 100)

    def test_publishing_limit_unknown(self, monkeypatch):
        _graph(monkeypatch, {("GET", "/content_publishing_limit"): [_Resp(200, {"data": []})]})
        assert InstaClient("123", TOKEN, "17841").publishing_limit() == (None, None)


# ===========================================================================
# 게시 러너
# ===========================================================================


def _manifest(base: pathlib.Path, ids, channels=None):
    channels = channels or [("face",)] * len(ids)
    items = []
    for cid, ch in zip(ids, channels, strict=False):
        (base / cid).mkdir(parents=True, exist_ok=True)
        (base / cid / "video.mp4").write_bytes(b"v")
        items.append({"content_id": cid, "fmt": "F1", "channels": list(ch),
                      "caption": f"캡션 {cid}", "video": f"{cid}/video.mp4"})
    (base / "manifest.json").write_text(json.dumps({"items": items}, ensure_ascii=False), "utf-8")


def _face_ok():
    face = mock.Mock()
    face.recent_descriptions.return_value = []
    face.publish_reel.return_value = ("v1", face_client.ReelStatus("ready", "complete", "complete",
                                                                     "complete", ""))
    return face


def _insta_ok(captions=(), limit=(0, 100)):
    insta = mock.Mock()
    insta.recent_captions.return_value = list(captions)
    insta.publishing_limit.return_value = limit
    insta.publish_reel.return_value = insta_client.InstaResult("c1", "m1", "PUBLISHED")
    return insta


class TestRunner:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        from src import run_shorts_publish as rp
        base = {"AUTOMATION_ENABLED": True, "WARMUP_UNTIL": "", "FACE_ENABLED": True,
                "FACE_STORY_ENABLED": False, "INSTA_ENABLED": True, "INSTA_DAILY_MAX": 1,
                "SHORTS_THREADS_ENABLED": False}
        for key, value in base.items():
            monkeypatch.setattr(config, key, value)
        monkeypatch.setenv("SHORTS_OUT_DIR", str(tmp_path))
        monkeypatch.setenv("FACE_PAGE_ID", "123")
        monkeypatch.setenv("FACE_PAGE_TOKEN", TOKEN)
        monkeypatch.setenv("SHORTS_PUBLISH_CHANNELS", "face,insta")
        monkeypatch.setenv("DRY_RUN", "false")
        sent: list[str] = []
        monkeypatch.setattr(rp, "_notify", sent.append)
        monkeypatch.setattr(rp, "_sleep_until", lambda t: None)
        fixed = dt.datetime.combine(dt.datetime.now(KST).date(), dt.time(11, 0), tzinfo=KST)

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz is None or tz == KST else fixed.astimezone(tz)

        monkeypatch.setattr(rp.dt, "datetime", _DT)
        ids = [f"sv-{fixed.date():%Y%m%d}-1", f"sv-{fixed.date():%Y%m%d}-2"]
        return rp, tmp_path, ids, sent

    def _clients(self, monkeypatch, rp, face=None, insta=None):
        face_ctor = mock.Mock(return_value=face or _face_ok())
        insta_ctor = mock.Mock(return_value=insta or _insta_ok())
        monkeypatch.setattr(rp, "FaceClient", face_ctor)
        monkeypatch.setattr(rp, "InstaClient", insta_ctor)
        return face_ctor, insta_ctor

    def test_posts_facebook_and_instagram_same_caption(self, env, monkeypatch):
        rp, tmp, ids, sent = env
        _manifest(tmp, ids[:1])
        face, insta = _face_ok(), _insta_ok()
        self._clients(monkeypatch, rp, face, insta)
        assert rp.run() == 0
        face.publish_reel.assert_called_once()
        insta.publish_reel.assert_called_once()
        assert insta.publish_reel.call_args.args[1] == f"캡션 {ids[0]}"
        assert "IG " in sent[-1] and "media_id=m1" in sent[-1]

    def test_daily_max_limits_instagram(self, env, monkeypatch):
        rp, tmp, ids, _ = env
        _manifest(tmp, ids)
        face, insta = _face_ok(), _insta_ok()
        self._clients(monkeypatch, rp, face, insta)
        assert rp.run() == 0
        assert face.publish_reel.call_count == 2
        assert insta.publish_reel.call_count == 1

    def test_instagram_independent_of_facebook_switch(self, env, monkeypatch):
        rp, tmp, ids, _ = env
        monkeypatch.setattr(config, "FACE_ENABLED", False)
        _manifest(tmp, ids[:1])
        insta = _insta_ok()
        face_ctor, _ = self._clients(monkeypatch, rp, insta=insta)
        assert rp.run() == 0
        face_ctor.assert_not_called()
        insta.publish_reel.assert_called_once()

    @pytest.mark.parametrize("override", [
        {"INSTA_ENABLED": False}, {"INSTA_DAILY_MAX": 0}, {"AUTOMATION_ENABLED": False},
    ])
    def test_switches_block_instagram(self, env, monkeypatch, override):
        rp, tmp, ids, _ = env
        for key, value in override.items():
            monkeypatch.setattr(config, key, value)
        _manifest(tmp, ids[:1])
        _, insta_ctor = self._clients(monkeypatch, rp)
        assert rp.run() == 0
        insta_ctor.assert_not_called()

    def test_channel_not_selected(self, env, monkeypatch):
        rp, tmp, ids, _ = env
        monkeypatch.setenv("SHORTS_PUBLISH_CHANNELS", "face")
        _manifest(tmp, ids[:1])
        _, insta_ctor = self._clients(monkeypatch, rp)
        assert rp.run() == 0
        insta_ctor.assert_not_called()

    def test_threads_only_items_not_sent_to_instagram(self, env, monkeypatch):
        rp, tmp, ids, _ = env
        _manifest(tmp, ids[:1], channels=[("threads",)])
        _, insta_ctor = self._clients(monkeypatch, rp)
        assert rp.run() == 0
        insta_ctor.assert_not_called()

    def test_dry_run_creates_no_clients(self, env, monkeypatch):
        rp, tmp, ids, _ = env
        monkeypatch.setenv("DRY_RUN", "true")
        _manifest(tmp, ids[:1])
        face_ctor, insta_ctor = self._clients(monkeypatch, rp)
        assert rp.run() == 0
        face_ctor.assert_not_called()
        insta_ctor.assert_not_called()

    def test_duplicate_caption_skipped(self, env, monkeypatch):
        rp, tmp, ids, sent = env
        _manifest(tmp, ids[:1])
        insta = _insta_ok(captions=[("m0", f"  캡션 {ids[0]} ")])
        self._clients(monkeypatch, rp, insta=insta)
        assert rp.run() == 0
        insta.publish_reel.assert_not_called()
        assert "이미 있어 건너뜀" in sent[-1]

    def test_quota_exhausted_skipped(self, env, monkeypatch):
        rp, tmp, ids, sent = env
        _manifest(tmp, ids[:1])
        insta = _insta_ok(limit=(100, 100))
        self._clients(monkeypatch, rp, insta=insta)
        assert rp.run() == 0
        insta.publish_reel.assert_not_called()
        assert "한도 소진" in sent[-1]

    def test_quota_lookup_failure_does_not_block(self, env, monkeypatch):
        rp, tmp, ids, _ = env
        _manifest(tmp, ids[:1])
        insta = _insta_ok()
        insta.publishing_limit.side_effect = InstaApiError(400, "x", code=100)
        self._clients(monkeypatch, rp, insta=insta)
        assert rp.run() == 0
        insta.publish_reel.assert_called_once()

    def test_timeout_reported_not_failed(self, env, monkeypatch):
        rp, tmp, ids, sent = env
        _manifest(tmp, ids[:1])
        insta = _insta_ok()
        insta.publish_reel.return_value = insta_client.InstaResult("c9", "", "TIMEOUT")
        self._clients(monkeypatch, rp, insta=insta)
        assert rp.run() == 0
        assert "게시하지 않음" in sent[-1] and "c9" in sent[-1]

    def test_instagram_failure_exit_4_facebook_kept(self, env, monkeypatch):
        rp, tmp, ids, sent = env
        _manifest(tmp, ids[:1])
        face, insta = _face_ok(), _insta_ok()
        err = InstaApiError(200, "처리 실패")
        err.definitive = True
        insta.publish_reel.side_effect = err
        self._clients(monkeypatch, rp, face, insta)
        assert rp.run() == rp.FAIL_EXIT_CODE
        face.publish_reel.assert_called_once()
        assert "처리 실패 확정" in sent[-1]

    def test_instagram_token_fatal_exit_7(self, env, monkeypatch):
        rp, tmp, ids, sent = env
        monkeypatch.setattr(config, "FACE_ENABLED", False)
        _manifest(tmp, ids[:1])
        insta = _insta_ok()
        insta.recent_captions.side_effect = InstaApiError(400, "x", code=190)
        self._clients(monkeypatch, rp, insta=insta)
        assert rp.main() == safety.FATAL_EXIT_CODE
        assert sent[-1].startswith("[Instagram][최우선]")
        assert "INSTA_ENABLED=false" in sent[-1]

    def test_fatal_message_facebook_unchanged(self):
        from src import run_shorts_publish as rp
        msg = rp.face_fatal_message(face_client.FaceApiError(400, "x", code=190))
        assert msg.startswith("[Facebook][최우선]") and "FACE_ENABLED=false" in msg


# ===========================================================================
# 사전 점검
# ===========================================================================


class TestPreflight:
    @pytest.fixture
    def pf(self, monkeypatch):
        import importlib.util
        spec = importlib.util.spec_from_file_location("face_preflight", ROOT / "scripts" / "face_preflight.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setenv("FACE_PAGE_ID", "123")
        monkeypatch.setenv("FACE_PAGE_TOKEN", TOKEN)
        return mod

    def _rows(self, pf, scopes, *, enabled=True, limit=(0, 100), linked=True, monkeypatch=None):
        monkeypatch.setattr(config, "INSTA_ENABLED", enabled)
        client = mock.Mock()
        if linked:
            client.account.return_value = ("17841", "tiger")
        else:
            client.account.side_effect = InstaApiError(404, "연결 없음")
        client.publishing_limit.return_value = limit
        monkeypatch.setattr(pf, "InstaClient", mock.Mock(return_value=client))
        r = pf.Report()
        pf.check_instagram(r, scopes)
        return {cid: (status, detail) for cid, _, status, detail in r.rows}

    ALL = ["pages_show_list", "pages_read_engagement", "pages_manage_posts",
           "instagram_basic", "instagram_content_publish"]

    def test_all_ok(self, pf, monkeypatch):
        rows = self._rows(pf, self.ALL, monkeypatch=monkeypatch)
        assert rows["P10"][0] == "OK" and "@tiger" in rows["P10"][1]
        assert rows["P11"][0] == "OK" and "0/100" in rows["P11"][1]

    def test_missing_scope_fails_when_enabled(self, pf, monkeypatch):
        rows = self._rows(pf, self.ALL[:3], monkeypatch=monkeypatch)
        assert rows["P11"][0] == "FAIL" and "instagram_content_publish" in rows["P11"][1]

    def test_missing_scope_warns_when_disabled(self, pf, monkeypatch):
        rows = self._rows(pf, self.ALL[:3], enabled=False, monkeypatch=monkeypatch)
        assert rows["P11"][0] == "WARN" and "INSTA_ENABLED=false" in rows["P11"][1]

    def test_not_linked(self, pf, monkeypatch):
        rows = self._rows(pf, self.ALL, linked=False, monkeypatch=monkeypatch)
        assert rows["P10"][0] == "FAIL" and rows["P11"][0] == "FAIL"

    def test_unknown_scopes_warn(self, pf, monkeypatch):
        rows = self._rows(pf, None, monkeypatch=monkeypatch)
        assert rows["P11"][0] == "WARN"

    def test_quota_exhausted(self, pf, monkeypatch):
        rows = self._rows(pf, self.ALL, limit=(100, 100), monkeypatch=monkeypatch)
        assert rows["P11"][0] == "FAIL" and "한도 소진" in rows["P11"][1]


# ===========================================================================
# 설정 · 워크플로
# ===========================================================================


class TestConfigAndWorkflow:
    def test_safe_defaults(self):
        assert config.SAFETY_VARIABLE_DEFAULTS["INSTA_ENABLED"] == "false"
        assert config.SAFETY_VARIABLE_DEFAULTS["INSTA_DAILY_MAX"] == "1"
        assert config.INSTA_IS_AI_GENERATED is True
        assert config.INSTA_RUPLOAD_BASE.startswith("https://rupload.facebook.com/ig-api-upload/v")

    @pytest.mark.parametrize("name", ["face_publish.yml", "face_preflight.yml"])
    def test_workflow_passes_insta_vars(self, name):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "INSTA_ENABLED: ${{ vars.INSTA_ENABLED || 'false' }}" in text
        assert "INSTA_DAILY_MAX: ${{ vars.INSTA_DAILY_MAX || '1' }}" in text

    def test_publish_channels_include_insta(self):
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / "face_publish.yml").read_text(encoding="utf-8"))
        step = wf["jobs"]["publish"]["steps"][-1]
        assert step["env"]["SHORTS_PUBLISH_CHANNELS"] == "face,insta"
        assert "INSTA_ACCESS_TOKEN" not in step["env"]     # 새 토큰 없음 — 페이지 토큰 재사용

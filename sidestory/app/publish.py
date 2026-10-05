"""P2 publish / verify stages (Facebook Page).

publish (dry run unless live):
  status assembled → gates (SG-1, SG-6 slides on disk == manifest, SG-5 copy, internal codes,
  caption length) → dry run: credential check + dry_run publication row, status unchanged
  → live: assembled → publishing → upload 8 unpublished photos → one feed post with the
  photos in order → side_publications row → published.

Duplicate-post safety:
  - one live row per episode+channel (DB unique index) and a `published` status check first;
  - `publishing` is a lock (compare-and-set), a second run cannot start a post;
  - a feed POST whose outcome is unknown becomes `hold` with publish_hold "AMBIGUOUS: ...";
    the next run first looks for the post on the Page (same message) and records it instead
    of posting again. Upload failures happen before any visible post: publish_hold "SAFE: ...".
verify: read-only credential check and, when published, the post's permalink.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sidestory.app.p1 import StageResult, _loggable, sha256_file, utcnow
from sidestory.app.pipeline import side_episode_id
from sidestory.core import gates
from sidestory.core.models import EchoPack, GateResult
from sidestory.core.script import CAPTION_MAX, SideScript, find_internal_codes
from sidestory.ports.main_feed import MainFeedReader
from sidestory.ports.publisher import Publisher, PublishError
from sidestory.ports.store import SideStore

CHANNEL = "facebook"
SLIDE_COUNT = 8
AMBIGUOUS = "AMBIGUOUS"
SAFE = "SAFE"


@dataclass
class PublishDeps:
    feed: MainFeedReader
    store: SideStore
    publisher: Publisher | None = None
    live: bool = False
    # P4: minimum age of the assembly before posting (scheduled runs: 60). 0 = no wait.
    wait_minutes: int = 0
    now: Callable[[], datetime] | None = None     # None = current UTC time


class _Hold(RuntimeError):
    def __init__(self, reason: str, kind: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.kind, self.detail = kind, detail or {}


def _slides_gate(row: dict[str, Any]) -> tuple[GateResult, list[Path]]:
    slides = row.get("slides_json") or []
    manifest = (row.get("manifest_json") or {}).get("slides") or {}
    paths = [Path(s["path"]) for s in slides]
    actual = {Path(s["path"]).name: sha256_file(Path(s["path"])) or "" for s in slides}
    gate = gates.sg6_manifest(manifest, actual)
    if gate.passed and len(paths) != SLIDE_COUNT:
        gate = GateResult(gate="SG-6", passed=False,
                          reason=f"expected {SLIDE_COUNT} slides, got {len(paths)}")
    return gate, paths


def _copy_problems(script: SideScript) -> list[str]:
    problems: list[str] = []
    payload = script.model_dump()
    sg5 = gates.sg5_copy(script.caption_fb, gates.copy_texts(payload))
    if not sg5.passed:
        problems.append(f"SG-5: {sg5.reason}")
    codes = find_internal_codes(script.caption_fb)
    if codes:
        problems.append(f"caption shows internal codes {codes}")
    if not script.caption_fb.strip() or len(script.caption_fb) > CAPTION_MAX:
        problems.append(f"caption length {len(script.caption_fb)} not in 1..{CAPTION_MAX}")
    return problems


def _restore_hint(row: dict[str, Any]) -> str:
    run = (row.get("manifest_json") or {}).get("run_id")
    return ("restore the assembly artifact with \"Resume only: previous run id or run URL\""
            + (f" = {run}" if run else ""))


def run_publish(side_day: date, deps: PublishDeps, *, retry_hold: bool = False) -> StageResult:
    sid = side_episode_id(side_day)
    row = deps.store.get_episode(sid)
    if not row:   # no side episode for this slot (no anchor / non-slot day): nothing to post
        return StageResult("publish", sid, "skipped",
                           detail={"reason": "no side episode for this slot"})
    status = row.get("status", "")
    result = StageResult("publish", sid, status, detail={"dry_run": not deps.live})

    existing = deps.store.live_publication(sid, CHANNEL)
    if existing:
        if status != "published":   # post + row exist, status update was lost
            deps.store.update_episode(sid, {"status": "published", "publish_hold": None,
                                            "error_message": None}, expect_status=status)
            result.status = "published"
        result.detail.update(reason="already published", post_id=existing.get("post_id"))
        return result

    if status == "hold":
        if not (row.get("error_message") or "").startswith("publish:"):
            if not retry_hold:   # production failed: p1 already alerted; nothing to post
                result.status = "skipped"
                result.detail.update(reason=f"production on hold: {row.get('error_message')}",
                                     still_on_hold=True)
                return result
            result.status = "error"
            result.detail["reason"] = ("episode is on hold for a production stage — use p1 with "
                                       "\"p1 / publish: release a held episode and resume from "
                                       "its last artifact\"")
            return result
        if not retry_hold:
            result.detail["reason"] = (f"episode on hold: {row.get('error_message')} "
                                       "(use --retry-hold)")
            result.detail["still_on_hold"] = True   # the hold itself was already notified
            return result
        if not deps.store.update_episode(sid, {"status": "assembled", "error_message": None},
                                         expect_status="hold"):
            result.status, result.detail["reason"] = "error", "hold release lost a race"
            return result
        deps.store.log("publish", "resume", {"sid": sid, "publish_hold": row.get("publish_hold")})
        status = "assembled"
    if status not in {"assembled", "publishing"}:
        result.status = "error"
        result.detail["reason"] = f"publish requires status 'assembled' (current {status!r})"
        return result
    if status == "publishing" and not deps.live:
        result.status = "error"
        result.detail["reason"] = "a live publish is in progress or was interrupted (status " \
                                  "'publishing'); re-run publish live to reconcile"
        return result

    if deps.wait_minutes > 0 and status == "assembled":
        waiting = _wait_reason(row, deps)
        if waiting is not None:
            result.detail.update(waiting)
            deps.store.log("publish", "waiting", {"sid": sid, **_loggable(result.detail)})
            return result

    # ── gates ───────────────────────────────────────────────────────────────
    echo = EchoPack.model_validate(row["echo_pack_json"])
    script = SideScript.model_validate(row["script_json"])
    before = deps.feed.main_fingerprint(echo.main_date)
    sg1 = gates.sg1_record(before)
    sg6, slides = _slides_gate(row)
    result.gates += [sg1, sg6]
    if not sg1.passed or not sg6.passed:
        result.status = "error"
        result.detail["reason"] = (sg1.reason if not sg1.passed
                                   else f"{sg6.reason} — {_restore_hint(row)}")
        deps.store.log("publish", "error", {"sid": sid, **_loggable(result.detail)})
        return result
    problems = _copy_problems(script)
    if problems:
        if not deps.live:   # a dry run reports; it never changes the episode status
            result.status, result.detail["reason"] = "error", "; ".join(problems)
            deps.store.log("publish", "error", {"sid": sid, **_loggable(result.detail)})
            return result
        return _hold(deps, sid, status, result, _Hold("; ".join(problems), SAFE))
    message = script.caption_fb
    result.detail.update(slides=len(slides), message_chars=len(message))

    # ── dry run ─────────────────────────────────────────────────────────────
    if not deps.live:
        if deps.publisher is not None:
            try:
                result.detail["page"] = deps.publisher.check()
            except PublishError as exc:
                result.status = "error"
                result.detail["reason"] = f"credential check failed: {exc}"
                deps.store.log("publish", "error", {"sid": sid, **_loggable(result.detail)})
                return result
        else:
            result.detail["page"] = None
            result.detail["note"] = "FACE_PAGE_ID / FACE_PAGE_TOKEN not provided: no check"
        deps.store.insert_publication({"side_episode_id": sid, "channel": CHANNEL,
                                       "post_id": None, "photo_ids": [], "dry_run": True})
        deps.store.log("publish", "dry_run", {"sid": sid, **_loggable(result.detail)})
        return result

    # ── live ────────────────────────────────────────────────────────────────
    if deps.publisher is None:
        result.status, result.detail["reason"] = "error", "publisher not configured"
        return result
    try:
        result.detail["page"] = deps.publisher.check()
    except PublishError as exc:
        result.status, result.detail["reason"] = "error", f"credential check failed: {exc}"
        deps.store.log("publish", "error", {"sid": sid, **_loggable(result.detail)})
        return result
    reconcile = status == "publishing" or str(row.get("publish_hold") or "").startswith(AMBIGUOUS)
    if status == "assembled" and not deps.store.update_episode(
            sid, {"status": "publishing"}, expect_status="assembled"):
        result.status = "error"
        result.detail["reason"] = "status changed concurrently (compare-and-set failed)"
        return result
    status = "publishing"
    try:
        post_id = None
        photo_ids: list[str] = []
        if reconcile:
            try:
                post_id = deps.publisher.find_recent_post(message)
            except PublishError as exc:
                raise _Hold(f"previous post outcome unknown and the Page could not be checked: "
                            f"{exc} — check the Page before retrying", AMBIGUOUS) from exc
            result.detail["reconciled"] = post_id is not None
        if post_id is None:
            for i, slide in enumerate(slides, start=1):
                try:
                    photo_ids.append(deps.publisher.upload_photo(slide))
                except PublishError as exc:
                    raise _Hold(f"upload failed at S{i} (no post created; safe to retry): {exc}",
                                SAFE, {"photo_ids": photo_ids}) from exc
            sg7 = gates.sg7_unchanged(before, deps.feed.main_fingerprint(echo.main_date))
            result.gates.append(sg7)
            if not sg7.passed:
                raise _Hold(f"{sg7.reason} (no post created)", SAFE, {"photo_ids": photo_ids})
            try:
                post_id = deps.publisher.create_post(message, photo_ids)
            except PublishError as exc:
                if exc.ambiguous:
                    raise _Hold(f"post outcome unknown: {exc} — the next run checks the Page "
                                "before posting", AMBIGUOUS, {"photo_ids": photo_ids}) from exc
                raise _Hold(f"post rejected (no post created; safe to retry): {exc}", SAFE,
                            {"photo_ids": photo_ids}) from exc
    except _Hold as hold:
        return _hold(deps, sid, status, result, hold)

    result.detail.update(post_id=post_id, photo_ids=photo_ids)
    try:
        deps.store.insert_publication({"side_episode_id": sid, "channel": CHANNEL,
                                       "post_id": post_id, "photo_ids": photo_ids,
                                       "dry_run": False})
    except Exception as exc:  # noqa: BLE001 — the post exists; keep the lock, report loudly
        result.status = "error"
        result.detail["reason"] = (f"POSTED as {post_id} but the publication row failed: {exc}. "
                                   "Status stays 'publishing'; re-run publish live to record it.")
        deps.store.log("publish", "error", {"sid": sid, **_loggable(result.detail)})
        return result
    if not deps.store.update_episode(sid, {"status": "published", "publish_hold": None,
                                           "error_message": None}, expect_status="publishing"):
        result.detail["warning"] = "publication recorded; status update lost a race"
    result.status = "published"
    deps.store.log("publish", "ok", {"sid": sid, **_loggable(result.detail)})
    return result


def _wait_reason(row: dict[str, Any], deps: PublishDeps) -> dict[str, Any] | None:
    """None = old enough to post; else the waiting detail."""
    raw = (row.get("manifest_json") or {}).get("assembled_at")
    try:
        assembled = datetime.fromisoformat(str(raw))
    except ValueError:
        return {"waiting": True, "reason": "assembly time not recorded — publish manually"}
    ready = assembled + timedelta(minutes=deps.wait_minutes)
    if (deps.now or utcnow)() >= ready:
        return None
    return {"waiting": True, "ready_at": ready.isoformat(),
            "reason": f"assembled {assembled.isoformat()}; waits {deps.wait_minutes} min"}


def _hold(deps: PublishDeps, sid: str, current: str, result: StageResult,
          hold: _Hold) -> StageResult:
    result.detail.update({"reason": str(hold), **hold.detail})
    deps.store.update_episode(sid, {"status": "hold", "error_message": f"publish: {hold}"[:2000],
                                    "publish_hold": f"{hold.kind}: {hold}"[:2000]},
                              expect_status=current)
    result.status = "hold"
    deps.store.log("publish", "hold", {"sid": sid, "kind": hold.kind, **_loggable(result.detail)})
    return result


def run_verify(side_day: date, deps: PublishDeps) -> StageResult:
    """Read-only: Page credential check, and the live post when one is recorded."""
    sid = side_episode_id(side_day)
    row = deps.store.get_episode(sid) or {}
    result = StageResult("verify", sid, row.get("status", ""))
    if deps.publisher is None:
        result.status, result.detail["reason"] = "error", "FACE_PAGE_ID / FACE_PAGE_TOKEN missing"
        return result
    try:
        result.detail["page"] = deps.publisher.check()
        pub = deps.store.live_publication(sid, CHANNEL) if row else None
        if pub and pub.get("post_id"):
            result.detail["post"] = deps.publisher.get_post(pub["post_id"])
    except PublishError as exc:
        result.status, result.detail["reason"] = "error", str(exc)
    deps.store.log("verify", "ok" if result.ok else "error",
                   {"sid": sid, **_loggable(result.detail)})
    return result

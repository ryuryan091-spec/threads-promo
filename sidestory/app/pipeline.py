"""Stage orchestration. P0: gate, echo (here). P1: narrative, image, assembly, p1 (app/p1.py)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sidestory.core import gates
from sidestory.core.dollar import infer_broad_from_history, select_dollar
from sidestory.core.echo import build_echo_pack
from sidestory.core.models import GateResult
from sidestory.core.schedule import ANCHOR_LOOKBACK_DAYS, is_publish_day, select_anchor
from sidestory.ports.main_feed import MainFeedReader
from sidestory.ports.market_source import DxySource
from sidestory.ports.store import SideStore

# History window for inferring the broad index run start (> weekly cadence, see core/dollar.py).
DOLLAR_HISTORY_DAYS = 21

STAGES = ("gate", "echo", "narrative", "image", "assembly", "inspect", "p1", "refgen", "publish",
          "verify", "locate")
P0_STAGES = ("gate", "echo")
P1_STAGES = ("narrative", "image", "assembly", "p1")


def side_episode_id(side_day: date, seq: int = 1) -> str:
    return f"SIDE-{side_day.isoformat()}-{seq:02d}"


@dataclass
class RunResult:
    side_episode_id: str
    gates: list[GateResult]
    echo: dict[str, Any] | None = None
    skipped: bool = False
    persisted: bool = False


def run_gate_and_echo(
    side_day: date,
    feed: MainFeedReader,
    store: SideStore | None,
    *,
    force: bool = False,
    persist: bool = True,
    dxy_source: DxySource | None = None,
) -> RunResult:
    """gate (SG-0, SG-1) → echo → SG-7 check → persist draft to icg_side.

    ``store=None`` or ``persist=False`` = read-only dry run (no writes anywhere).
    """
    sid = side_episode_id(side_day)
    result = RunResult(side_episode_id=sid, gates=[])

    # A slot is anchored once. Re-running the same slot must never re-select a
    # different (older) main episode and overwrite the existing draft.
    existing = store.get_episode(sid) if store is not None else None
    if existing:
        result.gates.append(GateResult(
            gate="SG-0", passed=False, skip=True,
            reason=f"slot exists: anchor={existing.get('anchor_main_episode')} "
                   f"status={existing.get('status')}"))
        result.skipped = True
        return result

    start = (side_day - timedelta(days=ANCHOR_LOOKBACK_DAYS)).isoformat()
    published = feed.published_episodes(start, side_day.isoformat())
    anchored = store.anchored_main_ids() if store is not None else set()
    anchor = select_anchor(side_day, published, anchored)

    sg0 = gates.sg0_anchor(anchor, is_publish_day(side_day), force)
    result.gates.append(sg0)
    if not sg0.passed:
        result.skipped = sg0.skip
        return result

    before = feed.main_fingerprint(anchor.episode_date)
    sg1 = gates.sg1_record(before)
    result.gates.append(sg1)
    if not sg1.passed:
        return result

    main_day = date.fromisoformat(anchor.episode_date)
    broad = infer_broad_from_history(feed.dollar_history(
        (main_day - timedelta(days=DOLLAR_HISTORY_DAYS)).isoformat(), anchor.episode_date))
    # Only completed sessions before the main KST date (main runs 01:36 KST = US D-1 midday).
    dxy = (dxy_source.dxy_close(main_day - timedelta(days=1))
           if dxy_source is not None else None)
    dollar = select_dollar(main_day, dxy, broad)
    echo = build_echo_pack(side_day.isoformat(), anchor, feed.market(anchor.episode_date),
                           feed.arc(), dollar)
    result.echo = echo.model_dump(mode="json")

    if store is not None and persist:
        store.upsert_episode(
            sid,
            {
                "episode_date": side_day.isoformat(),
                "anchor_main_episode": echo.main_episode_id,
                "outcome_class": echo.outcome_class.value,
                "echo_pack_json": result.echo,
                "status": "draft",
            },
        )
        result.persisted = True

    after = feed.main_fingerprint(anchor.episode_date)
    sg7 = gates.sg7_unchanged(before, after)
    result.gates.append(sg7)
    if store is not None and persist:
        store.log("echo", "ok" if sg7.passed else "hold", {"sid": sid, "sg7": sg7.reason})
    return result

# sidestory — New Network side canon (Facebook)

Isolated side track inside the ICG repo. Phase-1 = L-A only (main → side echo).
Main X pipeline is not modified; see boundary rules in `sidestory/__init__.py`.

## Layout
| Path | Role |
|---|---|
| `core/` | pure domain: models, outcome classes, Tue/Thu schedule, echo pack, canon rules, gates |
| `ports/` | Protocol interfaces |
| `adapters/supabase/` | icg_side access + main feed views (no engine imports) |
| `adapters/icg/` | ONLY place allowed to import `engine` (whitelisted symbols): Claude LLM, Gemini panel + ledger, PIL composer |
| `adapters/notion/` | side system prompt loader (`NOTION_SIDE_SYSTEM_ID`) |
| `adapters/facebook/` | Graph API Page publisher (P2) |
| `app/` | settings + stage orchestration |
| `migrations/` | `0000_precheck.sql` (read-only), `0001_icg_side_schema.sql` |
| `tests/` | side tests incl. boundary / split-readiness / isolation checks; `db_contract.py` = real Postgres + PostgREST E2E |

## Cadence
Tue/Thu 10:17 KST (`sidestory_run.yml`, inert until repo variable
`SIDESTORY_SCHEDULE_ENABLED=true`). Anchor = same-day published main episode,
else latest unanchored main episode after the previous slot (≤3 days). Main
outcome → side reaction class: VICTORY / DRAW / DEFEAT / NO_BATTLE.

## Dollar index redundancy (F3)
Main `dollar_index` is the Fed broad index (FRED DTWEXBGS, ~120, weekly runs carried
forward), not ICE DXY (~100). `core/dollar.py` picks the quotable value:
DXY completed close dated before the main KST date (yfinance `DX-Y.NYB`, ≤4 days old) → else broad index labelled 광의 달러지수
(run start ≤9 days old) → else omitted. A "DXY" within 3% of the broad value is rejected
as a series mix-up; opposite 1-week directions are flagged `direction_divergence`.
The raw `dollar_index` is never placed in `EchoPack.market`; SG-4 only accepts the chosen value.
`SIDESTORY_DXY_SOURCE=off` disables the second source (tests / offline).

## P0 runbook
1. Supabase SQL editor: run `migrations/0000_precheck.sql` → all `present = true`.
2. Run `migrations/0001_icg_side_schema.sql`.
3. Supabase dashboard → API → exposed schemas: add `icg_side` (if not listed).
4. Actions → "Sidestory Run" → stage `gate` (read-only) → then `echo` with persist=false.
5. Confirm output `gates` all passed (SG-0 skip is normal on non-slot days; use force).

## P1 (script → images → slides, no publishing)
Stages: `narrative` (draft→narrative_done), `image` (→image_done), `assembly` (→assembled),
`p1` (echo if the slot is new, then all remaining stages). Status is compare-and-set;
any gate failure → `hold` + `error_message`; `p1 --retry-hold` resumes from the last stored
artifact.
- narrative: Claude writes panels 1–6 + copy; panel 7 (data card, EchoPack numbers only) and
  8 (disclaimer) are deterministic. Schema + beats + SG-3/4/5 failures are fed back, max 3
  calls, then hold. Main characters are never drawn (D-P1-1).
- image: SG-2 (REF sha256 == `characters_side.yaml`; `__PENDING__` holds), then Gemini via
  the main `generate_panel` and the icg_side ledger (scope `output/sidestory/<date>/panels`).
- assembly: main `compose_episode(strict=True)` → 8 slides 1080×1350, SG-6 manifest, SG-7.
- Artifacts: Actions artifact `sidestory-<date>-<run_id>`; resume image/assembly in a new run
  with `artifact_run_id` (a paid panel whose file is gone is a ledger HOLD, never re-bought).

## P2 (Facebook publish)
Stages: `publish` (dry run unless live), `verify` (read-only). Restore the assembly artifact with
"Resume only: previous run id or run URL" (= `manifest_json.run_id`).
- assembly replaces S8 with the side disclaimer slide (`app/disclaimer_slide.py`, font-safe text).
- publish gates: SG-1, SG-6 (8 slides on disk == manifest), SG-5 + internal codes + caption length.
- dry run: Page credential check (`GET /{page-id}`), `side_publications` row with dry_run=true,
  status unchanged.
- live (double lock: input "publish only: post to Facebook for real" AND repository variable
  `SIDESTORY_PUBLISH_LIVE=true` → `DRY_RUN=false --live`): assembled → publishing → 8 unpublished
  photos → one feed post (`attached_media` in slide order) → live row → published.
- failures: upload / rejected post → hold `SAFE` (no visible post, retry is safe); unknown post
  outcome (timeout after sending, 5xx) → hold `AMBIGUOUS` (next run searches the Page feed for the
  same message and records it instead of posting again). Release a publish hold with
  "p1 / publish: release a held episode and resume from its last artifact".
- one live row per episode+channel (unique index) — a published episode is never posted twice.

## P4 (Tue/Thu automation)
Schedules (KST): 10:17 `p1`; 11:17 / 12:17 / 13:17 `publish --wait-minutes 60` (posts once the
assembly is ≥ 60 min old; later attempts are no-ops; 13:17 is the last attempt). The scheduled
publish restores the assembly artifact from `manifest_json.run_id` (`--stage locate`).
Turn on: repository variables `SIDESTORY_SCHEDULE_ENABLED=true` (runs) and
`SIDESTORY_PUBLISH_LIVE=true` (real posts; otherwise publish only dry-runs and says so).
Notices (`app/notify.py`, internal Telegram channel `TELEGRAM_BOT_TOKEN` +
`TELEGRAM_PAID_CHANNEL_ID`, notify step only): ✅ produced / ✅ posted (link) / ⏳ waiting /
ℹ️ skipped / 🚨 hold·error·crash. Repeat publish attempts stay quiet when nothing changed; the
last attempt always reports. A publish hold is never released automatically.

## Secrets used
`SUPABASE_URL`, `SUPABASE_KEY` (P0); `ANTHROPIC_API_KEY`, `GEMINI_API_SUB_PAY_KEY`,
`NOTION_API_KEY`, `NOTION_SIDE_SYSTEM_ID` (P1, run step only);
`FACE_PAGE_ID`, `FACE_PAGE_TOKEN` (P2+, publish/verify step only); `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_PAID_CHANNEL_ID` (P4, notify step only). X secrets and the free Telegram channel are never injected.

## Split procedure (after stabilisation)
`git subtree split --prefix=sidestory -b sidestory-split` → new repo; vendor the
whitelisted engine symbols listed in `adapters/icg/__init__.py`; move
`.github/workflows/sidestory_*.yml`; delete `test_drift_against_main_battle_calc`
and DR-1/diff_guard (ICG-coupled checks). The DB (icg_side + views) is unchanged.

"""icg_side DB + PostgREST end-to-end contract (local only).

Run: DATABASE_TEST_URL=postgresql://postgres@127.0.0.1:55432/postgres \
     POSTGREST_BIN=/tmp/postgrest python -m sidestory.tests.db_contract

Mirrors scripts/test_publication_postgrest_contract.py conventions: refuses any
non-local URL, builds a mock main ``icg`` schema, applies the side migration,
then drives the real CLI and the real engine image ledger over HTTP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

REPO = Path(__file__).resolve().parents[2]
SIDE = REPO / "sidestory"
LOCAL = {"localhost", "127.0.0.1"}
SECRET = "local-side-contract-secret-at-least-32-chars"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def jwt(role: str) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"role": role, "exp": int(time.time()) + 3600}).encode())
    sig = hmac.new(SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


# ── mock main schema (production-shaped) ─────────────────────────────────────
def bootstrap_main(db) -> None:
    db.execute("drop schema if exists icg_side cascade")
    db.execute("drop schema if exists icg cascade")
    db.execute("create schema icg")
    for role in ("anon", "authenticated", "service_role"):
        db.execute(f"do $$ begin create role {role} nologin; exception when duplicate_object"
                   f" then null; end $$")
    db.execute("do $$ begin create role authenticator login noinherit password 'local';"
               " exception when duplicate_object then null; end $$")
    db.execute("grant anon, authenticated, service_role to authenticator")
    # Supabase's service_role has BYPASSRLS; main icg.image_generation_calls relies on it too.
    db.execute("alter role service_role bypassrls")
    ints = ("arc_day arc_tension edt_pressure hero_momentum crowd_momentum villain_streak "
            "hero_win_streak season_arc_days defeated_villains villain_signature "
            "emergence_deficit_days dimensional_rift_progress").split()
    bools = "form2_available form3_activated volatility_fields_active zero_block_just_appeared"
    cols = [f"{k} integer" for k in ints] + [f"{k} boolean" for k in bools.split()]
    cols += [f"{k} text" for k in "open_hook last_outcome active_villain last_episode_type".split()]
    cols += ["last_episode_date date", "pair_tension jsonb", "updated_at timestamptz"]
    db.execute("create table icg.arc_state(id integer primary key," + ",".join(cols) + ")")
    db.execute(
        "create table icg.episode_assets(id serial primary key, episode_date date,"
        " episode_no integer, event_type text, status text, scenario_type text,"
        " heroes_json jsonb, battle_json jsonb, script_json jsonb, error_message text,"
        " total_runtime_sec numeric, updated_at timestamptz not null default now(),"
        " unique(episode_date, episode_no))")
    db.execute(
        "create table icg.daily_snapshots(snapshot_date date primary key, us10y numeric,"
        " vix numeric, oil_wti numeric, spy_change numeric, nasdaq_change numeric,"
        " dollar_index numeric, hy_spread numeric, fear_greed numeric, news_items jsonb)")
    db.execute("create table icg.daily_analysis(analysis_date date primary key,"
               " story_state_json jsonb)")
    db.execute("create table icg.published_comics(publish_date date, comic_type text,"
               " episode_no integer, risk_level text, tweet_id text, cut_count integer,"
               " cost_usd numeric, status text, created_at timestamptz default now())")
    db.execute((REPO / "docs/sql/narrative-publication-state.sql").read_text())
    db.execute((REPO / "migrations/20261001132931_image_generation_guard.sql").read_text())
    db.execute("grant usage on schema icg to service_role")
    db.execute("grant select, insert, update on all tables in schema icg to service_role")
    seed_main(db)


def seed_main(db) -> None:
    db.execute("insert into icg.arc_state(id, arc_day, arc_tension, hero_momentum,"
               " active_villain, last_outcome, last_episode_date, pair_tension)"
               " values (1, 16, 87, 100, 'CHAR_VILLAIN_001', 'HERO_VICTORY', '2026-10-06',"
               " '{\"PAIR_A\": 10}')")
    script = {
        "title": "첨탑 아래의 방패", "logline": "방패가 버텼다",
        "panels": [{"idx": 7, "panel_type": "AFTERMATH", "narration": "첨탑은 서 있다"},
                   {"idx": 8, "panel_type": "DISCLAIMER", "narration": "면책"}],
        "_continuity": {"next_hook": "30년물이 문을 두드린다",
                        "unresolved_threads": ["첨탑의 균열"]},
        "_state_candidate": {"secret": "must-not-leak"},
        "caption_x_cover": "main only",
    }
    rows = [
        ("2026-10-05", 1, "BATTLE", "published", "ONE_VS_ONE", "DRAW"),
        ("2026-10-06", 1, "BATTLE", "published", "ONE_VS_ONE", "HERO_VICTORY"),
        ("2026-10-07", 1, "INTEL", "assembled", "NO_BATTLE", "OBSERVATION"),
        ("2026-10-08", 1, "INTEL", "published", "NO_BATTLE", "OBSERVATION"),
    ]
    for day, no, event, status, scenario, outcome in rows:
        db.execute(
            "insert into icg.episode_assets(episode_date, episode_no, event_type, status,"
            " scenario_type, heroes_json, battle_json, script_json) values"
            " (%s,%s,%s,%s,%s,%s,%s,%s)",
            (day, no, event, status, scenario, Jsonb(["CHAR_HERO_002"]),
             Jsonb({"outcome": outcome, "villain_id": "CHAR_VILLAIN_001"}), Jsonb(script)))
    for day, ten in (("2026-10-05", 5.29), ("2026-10-06", 5.24), ("2026-10-08", 5.20)):
        db.execute("insert into icg.daily_snapshots(snapshot_date, us10y, vix, oil_wti,"
                   " spy_change, nasdaq_change, dollar_index, hy_spread, fear_greed)"
                   " values (%s,%s,15.3,91.26,0.73,-0.42,101.9,3.24,55)", (day, ten))
    db.execute("insert into icg.daily_analysis values ('2026-10-06', '{\"x\":1}')")
    db.execute("insert into icg.published_comics(publish_date, comic_type, episode_no, status)"
               " values ('2026-10-06','BATTLE',1,'published')")


def main_ddl(url: str) -> str:
    out = subprocess.run(["pg_dump", "--schema-only", "--schema=icg", "--no-owner", url],
                         check=True, capture_output=True, text=True).stdout
    # Recent pg_dump releases emit a random \\restrict token per run; it is not schema content.
    return "\n".join(line for line in out.splitlines()
                     if not line.startswith(("--", "\\restrict", "\\unrestrict")))


def main_data_hash(db) -> str:
    parts = []
    for table in ("arc_state", "episode_assets", "daily_snapshots", "daily_analysis",
                  "published_comics", "image_generation_calls"):
        rows = db.execute(f"select coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)::text),"
                          f" '[]') from icg.{table} t").fetchone()[0]
        parts.append(json.dumps(rows, sort_keys=True, default=str))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ── DB-level checks ──────────────────────────────────────────────────────────
def run_db_checks(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as db:
        bootstrap_main(db)
        rows = db.execute((SIDE / "migrations/0000_precheck.sql").read_text()).fetchall()
        check("D1 precheck: every contract column present", all(r[2] for r in rows),
              str([r[:2] for r in rows if not r[2]]))

        # Production state found 2026-10-05: an empty icg_side left by a manual run.
        db.execute("create schema icg_side")
        ddl_before, data_before = main_ddl(url), main_data_hash(db)
        db.execute((SIDE / "migrations/0001_icg_side_schema.sql").read_text())
        ddl_after = main_ddl(url)
        if ddl_after != ddl_before:
            import difflib
            print("\n".join(difflib.unified_diff(ddl_before.splitlines(), ddl_after.splitlines(),
                                                  lineterm="", n=1)))
        check("D2 migration leaves main icg DDL byte-identical", ddl_after == ddl_before)
        check("D3 migration leaves main icg data identical", main_data_hash(db) == data_before)

        objects = ("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace"
                   " where n.nspname='icg_side'")
        n_before = db.execute(objects).fetchone()[0]
        try:
            db.execute((SIDE / "migrations/0001_icg_side_schema.sql").read_text())
            check("D4 re-apply refused", False, "second apply succeeded")
        except psycopg.Error:
            db.execute("rollback")  # explicit BEGIN in the file leaves the block aborted
            n_after = db.execute(objects).fetchone()[0]
            check("D4 re-apply refused atomically (no partial objects)", n_before == n_after,
                  f"{n_before}->{n_after}")

        ddl_before = main_ddl(url)
        for _ in range(2):
            db.execute((SIDE / "migrations/0002_ledger_refs_scope.sql").read_text())
        check("D4b 0002 applies idempotently, main icg DDL unchanged",
              main_ddl(url) == ddl_before)
        for _ in range(2):
            db.execute((SIDE / "migrations/0003_ledger_retake_scope.sql").read_text())
        check("D4c 0003 applies idempotently, main icg DDL unchanged",
              main_ddl(url) == ddl_before)

        eps = db.execute("select episode_date, script_json from icg_side.main_feed_episode_v1"
                         " order by episode_date").fetchall()
        check("D5 episode view = published only", [r[0] for r in eps]
              == ["2026-10-05", "2026-10-06", "2026-10-08"], str([r[0] for r in eps]))
        keys = set().union(*(set(r[1].keys()) for r in eps))
        check("D6 episode view projects only contract keys", keys <= {
            "title", "logline", "panels", "next_hook", "unresolved_threads", "_continuity"},
            str(keys))
        check("D7 internal main keys not exposed (_state_candidate, captions)",
              not ({"_state_candidate", "caption_x_cover"} & keys))
        m = db.execute("select snapshot_date, us10y from icg_side.main_feed_market_v1"
                       " where snapshot_date <= '2026-10-07' order by snapshot_date desc"
                       " limit 1").fetchone()
        check("D8 market view latest-on-or-before", m[0] == "2026-10-06" and float(m[1]) == 5.24,
              str(m))
        a = db.execute("select arc_tension, last_episode_date from icg_side.main_feed_arc_v1"
                       ).fetchall()
        check("D9 arc view single row", a == [(87, "2026-10-06")], str(a))

        fp = lambda: db.execute(  # noqa: E731
            "select icg_side.main_state_fingerprint('2026-10-06')").fetchone()[0]
        f1, f2 = fp(), fp()
        check("D10 fingerprint deterministic sha256", f1 == f2 and len(f1) == 64)
        db.execute("insert into icg_side.side_episodes(side_episode_id, episode_date,"
                   " anchor_main_episode, outcome_class, echo_pack_json) values"
                   " ('SIDE-2026-10-06-01','2026-10-06','ICG-2026-10-06-001','VICTORY','{}')")
        check("D11 side writes do not move main fingerprint", fp() == f1)
        db.execute("update icg.arc_state set arc_tension = 88 where id = 1")
        f3 = fp()
        check("D12 main change moves fingerprint", f3 != f1)
        db.execute("update icg.arc_state set arc_tension = 87 where id = 1")
        db.execute("update icg.episode_assets set error_message='x'"
                   " where episode_date='2026-10-06'")
        check("D13 fingerprint covers episode_assets of the date", fp() != f1)
        db.execute("update icg.episode_assets set error_message=null"
                   " where episode_date='2026-10-06'")
        # updated_at moved — fingerprint differs from f1 by design (any write is detected)

        bad = {
            "D14 bad side id": "('SIDE-2026-10-06-1','2026-10-06','ICG-2026-10-05-001',"
                               "'DRAW','{}')",
            "D15 bad anchor id": "('SIDE-2026-10-08-01','2026-10-08','ICG-2026-10-08-1',"
                                 "'NO_BATTLE','{}')",
            "D16 duplicate anchor": "('SIDE-2026-10-08-01','2026-10-08','ICG-2026-10-06-001',"
                                    "'VICTORY','{}')",
            "D17 bad outcome class": "('SIDE-2026-10-08-01','2026-10-08','ICG-2026-10-08-001',"
                                     "'WIN','{}')",
        }
        for name, values in bad.items():
            try:
                db.execute("insert into icg_side.side_episodes(side_episode_id, episode_date,"
                           " anchor_main_episode, outcome_class, echo_pack_json) values "
                           + values)
                check(name + " rejected", False, "accepted")
            except psycopg.Error:
                check(name + " rejected", True)

        ins = ("insert into icg_side.side_publications(side_episode_id, channel, dry_run)"
               " values ('SIDE-2026-10-06-01','facebook',%s)")
        db.execute(ins, (True,))
        db.execute(ins, (True,))
        db.execute(ins, (False,))
        try:
            db.execute(ins, (False,))
            check("D18 second live publication rejected", False)
        except psycopg.Error:
            check("D18 second live publication rejected (dry runs repeatable)", True)
        try:
            db.execute("insert into icg_side.side_publications(side_episode_id, channel,"
                       " dry_run) values ('SIDE-2026-10-06-01','x',false)")
            check("D19 non-facebook channel rejected", False)
        except psycopg.Error:
            check("D19 non-facebook channel rejected", True)

        for role, expect in (("anon", False), ("authenticated", False), ("service_role", True)):
            ok_view = _can(db, role, "select 1 from icg_side.main_feed_episode_v1 limit 1")
            ok_fn = _can(db, role, "select icg_side.main_state_fingerprint('2026-10-06')")
            ok_tbl = _can(db, role, "select 1 from icg_side.side_episodes limit 1")
            check(f"D20 privileges {role}", (ok_view, ok_fn, ok_tbl) == (expect,) * 3,
                  f"view={ok_view} fn={ok_fn} table={ok_tbl}")
        no_main_write = not _can(db, "service_role",
                                 "delete from icg_side.main_feed_arc_v1")
        check("D21 contract views not writable", no_main_write)


def _can(db, role: str, sql: str) -> bool:
    try:
        with db.transaction():
            db.execute(f"set local role {role}")
            db.execute(sql)
            raise _Rollback
    except _Rollback:
        return True
    except psycopg.Error:
        return False


class _Rollback(Exception):
    pass


def run_ledger_db_checks(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as db:
        main_before = db.execute("select count(*) from icg.image_generation_calls").fetchone()[0]
        fp = "f" * 64
        r = db.execute("select icg_side.image_generation_reserve("
                       "'output/episodes/2026-10-06/panels',1,%s)", (fp,)).fetchone()[0]
        check("L1 main scope refused by side ledger", r.get("hold") == "invalid identity", str(r))
        scope = "output/sidestory/2026-10-20/panels"
        tokens = []
        for _ in range(3):
            res = db.execute("select icg_side.image_generation_reserve(%s,1,%s)",
                             (scope, fp)).fetchone()[0]
            if "token" in res:
                tokens.append(res["token"])
                db.execute("select icg_side.image_generation_finish(%s,1,%s,%s,'failed',0.1,null)",
                           (scope, fp, res["token"]))
        res4 = db.execute("select icg_side.image_generation_reserve(%s,1,%s)",
                          (scope, fp)).fetchone()[0]
        check("L2 per-panel attempt cap = 3", len(tokens) == 3 and "hold" in res4, str(res4))
        db.execute("delete from icg_side.image_generation_calls")
        holds = None
        for i in range(25):
            res = db.execute("select icg_side.image_generation_reserve(%s,%s,%s)",
                             (f"output/sidestory/2026-11-{(i % 20) + 1:02d}/panels", 1 + i // 20,
                              fp)).fetchone()[0]
            if "token" in res:
                db.execute("select icg_side.image_generation_finish(%s,%s,%s,%s,'success',0.05,%s)",
                           (f"output/sidestory/2026-11-{(i % 20) + 1:02d}/panels", 1 + i // 20,
                            fp, res["token"], "a" * 64))
            elif holds is None:
                holds = (i, res)
        check("L3 side daily cap (20 calls) enforced", holds is not None and holds[0] == 20,
              str(holds))
        db.execute("delete from icg_side.image_generation_calls")
        ok = db.execute("select icg_side.image_generation_reserve("
                        "'output/sidestory/refs/r1/panels',1,%s)", (fp,)).fetchone()[0]
        check("L5 refgen scope refs/r<N> accepted (0002)", "token" in ok, str(ok))
        bad = [db.execute("select icg_side.image_generation_reserve(%s,1,%s)", (sc, fp)
                          ).fetchone()[0].get("hold") for sc in (
            "output/sidestory/refs/x/panels", "output/sidestory/refs/r1/../panels",
            "output/sidestory/refs/r1", "output/sidestory/refs/r1000/panels")]
        check("L6 malformed refs scopes refused", bad == ["invalid identity"] * 4, str(bad))
        ok = db.execute("select icg_side.image_generation_reserve("
                        "'output/sidestory/2026-10-06/v2/panels',1,%s)", (fp,)).fetchone()[0]
        check("L7 SG-8 retake scope <date>/v2 accepted (0003)", "token" in ok, str(ok))
        bad = [db.execute("select icg_side.image_generation_reserve(%s,1,%s)", (sc, fp)
                          ).fetchone()[0].get("hold") for sc in (
            "output/sidestory/2026-10-06/v3/panels", "output/sidestory/2026-10-06/v2",
            "output/sidestory/2026-10-06/v2/v2/panels", "output/sidestory/v2/panels",
            "output/sidestory/refs/r1/v2/panels")]
        check("L8 only one retake scope (v2) exists", bad == ["invalid identity"] * 5, str(bad))
        main_after = db.execute("select count(*) from icg.image_generation_calls").fetchone()[0]
        check("L4 side ledger never touches main ledger", main_before == main_after == 0)
        db.execute("delete from icg_side.image_generation_calls")


# ── PostgREST end-to-end ─────────────────────────────────────────────────────
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_postgrest(binary: str, db_url: str, port: int,
                    schemas: str = "icg,icg_side") -> subprocess.Popen:
    conf = Path(tempfile.mkdtemp()) / "pgrst.conf"
    parsed = urlparse(db_url)
    uri = f"postgresql://authenticator:local@{parsed.hostname}:{parsed.port}{parsed.path}"
    conf.write_text(f'db-uri = "{uri}"\ndb-schemas = "{schemas}"\ndb-anon-role = "anon"\n'
                    f'jwt-secret = "{SECRET}"\nserver-port = {port}\n')
    log = open("/tmp/sidestory_postgrest.log", "w")
    return subprocess.Popen([binary, str(conf)], stdout=log, stderr=subprocess.STDOUT)


def start_proxy(upstream_port: int) -> tuple[ThreadingHTTPServer, int]:
    """Emulates the Supabase gateway prefix /rest/v1 in front of PostgREST."""

    class Proxy(BaseHTTPRequestHandler):
        def _forward(self):
            path = self.path.removeprefix("/rest/v1")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(f"http://127.0.0.1:{upstream_port}{path}", data=body,
                                         method=self.command)
            for key, value in self.headers.items():
                if key.lower() not in {"host", "content-length", "accept-encoding"}:
                    req.add_header(key, value)
            try:
                with urllib.request.urlopen(req) as resp:
                    status, headers, payload = resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as err:
                status, headers, payload = err.code, err.headers, err.read()
            self.send_response(status)
            for key, value in headers.items():
                if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = do_DELETE = do_HEAD = _forward

        def log_message(self, *args):
            pass

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def cli(env: dict, *args: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, "-m", "sidestory", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {"stdout": proc.stdout, "stderr": proc.stderr[-800:]}


def _wait(port: int) -> None:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.25)


def _env(proxy_port: int) -> dict:
    return {**os.environ, "PYTHONPATH": str(REPO), "SUPABASE_SCHEMA": "icg_side",
            "SUPABASE_URL": f"http://127.0.0.1:{proxy_port}", "SUPABASE_KEY": jwt("service_role"),
            "DRY_RUN": "true", "SIDESTORY_DXY_SOURCE": "off"}  # deterministic: no network


def run_setup_error_cases(url: str, binary: str) -> None:
    """Reproduces the 2026-10-05 production gate failure (PGRST106) and the
    not-yet-migrated case; both must exit 3 with a runbook hint, not a trace."""
    port = free_port()
    server = start_postgrest(binary, url, port, schemas="icg")  # icg_side NOT exposed
    proxy, proxy_port = start_proxy(port)
    try:
        _wait(port)
        code, out = cli(_env(proxy_port), "--stage", "gate", "--date", "2026-10-06")
        check("S1 not-exposed schema → exit 3 + Exposed schemas hint",
              code == 3 and "PGRST106" in out.get("error", "")
              and "Exposed schemas" in out.get("error", ""), str(out)[:300])
    finally:
        proxy.shutdown()
        server.terminate()
        server.wait(timeout=10)
    with psycopg.connect(url, autocommit=True) as db:
        db.execute("create schema if not exists icg_side_empty")
    port = free_port()
    server = start_postgrest(binary, url, port, schemas="icg,icg_side_empty")
    proxy, proxy_port = start_proxy(port)
    try:
        _wait(port)
        env = {**_env(proxy_port)}
        # settings pin the schema name; emulate an exposed-but-empty icg_side via a probe client
        os.environ.update({"SUPABASE_URL": env["SUPABASE_URL"], "SUPABASE_KEY": env["SUPABASE_KEY"]})
        from supabase import create_client

        from sidestory.adapters.supabase import client as side_client_mod
        original = side_client_mod.SIDE_SCHEMA
        side_client_mod.SIDE_SCHEMA = "icg_side_empty"
        try:
            side_client_mod.preflight(create_client(env["SUPABASE_URL"], env["SUPABASE_KEY"]))
            check("S2 exposed-but-unmigrated → PGRST205 hint", False, "preflight passed")
        except side_client_mod.SideSetupError as exc:
            check("S2 exposed-but-unmigrated → migration hint", "0001_icg_side_schema.sql"
                  in str(exc), str(exc))
        finally:
            side_client_mod.SIDE_SCHEMA = original
    finally:
        proxy.shutdown()
        server.terminate()
        server.wait(timeout=10)
        with psycopg.connect(url, autocommit=True) as db:
            db.execute("drop schema if exists icg_side_empty")


def run_e2e(url: str, binary: str) -> None:
    with psycopg.connect(url, autocommit=True) as db:
        db.execute("delete from icg_side.side_publications")
        db.execute("delete from icg_side.side_episodes")
        main_before = main_data_hash(db)
    port = free_port()
    server = start_postgrest(binary, url, port)
    proxy, proxy_port = start_proxy(port)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.25)
        env = {**os.environ, "PYTHONPATH": str(REPO), "SUPABASE_SCHEMA": "icg_side",
               "SUPABASE_URL": f"http://127.0.0.1:{proxy_port}", "SUPABASE_KEY": jwt("service_role"),
               "DRY_RUN": "true", "SIDESTORY_DXY_SOURCE": "off"}

        code, out = cli(env, "--stage", "gate", "--date", "2026-10-06")
        check("E1 CLI gate on Tue anchors same-day main",
              code == 0 and out.get("gates", [{}])[0].get("reason") == "ICG-2026-10-06-001"
              and not out.get("persisted"), str(out)[:400])

        code, out = cli(env, "--stage", "echo", "--date", "2026-10-06", "--no-persist")
        echo = out.get("echo") or {}
        check("E2 CLI echo read-only builds pack from views",
              code == 0 and echo.get("title") == "첨탑 아래의 방패"
              and echo.get("outcome_class") == "VICTORY" and echo.get("market", {}).get("us10y")
              == 5.24 and not out.get("persisted"), str(out)[:400])
        dollar = echo.get("dollar") or {}
        check("E2b F3 dollar via view history: broad fallback, raw field not in market",
              dollar.get("kind") == "BROAD" and dollar.get("label_ko") == "광의 달러지수"
              and "dollar_index" not in echo.get("market", {})
              and dollar.get("rejected", {}).get("DXY") == "unavailable", str(dollar))
        with psycopg.connect(url, autocommit=True) as db:
            n = db.execute("select count(*) from icg_side.side_episodes").fetchone()[0]
        check("E3 --no-persist wrote nothing", n == 0, f"rows={n}")

        code, out = cli(env, "--stage", "echo", "--date", "2026-10-06")
        gates = {g["gate"]: g["passed"] for g in out.get("gates", [])}
        check("E4 CLI echo persists draft, SG-0/1/7 pass",
              code == 0 and out.get("persisted") and gates == {"SG-0": True, "SG-1": True,
                                                                 "SG-7": True}, str(out)[:400])
        code, out = cli(env, "--stage", "echo", "--date", "2026-10-06")
        check("E5 same anchor not echoed twice (SKIP)", code == 0 and out.get("skipped"),
              str(out)[:300])
        code, out = cli(env, "--stage", "echo", "--date", "2026-10-08")
        check("E6 Thu echoes NO_BATTLE main episode",
              code == 0 and (out.get("echo") or {}).get("outcome_class") == "NO_BATTLE",
              str(out)[:300])
        code, out = cli(env, "--stage", "gate", "--date", "2026-10-07")
        check("E7 Wednesday is SKIP", code == 0 and out.get("skipped"), str(out)[:300])
        bad_env = {**env, "SUPABASE_SCHEMA": "icg"}
        code, out = cli(bad_env, "--stage", "gate", "--date", "2026-10-06")
        check("E8 CLI refuses main schema", code != 0, str(out)[:200])

        # Real engine image ledger through icg_side (P1 path), no provider call.
        os.environ.update({k: env[k] for k in ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SCHEMA")})
        os.environ["ICG_GENERATION_REVISION"] = "1"
        sys.path.insert(0, str(REPO))
        from engine.image.generation_guard import GenerationHold, ProductionGenerationGuard

        out_dir = Path(tempfile.mkdtemp()) / "output/sidestory/2026-10-06/panels"
        out_dir.mkdir(parents=True)
        guard = ProductionGenerationGuard(scope=out_dir.as_posix(), panel=1, prompt="p", refs=[])
        reused = guard.reuse(out_dir / "P1.png")
        token = guard.reserve()
        png = b"\x89PNG-side-test"
        (out_dir / "P1.png").write_bytes(png)
        guard.finish(token, state="success", actual_cost=0.04,
                     output_hash=hashlib.sha256(png).hexdigest())
        check("E9 engine guard reserve/finish via icg_side",
              reused is False and guard.reuse(out_dir / "P1.png") is True)
        main_guard = ProductionGenerationGuard(
            scope="output/episodes/2026-10-06/panels", panel=1, prompt="p", refs=[])
        try:
            main_guard.reserve()
            check("E10 main scope refused when schema=icg_side", False)
        except GenerationHold as exc:
            check("E10 main scope refused when schema=icg_side", "invalid identity" in str(exc))

        run_p1_checks(url, env)

        with psycopg.connect(url, autocommit=True) as db:
            check("E11 main icg data unchanged after full E2E", main_data_hash(db) == main_before)
    finally:
        proxy.shutdown()
        server.terminate()
        server.wait(timeout=10)


# ── P1 over PostgREST: real store/feed/ledger, fake LLM, stubbed image provider ──
def run_p1_checks(url: str, env: dict) -> None:
    import contextlib
    import io
    from datetime import date
    from functools import partial

    from PIL import Image

    import engine.image.gemini_client as gemini
    from sidestory.adapters.icg.composer_adapter import KOREAN_FONT_CANDIDATES, PilSlideComposer
    from sidestory.adapters.icg.image_adapter import GeminiPanelGenerator
    from sidestory.adapters.supabase.client import side_client
    from sidestory.adapters.supabase.main_feed_reader import SupabaseMainFeedReader
    from sidestory.adapters.supabase.side_store import SupabaseSideStore
    from sidestory.app.disclaimer_slide import render as render_disclaimer
    from sidestory.app.p1 import P1Deps, run_p1, run_stage
    from sidestory.app.settings import load_settings
    from sidestory.tests.p1_fixtures import (
        CLEAN,
        FakeInspector,
        FakeLLM,
        FakePrompts,
        make_refs,
        raw_script,
    )

    client = side_client(load_settings())
    store, feed = SupabaseSideStore(client), SupabaseMainFeedReader(client)
    sid = "SIDE-2026-10-08-01"  # drafted by E6 (NO_BATTLE anchor)

    check("P1 CAS update refuses wrong expected status",
          store.update_episode(sid, {"error_message": "x"}, expect_status="assembled") is False
          and store.get_episode(sid)["status"] == "draft")
    try:
        store.update_episode(sid, {"status": "bogus"}, expect_status="draft")
        check("P2 status check constraint enforced over API", False)
    except Exception as exc:  # noqa: BLE001
        check("P2 status check constraint enforced over API", "23514" in str(exc), str(exc)[:200])

    buf = io.BytesIO()
    Image.new("RGB", (96, 120), (40, 20, 90)).save(buf, "PNG")
    provider_calls = []

    def fake_generate_one(client_, prompt, refs, aspect_ratio=None):
        provider_calls.append((prompt[:20], [Path(r).name for r in refs]))
        return buf.getvalue(), 1000, 1290

    real_one, real_client = gemini._generate_one, gemini._get_client
    gemini._generate_one, gemini._get_client = fake_generate_one, (lambda: object())
    cwd = os.getcwd()
    work = Path(tempfile.mkdtemp())
    os.chdir(work)
    try:
        raw = raw_script()
        raw["panels"][2]["narration"] = "붕괴 데이터가 흘러간다"
        deps = P1Deps(feed=feed, store=store, llm=FakeLLM([raw]), prompts=FakePrompts(),
                      images=GeminiPanelGenerator(), composer=PilSlideComposer(),
                      characters=make_refs(work), ref_root=work,
                      disclaimer=partial(render_disclaimer, font_path=next(
                          p for p in KOREAN_FONT_CANDIDATES if p.is_file())),
                      inspector=FakeInspector({(2, 1): dict(CLEAN, figures=[
                          {"kind": "other", "prominence": "major"}])}), run_id="777")
        with contextlib.redirect_stderr(io.StringIO()):
            results = run_p1(date(2026, 10, 8), deps)
        statuses = [(r.stage, r.status) for r in results]
        row = store.get_episode(sid)
        check("P3 run_p1 draft→assembled through PostgREST + real ledger",
              statuses == [("narrative", "narrative_done"), ("image", "image_done"),
                           ("assembly", "assembled")] and row["status"] == "assembled"
              and len(row["slides_json"]) == 8 and row["manifest_json"]["slides"],
              f"{statuses} {[r.detail for r in results][-1]}")
        with psycopg.connect(url, autocommit=True) as db:
            ledger = db.execute(
                "select count(*), count(*) filter (where state='success') from"
                " icg_side.image_generation_calls where scope ="
                " 'output/sidestory/2026-10-08/panels'").fetchone()
        with psycopg.connect(url, autocommit=True) as db:
            retake = db.execute(
                "select panel, state from icg_side.image_generation_calls where scope ="
                " 'output/sidestory/2026-10-08/v2/panels'").fetchall()
        check("P4 six paid panels reserved+settled in icg_side ledger",
              ledger == (6, 6) and len(provider_calls) == 7, f"{ledger} calls={provider_calls}")
        p2 = row["panels_json"]["panels"][1]
        check("P10 SG-8 critical P2 → one retake via real ledger scope <date>/v2, used in"
              " assembly", [tuple(r) for r in retake] == [(2, "success")]
              and "/v2/panels/P2.png" in p2["path"] and row["panels_json"]["run_id"] == "777",
              f"{retake} {p2.get('path')}")
        posed = [c for c in provider_calls if c[1]]
        check("P5 REF attached only for posed panels",
              all(len(c[1]) == 1 and c[1][0].startswith("zero_block_") for c in posed)
              and len(posed) == 3, str(provider_calls))

        # Re-run image on the same artifacts: ledger reuse, no provider call, no new rows.
        store.update_episode(sid, {"status": "narrative_done"}, expect_status="assembled")
        before = len(provider_calls)
        res = run_stage("image", date(2026, 10, 8), deps)
        with psycopg.connect(url, autocommit=True) as db:
            n = db.execute("select count(*) from icg_side.image_generation_calls where scope ="
                           " 'output/sidestory/2026-10-08/panels'").fetchone()[0]
        check("P6 image rerun reuses ledger artifacts incl. retake (0 provider calls)",
              res.status == "image_done" and len(provider_calls) == before and n == 6,
              f"{res.status} {res.detail}")

        # Lost artifact on a fresh runner → ledger HOLD, never a silent paid regeneration.
        store.update_episode(sid, {"status": "narrative_done"}, expect_status="image_done")
        (work / "output/sidestory/2026-10-08/panels/P1.png").unlink()
        res = run_stage("image", date(2026, 10, 8), deps)
        row = store.get_episode(sid)
        check("P7 missing paid artifact → hold (restore artifact), no new call",
              res.status == "hold" and row["status"] == "hold"
              and "Restore" in (row.get("error_message") or "")
              and len(provider_calls) == before, f"{res.detail}")
        with psycopg.connect(url, autocommit=True) as db:
            logs = db.execute("select stage, status from icg_side.side_run_logs where"
                              " detail->>'sid' = %s order by id", (sid,)).fetchall()
        check("P8 run logs recorded per stage",
              [tuple(x) for x in logs][-5:] == [("narrative", "ok"), ("image", "ok"),
                                                ("assembly", "ok"), ("image", "ok"),
                                                ("image", "hold")], str(logs))
        # refgen through the real ledger (scope refs/r1) with the stubbed provider.
        from sidestory.app.refgen import POSE_ORDER, run_refgen

        class _Prompts:
            def ref_prompts(self):
                return {p: f"REF prompt for {p} " * 40 for p in POSE_ORDER}

        calls_before = len(provider_calls)
        with contextlib.redirect_stderr(io.StringIO()):
            ref = run_refgen(1, images=GeminiPanelGenerator(), prompts=_Prompts(), store=store)
        with psycopg.connect(url, autocommit=True) as db:
            n = db.execute("select count(*) filter (where state='success') from"
                           " icg_side.image_generation_calls where scope ="
                           " 'output/sidestory/refs/r1/panels'").fetchone()[0]
        attached = [c[1] for c in provider_calls[calls_before:]]
        check("P9 refgen: 5 REFs via ledger, front first then attached to the other 4",
              ref.status == "ok" and n == 5 and attached[0] == []
              and all(a == ["zero_block_front.png"] for a in attached[1:])
              and set(ref.files) == set(POSE_ORDER), f"{ref.as_dict()} {attached}")

        # P2 publish over PostgREST (fake Facebook publisher, real side_publications table).
        from sidestory.app.publish import PublishDeps, run_publish
        from sidestory.tests.test_p2_publish import FakePublisher

        store.update_episode(sid, {"status": "assembled", "error_message": None},
                             expect_status="hold")
        pdeps = PublishDeps(feed=feed, store=store, publisher=FakePublisher(), live=False)
        dry = run_publish(date(2026, 10, 8), pdeps)
        with psycopg.connect(url, autocommit=True) as db:
            rows = db.execute("select dry_run, post_id from icg_side.side_publications"
                              " where side_episode_id=%s", (sid,)).fetchall()
        check("P11 publish dry run: credential check, dry row via API, status unchanged",
              dry.status == "assembled" and [tuple(r) for r in rows] == [(True, None)]
              and store.get_episode(sid)["status"] == "assembled", f"{dry.detail} {rows}")
        from datetime import datetime, timedelta

        assembled_at = datetime.fromisoformat(
            store.get_episode(sid)["manifest_json"]["assembled_at"])
        pub = FakePublisher()
        early = PublishDeps(feed=feed, store=store, publisher=pub, live=True, wait_minutes=60,
                            now=lambda: assembled_at + timedelta(minutes=59))
        wait = run_publish(date(2026, 10, 8), early)
        check("P14 scheduled publish waits 60 min (assembled_at round-trips via API)",
              wait.detail.get("waiting") is True and pub.uploads == []
              and store.get_episode(sid)["status"] == "assembled", str(wait.detail))
        pdeps.publisher, pdeps.live = pub, True
        live = run_publish(date(2026, 10, 8), pdeps)
        with psycopg.connect(url, autocommit=True) as db:
            rows = db.execute("select dry_run, post_id, jsonb_array_length(photo_ids) from"
                              " icg_side.side_publications where side_episode_id=%s"
                              " order by id", (sid,)).fetchall()
            st = db.execute("select status, publish_hold from icg_side.side_episodes"
                            " where side_episode_id=%s", (sid,)).fetchone()
        check("P12 live publish: assembled→publishing→published, live row with 8 photos",
              live.status == "published" and tuple(st) == ("published", None)
              and [tuple(r) for r in rows] == [(True, None, 0), (False, "123_999", 8)]
              and len(pub.posts) == 1, f"{live.detail} {rows} {st}")
        again = run_publish(date(2026, 10, 8), pdeps)
        try:
            store.insert_publication({"side_episode_id": sid, "channel": "facebook",
                                      "post_id": "dup", "photo_ids": [], "dry_run": False})
            dup_blocked = False
        except Exception as exc:  # noqa: BLE001
            dup_blocked = "23505" in str(exc) or "duplicate" in str(exc)
        check("P13 no second post: rerun is a no-op and the DB refuses a 2nd live row",
              again.detail.get("reason") == "already published" and len(pub.posts) == 1
              and dup_blocked, f"{again.detail} dup_blocked={dup_blocked}")
    finally:
        gemini._generate_one, gemini._get_client = real_one, real_client
        os.chdir(cwd)


def main() -> int:
    url = os.environ.get("DATABASE_TEST_URL", "")
    if urlparse(url).hostname not in LOCAL:
        print("refusing non-local DATABASE_TEST_URL")
        return 2
    binary = os.environ.get("POSTGREST_BIN", "/tmp/postgrest")
    run_db_checks(url)
    run_ledger_db_checks(url)
    if Path(binary).exists():
        run_setup_error_cases(url, binary)
        run_e2e(url, binary)
    else:
        check("E* PostgREST binary present", False, binary)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nSUMMARY: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

from chatvault.brain_db import BRAIN_MIGRATIONS_DIR
from chatvault.brain.distill import OLLAMA_GENERATE_URL
from chatvault.doctor import CheckResult

# `brain doctor` (docs/51_SPEC_brain_parity_and_naming.md §2.1 `doctor` row):
# "Brain has more moving parts than ChatVault here (two DBs, one optional)."
# Reuses chatvault.doctor.CheckResult so both `chatvault doctor` and
# `brain doctor` render through the exact same table shape in the CLI.

# `summarise conversation`/`summarise entry` (chatvault/brain/distill.py)
# POST to OLLAMA_GENERATE_URL ("http://localhost:11434/api/generate"); this
# check derives the host from that same constant rather than hardcoding a
# second copy, and hits Ollama's own lightweight /api/tags listing endpoint
# instead of /api/generate — a reachability probe should not itself burn a
# model invocation.
_OLLAMA_TAGS_URL = OLLAMA_GENERATE_URL.rsplit("/api/", 1)[0] + "/api/tags"
_TIMEOUT_S = 3


def check_ollama_reachable(base_url: str = _OLLAMA_TAGS_URL) -> CheckResult:
    """Lightweight ping — GET /api/tags, no generation. Lists locally
    available models on success (§8 Q5: 'doctor list locally available
    models so the user can choose informedly' for --model)."""
    req = urllib.request.Request(base_url)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return CheckResult(
            name="ollama reachability",
            ok=False,
            fatal=False,
            detail=f"not reachable at {base_url} ({exc.__class__.__name__}) — "
            "warn only; `summarise conversation`/`summarise entry`/`embed` need it, everything else still works",
        )
    models = [m.get("name", "?") for m in data.get("models", [])]
    detail = f"reachable at {base_url}"
    detail += f"; local models: {', '.join(models)}" if models else "; no models pulled yet"
    return CheckResult(name="ollama reachability", ok=True, fatal=False, detail=detail)


def check_db_integrity(conn: sqlite3.Connection) -> CheckResult:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    ok = result == "ok"
    return CheckResult(
        name="brain.sqlite integrity",
        ok=ok,
        fatal=ok is False,
        detail=result if ok else f"PRAGMA integrity_check reported: {result}",
    )


def check_migration_state(conn: sqlite3.Connection) -> CheckResult:
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    all_migrations = sorted(p.name for p in BRAIN_MIGRATIONS_DIR.glob("*.sql"))
    pending = [m for m in all_migrations if m not in applied]
    ok = not pending
    return CheckResult(
        name="brain migration state",
        ok=ok,
        fatal=False,
        detail="up to date" if ok else f"pending: {', '.join(pending)} — will auto-apply on next connect",
    )


def check_orphaned_fts(conn: sqlite3.Connection) -> CheckResult:
    """items_fts (migrations/brain — base SCHEMA_SQL in brain_db.py) is a
    plain, trigger-synced FTS5 table (see brain_db.py's own comment on why
    it isn't contentless) — a row here with no matching `items` row would
    mean the sync triggers missed a delete somewhere."""
    orphans = conn.execute(
        "SELECT COUNT(*) FROM items_fts LEFT JOIN items ON items.id = items_fts.rowid WHERE items.id IS NULL"
    ).fetchone()[0]
    ok = orphans == 0
    return CheckResult(
        name="orphaned FTS rows",
        ok=ok,
        fatal=False,
        detail="none" if ok else f"{orphans} row(s) in items_fts with no matching item — run `brain verify`",
    )


def check_item_links_edge_count(conn: sqlite3.Connection, threshold: int = 200) -> CheckResult:
    """docs/44_SPEC_BRAIN_options_and_neo4j.md's ~200-edge threshold for
    when a Neo4j graph projection starts paying for itself. `brain graph`
    itself is a later batch (docs/51_SPEC §2.1 `graph` row) — this just
    reports the number against the threshold now, every time doctor runs."""
    count = conn.execute("SELECT COUNT(*) FROM item_links").fetchone()[0]
    ok = count < threshold
    return CheckResult(
        name="item_links edge count",
        ok=True,
        fatal=False,
        detail=f"{count} edge(s) (graph threshold: {threshold})" if ok else f"{count} edge(s) — at/above the {threshold}-edge graph threshold; consider `brain graph rebuild` once it exists",
    )


def run_all(conn: sqlite3.Connection) -> list[CheckResult]:
    return [
        check_db_integrity(conn),
        check_migration_state(conn),
        check_orphaned_fts(conn),
        check_item_links_edge_count(conn),
        check_ollama_reachable(),
    ]

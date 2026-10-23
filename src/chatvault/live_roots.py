"""The live-root registry: what `chatvault ingest --live` refreshes.

A live source is either
  * direct     a directory that grows in place (`~/.claude/projects`): scan it, or
  * extracted  a SQLite store that ingest cannot read (Devin, Cursor,
               Antigravity): snapshot it with `extract_cmd` into the vault's
               inbox, ingest the fresh snapshot, let `intake` file it.

`source_files.kind='live'` answers "does this go stale?"; it is NOT the list of
places to read from. That list is declared here (docs/02_DECISIONS.md, "Derive
facts, declare intentions"). Extraction is read-only and WAL-safe: each script
snapshots through SQLite's backup API on a `mode=ro` connection and verifies
the source's mtime/size did not change underneath it.
"""

from __future__ import annotations

import glob
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from chatvault import paths
from chatvault.config import Config

PROJECT_DIR = Path(__file__).resolve().parents[2]

# (source, path, live_store, extract_cmd). Generic application locations only.
_L = "~/Library/Application Support"
DEFAULTS: list[dict] = [
    {"source": "claude_code", "path": "~/.claude/projects"},
    {"source": "claude_code", "path": f"{_L}/Claude/local-agent-mode-sessions"},
    {"source": "openclaw", "path": "~/.openclaw/agents"},
    {"source": "hermes", "path": "~/.hermes"},
    {"source": "devin_cli", "live_store": ["~/.local/share/devin/cli/sessions.db"],
     "extract_cmd": "scripts/extract_devin_cli.py"},
    {"source": "devin_desktop_acp", "live_store": [f"{_L}/Devin/User/acp-messages/*.db"],
     "extract_cmd": "scripts/extract_devin_desktop_acp.py"},
    {"source": "cursor",
     "live_store": [f"{_L}/Cursor/User/globalStorage/state.vscdb", f"{_L}/Cursor/User/workspaceStorage/*/state.vscdb"],
     "extract_cmd": "scripts/extract_cursor.py"},
    {"source": "antigravity",
     "live_store": ["~/.gemini/antigravity-ide/conversations", "~/.gemini/antigravity-cli/conversations"],
     "extract_cmd": "scripts/extract_antigravity.py"},
]
EXTRACTED_SOURCES = tuple(d["source"] for d in DEFAULTS if "extract_cmd" in d)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_defaults(conn: sqlite3.Connection) -> int:
    """Insert any default entry not yet registered. Idempotent; never edits or
    removes what is there. Returns the number of rows added."""
    added = 0
    for d in DEFAULTS:
        if d.get("extract_cmd"):
            exists = conn.execute("SELECT 1 FROM live_roots WHERE source = ? AND extract_cmd IS NOT NULL", (d["source"],)).fetchone()
        else:
            exists = conn.execute("SELECT 1 FROM live_roots WHERE path = ?", (d["path"],)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO live_roots (source, path, live_store, extract_cmd, stage_dir, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                d["source"], d.get("path"),
                json.dumps(d["live_store"]) if d.get("live_store") else None,
                d.get("extract_cmd"),
                f"raw/00_inbox/{d['source']}" if d.get("extract_cmd") else None,
                _now(),
            ),
        )
        added += 1
    conn.commit()
    return added


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM live_roots WHERE active = 1 ORDER BY source, path").fetchall()


def deactivate(conn: sqlite3.Connection, source: str, reason: str) -> int:
    cur = conn.execute(
        "UPDATE live_roots SET active = 0, note = ? WHERE source = ? AND active = 1", (reason, source)
    )
    conn.commit()
    return cur.rowcount


# ───────────────────────────── status ─────────────────────────────


def _expand(entry: str) -> list[Path]:
    return [Path(p) for p in sorted(glob.glob(str(Path(entry).expanduser())))]


def store_paths(row: sqlite3.Row) -> list[Path]:
    """Every file or directory currently matched by the row's live store (or
    its scanned `path`)."""
    entries = json.loads(row["live_store"]) if row["live_store"] else [row["path"]]
    return [m for e in entries for m in _expand(e)]


def _newest_mtime(p: Path) -> float | None:
    newest = None
    candidates = [p] if p.is_file() else (list(p.rglob("*")) if p.is_dir() else [])
    for f in candidates:
        # a SQLite store in WAL mode records new writes in <db>-wal first
        for g in (f, f.with_name(f.name + "-wal")):
            try:
                if g.is_file():
                    m = g.stat().st_mtime
                    newest = m if newest is None or m > newest else newest
            except OSError:
                continue
    return newest


def newest_mtime(row: sqlite3.Row) -> float | None:
    times = [t for t in (_newest_mtime(p) for p in store_paths(row)) if t is not None]
    return max(times) if times else None


@dataclass
class SourceStatus:
    source: str
    rows: list[sqlite3.Row]
    store_label: str
    extracted: bool
    exists: bool
    last_ingest: float | None  # epoch seconds
    data_age: float | None     # newest live-store mtime, epoch seconds
    state: str                 # ok | STALE | MISSING
    missing: list[str]


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _label(row: sqlite3.Row, matched: int) -> str:
    entries = json.loads(row["live_store"]) if row["live_store"] else [row["path"]]
    first = entries[0].replace(str(Path.home()), "~")
    if row["extract_cmd"] and ("*" in first or matched > 1):
        return f"{first}  ({matched})"
    return first


def statuses(conn: sqlite3.Connection) -> list[SourceStatus]:
    """One status per source (several registered trees collapse to one line)."""
    by_source: dict[str, list[sqlite3.Row]] = {}
    for r in active(conn):
        by_source.setdefault(r["source"], []).append(r)
    out = []
    for source, rows in by_source.items():
        extracted = any(r["extract_cmd"] for r in rows)
        matched = [p for r in rows for p in store_paths(r)]
        missing = []
        for r in rows:
            if not store_paths(r):
                entries = json.loads(r["live_store"]) if r["live_store"] else [r["path"]]
                missing.extend(entries)
        # for an extracted store, one surface of several (IDE vs CLI) may be absent
        # legitimately; the source is MISSING only when none of it exists.
        exists = bool(matched) if extracted else not missing
        mtimes = [t for t in (newest_mtime(r) for r in rows) if t is not None]
        data_age = max(mtimes) if mtimes else None
        if extracted:
            last = max((t for t in (_ts(r["last_extracted_at"]) for r in rows) if t), default=None)
            if last is None:
                last = _ts(conn.execute(
                    "SELECT MAX(first_seen_at) FROM source_files WHERE platform = ?", (source,)
                ).fetchone()[0])
        else:
            last = _ts(conn.execute(
                "SELECT MAX(last_seen_at) FROM source_files WHERE platform = ?", (source,)
            ).fetchone()[0])
        if not exists:
            state = "MISSING"
        elif extracted and (last is None or (data_age is not None and data_age > last)):
            state = "STALE"
        else:
            state = "ok"
        label = _label(rows[0], len(matched))
        if len(rows) > 1:
            label += f" (+{len(rows) - 1} more tree{'s' if len(rows) > 2 else ''})"
        out.append(SourceStatus(source, rows, label, extracted, exists, last, data_age, state, missing if not exists else []))
    return out


# ───────────────────────────── extraction ─────────────────────────────


def stage_path(config: Config, row: sqlite3.Row) -> Path:
    return config.root / row["stage_dir"]


def extract(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> tuple[bool, str]:
    """Snapshot the live store into the inbox. Returns (ok, message)."""
    script = PROJECT_DIR / row["extract_cmd"]
    if not script.is_file():
        return False, f"extract script not found: {script}"
    out_dir = stage_path(config, row)
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(script), "--out-dir", str(out_dir)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout).strip() else f"exit {proc.returncode}"
    conn.execute("UPDATE live_roots SET last_extracted_at = ? WHERE id = ?", (_now(), row["id"]))
    conn.commit()
    return True, proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "ok"


# ───────────────────────────── one-time normalisation ─────────────────────────────

_CASE_TRIGGERS = ("trg_source_files_root_case_insert", "trg_source_files_root_case_update")


def normalise(conn: sqlite3.Connection, *, apply: bool) -> dict:
    """Rewrite every source_files path/root_dir to its canonical spelling,
    mark staged snapshots of extraction-backed sources 'frozen', and seed the
    registry. Rows are rewritten, never deleted (D3). With apply=False only
    counts. Returns a report."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'live_roots'").fetchone():
        raise RuntimeError("migration 016 is not applied — run `chatvault init` first")
    rows = conn.execute("SELECT id, path, root_dir FROM source_files").fetchall()
    cache: dict[str, str] = {}

    def canon(p: str | None) -> str | None:
        if not p:
            return p
        if p not in cache:
            cache[p] = paths.canonical(p)
        return cache[p]

    changes = []
    roots_before = {r["root_dir"] for r in rows if r["root_dir"]}
    for r in rows:
        np_, nr = canon(r["path"]), canon(r["root_dir"])
        if np_ != r["path"] or nr != r["root_dir"]:
            changes.append((np_, nr, r["id"]))
    roots_after = {nr for _, nr, _ in changes if nr} | {
        canon(r["root_dir"]) for r in rows if r["root_dir"]
    }
    snapshots = conn.execute(
        f"SELECT COUNT(*) FROM source_files WHERE kind = 'live' AND platform IN ({','.join('?' * len(EXTRACTED_SOURCES))})",  # noqa: S608
        EXTRACTED_SOURCES,
    ).fetchone()[0]
    live_before = conn.execute("SELECT COUNT(DISTINCT root_dir) FROM source_files WHERE kind = 'live' AND presence != 'retired'").fetchone()[0]
    report = {
        "rows": len(rows),
        "rows_rewritten": len(changes),
        "roots_before": len(roots_before),
        "roots_after": len(roots_after),
        "snapshots_to_frozen": snapshots,
        "live_root_dirs_before": live_before,
        "registry_added": 0,
    }
    if not apply:
        return report

    try:
        conn.execute("BEGIN")  # DDL below must roll back with the rewrite
        # The case guards compare against rows not yet rewritten; lift them for
        # the batch and restore them in the same transaction.
        trigger_sql = {
            r["name"]: r["sql"]
            for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?)", _CASE_TRIGGERS)
        }
        for name in trigger_sql:
            conn.execute(f"DROP TRIGGER {name}")  # noqa: S608
        conn.executemany("UPDATE source_files SET path = ?, root_dir = ? WHERE id = ?", changes)
        for sql in trigger_sql.values():
            conn.execute(sql)
        conn.execute(
            f"UPDATE source_files SET kind = 'frozen' WHERE kind = 'live' AND platform IN ({','.join('?' * len(EXTRACTED_SOURCES))})",  # noqa: S608
            EXTRACTED_SOURCES,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    report["registry_added"] = seed_defaults(conn)
    report["live_root_dirs_after"] = conn.execute(
        "SELECT COUNT(DISTINCT root_dir) FROM source_files WHERE kind = 'live' AND presence != 'retired'"
    ).fetchone()[0]
    return report

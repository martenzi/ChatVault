from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, now

# `brain view` (docs/51_SPEC_brain_parity_and_naming.md §2.1 `view` row) —
# saved queries against `brain.sqlite`, mirroring ChatVault's own
# `view save`/`view run`/`view list` (chatvault/cli.py, backed by
# chatvault/curation.py's view_save/view_run/view_list) command shape, for
# consistency between the two tools. The `views` table itself already
# exists from migrations/brain/005_brain_parity.sql.


def save_view(conn: sqlite3.Connection, name: str, query: str, description: str | None = None) -> dict:
    if not name or not name.strip():
        raise BrainError("view name must not be empty")
    if not query or not query.strip():
        raise BrainError("--query must not be empty")
    existing = conn.execute("SELECT id FROM views WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        raise BrainError(f"a view named {name!r} already exists — use `brain view remove` first to replace it")
    conn.execute(
        "INSERT INTO views (name, query, description, created_at) VALUES (?, ?, ?, ?)",
        (name.strip(), query.strip(), description, now()),
    )
    conn.commit()
    return {"name": name.strip()}


def list_views(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM views ORDER BY name").fetchall()


def get_view(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM views WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise BrainError(f"no view named {name!r}")
    return row


def run_view(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Executes a saved view's SQL and returns the raw rows — same
    "whatever the query does, it does" contract as ChatVault's `view run`.
    No safety rails beyond SQLite's own read/write behaviour; a view is
    something the user wrote for themselves."""
    view = get_view(conn, name)
    try:
        return conn.execute(view["query"]).fetchall()
    except sqlite3.Error as exc:
        raise BrainError(f"view {name!r} failed to run: {exc}") from exc


def remove_view(conn: sqlite3.Connection, name: str) -> dict:
    view = get_view(conn, name)
    conn.execute("DELETE FROM views WHERE id = ?", (view["id"],))
    conn.commit()
    return {"name": name}

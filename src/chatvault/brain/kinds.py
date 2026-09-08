from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError


def add_kind(conn: sqlite3.Connection, name: str, description: str | None = None) -> dict:
    """§7 acceptance: 'brain add kind then using it in add entry works with
    no code change' — add_entry validates kind against `SELECT name FROM
    kinds`, so nothing else needs to know this ran."""
    if conn.execute("SELECT 1 FROM kinds WHERE name = ?", (name,)).fetchone():
        raise BrainError(f"kind {name!r} already exists")
    next_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 FROM kinds").fetchone()[0]
    conn.execute(
        "INSERT INTO kinds (name, description, sort_order, builtin) VALUES (?, ?, ?, 0)",
        (name, description, next_order),
    )
    conn.commit()
    return {"kind": name}


def list_kinds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT k.name, k.description, k.sort_order, k.builtin, COUNT(i.id) AS item_count
        FROM kinds k LEFT JOIN items i ON i.kind = k.name AND i.status = 'active'
        GROUP BY k.name ORDER BY k.sort_order
        """
    ).fetchall()


def remove_kind(conn: sqlite3.Connection, name: str, reassign_to: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM kinds WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise BrainError(f"no kind named {name!r}")
    in_use = conn.execute("SELECT COUNT(*) FROM items WHERE kind = ?", (name,)).fetchone()[0]
    if in_use:
        if reassign_to is None:
            raise BrainError(f"kind {name!r} is used by {in_use} item(s) — pass --reassign-to")
        if not conn.execute("SELECT 1 FROM kinds WHERE name = ?", (reassign_to,)).fetchone():
            raise BrainError(f"no kind named {reassign_to!r} to reassign to")
        conn.execute("UPDATE items SET kind = ? WHERE kind = ?", (reassign_to, name))
    conn.execute("DELETE FROM kinds WHERE name = ?", (name,))
    conn.commit()
    return {"kind": name, "reassigned": in_use if reassign_to else 0}

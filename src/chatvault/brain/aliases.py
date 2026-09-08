from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, resolve_item


def add_alias(conn: sqlite3.Connection, ref, alias: str, note: str | None = None) -> dict:
    item = resolve_item(conn, ref)
    conn.execute(
        "INSERT INTO aliases (item_id, alias, note) VALUES (?, ?, ?) ON CONFLICT(item_id, alias) DO NOTHING",
        (item["id"], alias, note),
    )
    conn.commit()
    return {"item_id": item["id"], "alias": alias}


def remove_alias(conn: sqlite3.Connection, ref, alias: str) -> dict:
    item = resolve_item(conn, ref)
    cur = conn.execute("DELETE FROM aliases WHERE item_id = ? AND alias = ?", (item["id"], alias))
    conn.commit()
    if cur.rowcount == 0:
        raise BrainError(f"item {item['id']} has no alias {alias!r}")
    return {"item_id": item["id"], "alias": alias}


def list_aliases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.id, a.alias, a.note, i.id AS item_id, i.ref, i.title
        FROM aliases a JOIN items i ON i.id = a.item_id
        ORDER BY i.title, a.alias
        """
    ).fetchall()

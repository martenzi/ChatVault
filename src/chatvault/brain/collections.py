from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, now, resolve_collection, resolve_item


def add_collection(conn: sqlite3.Connection, name: str, why: str, ref: str | None = None) -> dict:
    if why is None or not why.strip():
        raise BrainError("--why is required and must not be empty")
    if ref is not None and conn.execute("SELECT id FROM collections WHERE ref = ?", (ref,)).fetchone():
        raise BrainError(f"collection ref {ref!r} already exists")
    cur = conn.execute(
        "INSERT INTO collections (ref, name, why, created_at, status) VALUES (?, ?, ?, ?, 'active')",
        (ref, name, why.strip(), now()),
    )
    conn.commit()
    return {"collection_id": cur.lastrowid, "name": name}


def add_to_collection(conn: sqlite3.Connection, coll_ref, item_refs: list[str], position: int | None = None, note: str | None = None) -> dict:
    coll = resolve_collection(conn, coll_ref)
    max_pos = conn.execute("SELECT COALESCE(MAX(position), 0) FROM collection_items WHERE collection_id = ?", (coll["id"],)).fetchone()[0]
    added = []
    for i, item_ref in enumerate(item_refs):
        item = resolve_item(conn, item_ref)
        pos = position + i if position is not None else max_pos + 1
        if position is not None:
            conn.execute(
                "UPDATE collection_items SET position = position + 1 WHERE collection_id = ? AND position >= ?",
                (coll["id"], pos),
            )
        else:
            max_pos += 1
        conn.execute(
            """
            INSERT INTO collection_items (collection_id, item_id, position, note) VALUES (?, ?, ?, ?)
            ON CONFLICT(collection_id, item_id) DO UPDATE SET position = excluded.position, note = COALESCE(excluded.note, collection_items.note)
            """,
            (coll["id"], item["id"], pos, note),
        )
        added.append(item["id"])
    conn.commit()
    return {"collection_id": coll["id"], "items": added}


def remove_from_collection(conn: sqlite3.Connection, coll_ref, item_ref) -> dict:
    coll = resolve_collection(conn, coll_ref)
    item = resolve_item(conn, item_ref)
    conn.execute("DELETE FROM collection_items WHERE collection_id = ? AND item_id = ?", (coll["id"], item["id"]))
    conn.commit()
    return {"collection_id": coll["id"], "item_id": item["id"]}


def list_collections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.id, c.ref, c.name, c.why, c.status, COUNT(ci.item_id) AS item_count
        FROM collections c LEFT JOIN collection_items ci ON ci.collection_id = c.id
        GROUP BY c.id ORDER BY c.name
        """
    ).fetchall()


def show_collection(conn: sqlite3.Connection, coll_ref) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    coll = resolve_collection(conn, coll_ref)
    items = conn.execute(
        """
        SELECT i.*, ci.position, ci.note AS collection_note
        FROM collection_items ci JOIN items i ON i.id = ci.item_id
        WHERE ci.collection_id = ?
        ORDER BY ci.position
        """,
        (coll["id"],),
    ).fetchall()
    return coll, items


def reorder_collection(conn: sqlite3.Connection, coll_ref, ordered_item_refs: list[str]) -> dict:
    """Sets the collection's order to exactly `ordered_item_refs` (§7
    acceptance: reordering a collection changes only that collection's
    `show collection` output, and nothing else — this only ever writes
    collection_items.position, never anything on items itself)."""
    coll = resolve_collection(conn, coll_ref)
    current_ids = {r["item_id"] for r in conn.execute("SELECT item_id FROM collection_items WHERE collection_id = ?", (coll["id"],))}
    new_ids = [resolve_item(conn, r)["id"] for r in ordered_item_refs]
    if set(new_ids) != current_ids:
        raise BrainError("reorder must list exactly the collection's current members, in the new order")
    for position, item_id in enumerate(new_ids, start=1):
        conn.execute(
            "UPDATE collection_items SET position = ? WHERE collection_id = ? AND item_id = ?",
            (position, coll["id"], item_id),
        )
    conn.commit()
    return {"collection_id": coll["id"], "order": new_ids}

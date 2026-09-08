from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, now, resolve_item

# `brain add note <ref>` (docs/51_SPEC_brain_parity_and_naming.md §2.1 `note`
# row) — free-form running commentary on an item, distinct from the single
# mandatory `why` set once at creation and from `history` (which only
# records field edits, not standalone remarks). Migration
# migrations/brain/005_brain_parity.sql created the `notes` table this
# writes to.


def add_note(conn: sqlite3.Connection, ref, text: str) -> dict:
    if text is None or not text.strip():
        raise BrainError("note text is required and must not be empty")
    item = resolve_item(conn, ref)
    ts = now()
    cur = conn.execute(
        "INSERT INTO notes (item_id, text, created_at) VALUES (?, ?, ?)",
        (item["id"], text.strip(), ts),
    )
    conn.commit()
    return {"note_id": cur.lastrowid, "item_id": item["id"], "title": item["title"]}


def list_notes(conn: sqlite3.Connection, ref) -> list[sqlite3.Row]:
    item = resolve_item(conn, ref)
    return conn.execute(
        "SELECT * FROM notes WHERE item_id = ? ORDER BY created_at, id",
        (item["id"],),
    ).fetchall()

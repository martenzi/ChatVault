from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, known_rels, now, resolve_item


def add_rel(conn: sqlite3.Connection, name: str, description: str | None = None, symmetric: bool = False, inverse: str | None = None) -> dict:
    if conn.execute("SELECT 1 FROM rel_types WHERE name = ?", (name,)).fetchone():
        raise BrainError(f"rel type {name!r} already exists")
    conn.execute(
        "INSERT INTO rel_types (name, description, symmetric, inverse) VALUES (?, ?, ?, ?)",
        (name, description, int(symmetric), inverse),
    )
    conn.commit()
    return {"rel": name}


def link_entry(conn: sqlite3.Connection, a_ref, to_ref, as_rel: str, note: str | None = None) -> dict:
    """§1.5: item_links.rel is validated against rel_types at the
    application layer rather than a DB-level FK (migrations/brain/002_brain_v2.sql's
    top-of-file note explains why) — this is where that check lives."""
    if as_rel not in known_rels(conn):
        raise BrainError(f"--as must be one of {sorted(known_rels(conn))}, got {as_rel!r} — see `brain add rel`")
    a = resolve_item(conn, a_ref)
    b = resolve_item(conn, to_ref)
    conn.execute(
        "INSERT INTO item_links (from_id, to_id, rel, note, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (a["id"], b["id"], as_rel, note, now()),
    )
    conn.commit()
    return {"from_id": a["id"], "to_id": b["id"], "rel": as_rel}


def unlink_entry(conn: sqlite3.Connection, a_ref, to_ref, as_rel: str) -> dict:
    a = resolve_item(conn, a_ref)
    b = resolve_item(conn, to_ref)
    conn.execute("DELETE FROM item_links WHERE from_id = ? AND to_id = ? AND rel = ?", (a["id"], b["id"], as_rel))
    conn.commit()
    return {"from_id": a["id"], "to_id": b["id"], "rel": as_rel}


def list_links(conn: sqlite3.Connection, as_rel: str | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT il.from_id, fi.title AS from_title, il.to_id, ti.title AS to_title, il.rel, il.note, il.created_at
        FROM item_links il
        JOIN items fi ON fi.id = il.from_id
        JOIN items ti ON ti.id = il.to_id
    """
    params: list = []
    if as_rel is not None:
        sql += " WHERE il.rel = ?"
        params.append(as_rel)
    sql += " ORDER BY il.created_at"
    return conn.execute(sql, params).fetchall()

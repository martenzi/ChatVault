from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from chatvault.brain.core import BrainError, now, resolve_item

_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365}
_IN_PATTERN = re.compile(r"^(\d+)\s*([a-zA-Z]+)$")


def _parse_in(spec: str) -> str:
    """'3months', '2 weeks', '10days' -> an absolute ISO timestamp. Kept
    deliberately simple (fixed 30/365-day months/years) — this is a
    reminder date, not a calendar computation that needs to be exact."""
    m = _IN_PATTERN.match(spec.strip())
    if not m:
        raise BrainError(f"--in expects e.g. '3months', '2weeks', '10days', got {spec!r}")
    count, unit = int(m.group(1)), m.group(2).lower()
    if unit not in _UNIT_DAYS:
        raise BrainError(f"unknown unit {unit!r} in --in {spec!r}")
    when = datetime.now(timezone.utc) + timedelta(days=count * _UNIT_DAYS[unit])
    return when.isoformat()


def review_entry(conn: sqlite3.Connection, ref, in_spec: str, reason: str | None = None) -> dict:
    item = resolve_item(conn, ref)
    review_at = _parse_in(in_spec)
    cur = conn.execute(
        "INSERT INTO reviews (item_id, review_at, reason) VALUES (?, ?, ?)",
        (item["id"], review_at, reason),
    )
    conn.commit()
    return {"review_id": cur.lastrowid, "item_id": item["id"], "review_at": review_at}


def due(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.id AS review_id, r.review_at, r.reason, i.id AS item_id, i.ref, i.title
        FROM reviews r JOIN items i ON i.id = r.item_id
        WHERE r.done_at IS NULL AND r.review_at <= ?
        ORDER BY r.review_at
        """,
        (now(),),
    ).fetchall()


def done(conn: sqlite3.Connection, ref, outcome: str) -> dict:
    if outcome not in {"kept", "edited", "archived", "superseded"}:
        raise BrainError("--outcome must be one of kept, edited, archived, superseded")
    item = resolve_item(conn, ref)
    row = conn.execute(
        "SELECT id FROM reviews WHERE item_id = ? AND done_at IS NULL ORDER BY review_at LIMIT 1",
        (item["id"],),
    ).fetchone()
    if row is None:
        raise BrainError(f"no pending review for {ref!r}")
    conn.execute("UPDATE reviews SET done_at = ?, outcome = ? WHERE id = ?", (now(), outcome, row["id"]))
    conn.commit()
    return {"review_id": row["id"], "item_id": item["id"], "outcome": outcome}


def stale(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM v_stale ORDER BY created_at").fetchall()


def orphans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM v_orphans ORDER BY created_at").fetchall()

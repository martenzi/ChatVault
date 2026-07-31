from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from chatvault import hashing

_TARGET_TABLE = {"conversation": "conversations", "message": "messages", "block": "blocks"}
_TARGET_VIEW = {"conversation": "v_conversations", "message": "v_messages", "block": "v_blocks"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_for(target: str) -> str:
    if target not in _TARGET_TABLE:
        raise ValueError(f"target must be one of {list(_TARGET_TABLE)}, got {target!r}")
    return _TARGET_TABLE[target]


def tag_add(conn: sqlite3.Connection, name: str, where: str, target: str) -> int:
    view = _TARGET_VIEW[target]
    now = _now()
    conn.execute(
        "INSERT INTO tags (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING", (name, now)
    )
    tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]

    ids = [row[0] for row in conn.execute(f"SELECT id FROM {view} WHERE {where}").fetchall()]  # noqa: S608
    for target_id in ids:
        conn.execute(
            """
            INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
            VALUES (?, ?, ?, 'manual', ?)
            ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
            """,
            (tag_id, target, target_id, now),
        )
    conn.commit()
    return len(ids)


def tag_list(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.name, COUNT(tl.tag_id) AS uses
        FROM tags t
        LEFT JOIN tag_links tl ON tl.tag_id = t.id
        GROUP BY t.id
        ORDER BY t.name
        """
    ).fetchall()


def note_add(conn: sqlite3.Connection, target_type: str, target_id: int, text: str) -> int:
    if target_type not in _TARGET_TABLE:
        raise ValueError(f"target_type must be one of {list(_TARGET_TABLE)}, got {target_type!r}")
    note_hash = hashing.note_hash(text)
    existing = conn.execute(
        "SELECT id FROM annotations WHERE target_type = ? AND target_id = ? AND note_hash = ?",
        (target_type, target_id, note_hash),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    now = _now()
    cur = conn.execute(
        "INSERT INTO annotations (target_type, target_id, note, note_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (target_type, target_id, text, note_hash, now),
    )
    conn.commit()
    return cur.lastrowid


def matching_count(conn: sqlite3.Connection, target: str, where: str, currently_excluded: bool = False) -> int:
    """Cheap precount used by the CLI to decide whether a bulk exclude/restore
    needs an automatic backup first (spec: >100 targets)."""
    table = _table_for(target)
    flag = 1 if currently_excluded else 0
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE excluded = {flag} AND ({where})"  # noqa: S608
    ).fetchone()[0]


def exclude(conn: sqlite3.Connection, target: str, where: str, reason: str | None) -> int:
    table = _table_for(target)
    now = _now()
    ids = [
        row[0]
        for row in conn.execute(
            f"SELECT id FROM {table} WHERE excluded = 0 AND ({where})"  # noqa: S608
        ).fetchall()
    ]
    for target_id in ids:
        conn.execute(f"UPDATE {table} SET excluded = 1 WHERE id = ?", (target_id,))  # noqa: S608
        conn.execute(
            "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) VALUES (?, ?, 'exclude', ?, ?, ?)",
            (target, target_id, reason, where, now),
        )
    conn.commit()
    return len(ids)


def restore(conn: sqlite3.Connection, target: str, where: str) -> int:
    table = _table_for(target)
    now = _now()
    ids = [
        row[0]
        for row in conn.execute(
            f"SELECT id FROM {table} WHERE excluded = 1 AND ({where})"  # noqa: S608
        ).fetchall()
    ]
    for target_id in ids:
        conn.execute(f"UPDATE {table} SET excluded = 0 WHERE id = ?", (target_id,))  # noqa: S608
        conn.execute(
            "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) VALUES (?, ?, 'restore', NULL, ?, ?)",
            (target, target_id, where, now),
        )
    conn.commit()
    return len(ids)


def view_save(conn: sqlite3.Connection, name: str, sql: str, description: str | None = None) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO saved_views (name, description, sql, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET sql = excluded.sql, description = excluded.description
        """,
        (name, description, sql, now),
    )
    conn.commit()


def view_run(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    row = conn.execute("SELECT sql FROM saved_views WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"no saved view named {name!r}")
    return conn.execute(row["sql"]).fetchall()


def view_list(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT name, description, created_at FROM saved_views ORDER BY name").fetchall()


def dedupe_report(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    superseded = conn.execute(
        "SELECT id, uid, superseded_by FROM conversations WHERE superseded_by IS NOT NULL"
    ).fetchall()
    flagged = conn.execute("SELECT id, uid FROM conversations WHERE uid LIKE '%#dup-%'").fetchall()
    repeated_blocks = conn.execute(
        """
        SELECT content_hash, COUNT(*) AS n
        FROM blocks WHERE excluded = 0
        GROUP BY content_hash HAVING COUNT(*) > 20
        ORDER BY n DESC
        """
    ).fetchall()
    return {"superseded": superseded, "flagged_duplicates": flagged, "repeated_blocks": repeated_blocks}

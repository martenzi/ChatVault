from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from chatvault import backup as backup_mod
from chatvault.config import Config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_attachments(
    conn: sqlite3.Connection,
    conv: int | None = None,
    project: int | None = None,
    platform: str | None = None,
    type_: str | None = None,
    missing: bool = False,
) -> list[sqlite3.Row]:
    clauses = ["1=1"]
    params: list = []
    joins = ""

    if conv is not None:
        joins += " JOIN attachment_links al_c ON al_c.attachment_id = a.id AND al_c.target_type = 'message'"
        joins += " JOIN messages m ON m.id = al_c.target_id AND m.conv_id = ?"
        params.append(conv)
    if project is not None:
        joins += " JOIN attachment_links al_p ON al_p.attachment_id = a.id AND al_p.target_type = 'project' AND al_p.target_id = ?"
        params.append(project)
    if platform:
        clauses.append("a.platform = ?")
        params.append(platform)
    if type_:
        clauses.append("a.ext = ?")
        params.append(type_)
    if missing:
        clauses.append("a.availability = 'missing'")

    sql = f"""
        SELECT DISTINCT a.* FROM v_attachments a
        {joins}
        WHERE {' AND '.join(clauses)}
        ORDER BY a.first_seen_at DESC
    """  # noqa: S608
    return conn.execute(sql, params).fetchall()


def show(conn: sqlite3.Connection, attachment_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS bytes,
               SUM(CASE WHEN availability = 'missing' THEN 1 ELSE 0 END) AS missing
        FROM v_attachments
        """
    ).fetchone()
    by_platform = conn.execute(
        "SELECT platform, COUNT(*) AS n FROM v_attachments GROUP BY platform ORDER BY n DESC"
    ).fetchall()
    return {"total": row["n"], "bytes": row["bytes"], "missing": row["missing"], "by_platform": by_platform}


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.id, a.filename, a.platform, a.ext,
               snippet(attachments_fts, 0, '[', ']', '...', 10) AS snippet
        FROM attachments_fts f
        JOIN v_attachments a ON a.id = f.rowid
        WHERE attachments_fts MATCH ?
        ORDER BY rank LIMIT ?
        """,
        (query, limit),
    ).fetchall()


def _resolve_ids(conn: sqlite3.Connection, attachment_id: int | None, where: str | None) -> list[int]:
    if attachment_id is not None:
        return [attachment_id]
    if where:
        return [r[0] for r in conn.execute(f"SELECT id FROM attachments WHERE {where}").fetchall()]  # noqa: S608
    raise ValueError("pass an attachment id or --where")


def exclude(conn: sqlite3.Connection, attachment_id: int | None = None, where: str | None = None, reason: str | None = None) -> int:
    """Hide it. Reversible. The normal move."""
    now = _now()
    ids = [i for i in _resolve_ids(conn, attachment_id, where) if _is_excluded(conn, i) == 0]
    for aid in ids:
        conn.execute("UPDATE attachments SET excluded = 1 WHERE id = ?", (aid,))
        conn.execute(
            "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) VALUES ('attachment', ?, 'exclude', ?, ?, ?)",
            (aid, reason, where, now),
        )
    conn.commit()
    return len(ids)


def restore(conn: sqlite3.Connection, attachment_id: int | None = None, where: str | None = None) -> int:
    now = _now()
    ids = [i for i in _resolve_ids(conn, attachment_id, where) if _is_excluded(conn, i) == 1]
    for aid in ids:
        conn.execute("UPDATE attachments SET excluded = 0 WHERE id = ?", (aid,))
        conn.execute(
            "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) VALUES ('attachment', ?, 'restore', NULL, ?, ?)",
            (aid, where, now),
        )
    conn.commit()
    return len(ids)


def _is_excluded(conn: sqlite3.Connection, attachment_id: int) -> int:
    row = conn.execute("SELECT excluded FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    return row["excluded"] if row else 0


def redact(conn: sqlite3.Connection, attachment_id: int | None = None, where: str | None = None, confirm: bool = False) -> int:
    """Wipe the content, keep the record: 'I don't want this text stored, but
    I want to remember it existed.' content -> NULL, availability -> 'missing'.
    Logged, requires --confirm (irreversible: the text itself is gone)."""
    if not confirm:
        raise ValueError("refusing to redact without --confirm")
    now = _now()
    ids = _resolve_ids(conn, attachment_id, where)
    for aid in ids:
        conn.execute(
            "UPDATE attachments SET content = NULL, availability = 'missing' WHERE id = ?", (aid,)
        )
        conn.execute(
            "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) VALUES ('attachment', ?, 'redact', 'content removed, record kept', ?, ?)",
            (aid, where, now),
        )
    conn.commit()
    return len(ids)


def purge(conn: sqlite3.Connection, config: Config, attachment_id: int | None = None, where: str | None = None, confirm: bool = False) -> int:
    """Delete the row entirely. Takes a backup first, logs it, requires --confirm."""
    if not confirm:
        raise ValueError("refusing to purge without --confirm")
    ids = _resolve_ids(conn, attachment_id, where)
    if not ids:
        return 0

    backup_mod.create_backup(config, label="pre-attachment-purge")

    now = _now()
    for aid in ids:
        conn.execute(
            "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) VALUES ('attachment', ?, 'purge', 'row deleted', ?, ?)",
            (aid, where, now),
        )
        # blocks.attachment_id has no ON DELETE action (it's a pointer, not an
        # ownership edge — the block itself must survive); attachment_links
        # does cascade since a link without its attachment is meaningless.
        conn.execute("UPDATE blocks SET attachment_id = NULL WHERE attachment_id = ?", (aid,))
        conn.execute("DELETE FROM attachments WHERE id = ?", (aid,))
    conn.commit()
    return len(ids)

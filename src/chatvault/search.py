from __future__ import annotations

import sqlite3

from chatvault import tags as tags_mod
from chatvault.browse import HIDDEN_TYPES_SQL


def search(
    conn: sqlite3.Connection,
    query: str,
    platform: str | tuple[str, ...] | None = None,
    type_: str | None = None,
    limit: int = 20,
    include_injected: bool = False,
    since: str | None = None,
    before: str | None = None,
    totals: dict | None = None,
    active_since: str | None = None,
) -> list[sqlite3.Row]:
    """Block-level FTS. `totals`, if given, is filled with the uncapped
    `blocks` and `conversations` match counts (ignoring `limit`)."""
    sql = """
        SELECT b.id, b.conv_id, b.type, b.tool_name, snippet(blocks_fts, 0, '[', ']', '…', 24) AS snippet, b.text, c.uid, c.title, c.platform, c.started_at, c.ended_at
        FROM blocks_fts f
        JOIN v_blocks b ON b.id = f.rowid
        JOIN v_conversations c ON c.id = b.conv_id
        WHERE blocks_fts MATCH ?
    """
    params: list = [query]
    if platform:
        names = (platform,) if isinstance(platform, str) else tuple(platform)
        if len(names) == 1:
            sql += " AND c.platform = ?"
            params.append(names[0])
        else:
            sql += f" AND c.platform IN ({','.join('?' * len(names))})"
            params.extend(names)
    if type_:
        sql += " AND b.type = ?"
        params.append(type_)
    elif not include_injected:
        sql += f" AND b.type NOT IN {HIDDEN_TYPES_SQL}"
    if since:
        sql += " AND c.started_at >= ?"
        params.append(since)
    if before:
        sql += " AND c.started_at < ?"
        params.append(before)
    if active_since:
        sql += " AND COALESCE(c.ended_at, c.started_at) >= ?"
        params.append(active_since)
    if totals is not None:
        head, _, _ = sql.partition("WHERE blocks_fts MATCH ?")
        count_sql = (
            "SELECT COUNT(*), COUNT(DISTINCT c.id) FROM blocks_fts f "
            "JOIN v_blocks b ON b.id = f.rowid JOIN v_conversations c ON c.id = b.conv_id "
            "WHERE blocks_fts MATCH ?" + sql.partition("WHERE blocks_fts MATCH ?")[2]
        )
        n_blocks, n_convs = conn.execute(count_sql, params).fetchone()
        totals["blocks"], totals["conversations"] = n_blocks, n_convs
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


_STATS_GROUP_COLUMN = {
    "model": "COALESCE(m.model, 'unknown')",
    "platform": "c.platform",
    "project": "COALESCE(c.project, 'none')",
    "month": "substr(c.started_at, 1, 7)",
    "tool": "COALESCE(b.tool_name, 'none')",
    "tag": None,  # special-cased below — grouped by tag name, not a plain column
}


def stats(conn: sqlite3.Connection, by: str, prefix: str | None = None) -> list[sqlite3.Row]:
    if by not in _STATS_GROUP_COLUMN:
        raise ValueError(f"by must be one of {list(_STATS_GROUP_COLUMN)}, got {by!r}")

    if by == "tag":
        where_frag, params = ("1=1", []) if not prefix else tags_mod.tag_match("t.name", prefix)
        sql = f"""
            SELECT t.name AS key, COUNT(DISTINCT tl.target_id) AS n
            FROM tags t JOIN tag_links tl ON tl.tag_id = t.id
            WHERE tl.target_type = 'conversation' AND {where_frag}
            GROUP BY t.name ORDER BY n DESC
        """  # noqa: S608
        return conn.execute(sql, params).fetchall()

    if by == "tool":
        sql = """
            SELECT COALESCE(b.tool_name, 'none') AS key, COUNT(*) AS n
            FROM v_blocks b
            WHERE b.type IN ('tool_use', 'tool_result')
            GROUP BY key ORDER BY n DESC
        """
        return conn.execute(sql).fetchall()

    if by == "model":
        sql = """
            SELECT COALESCE(m.model, 'unknown') AS key, COUNT(*) AS n
            FROM v_messages m
            WHERE m.role = 'assistant'
            GROUP BY key ORDER BY n DESC
        """
        return conn.execute(sql).fetchall()

    col = _STATS_GROUP_COLUMN[by]
    sql = f"""
        SELECT {col} AS key, COUNT(*) AS n
        FROM v_conversations c
        GROUP BY key ORDER BY n DESC
    """  # noqa: S608
    return conn.execute(sql).fetchall()


def unknown_block_report(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.platform, COUNT(*) AS n
        FROM v_blocks b
        JOIN v_conversations c ON c.id = b.conv_id
        WHERE b.type = 'unknown'
        GROUP BY c.platform ORDER BY n DESC
        """
    ).fetchall()

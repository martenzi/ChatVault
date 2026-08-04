from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from chatvault import tags as tags_mod
from chatvault.config import Config

# Spec v1.3 says `~/.chatvault/last_query.json`. Scoped under the vault root
# instead: this repo already keeps everything vault-scoped (db/, exports/,
# raw/), a literal home-dir path would collide across multiple vaults and
# can't be sandboxed in tests, and nothing about the behaviour the spec asks
# for (content-based invalidation, row -> conv_id cache) depends on the exact
# location.
_CACHE_RELATIVE = Path(".chatvault") / "last_query.json"

_SORT_COLUMNS = {
    "recent": "started_at DESC",
    "oldest": "started_at ASC",
    "longest": "message_count DESC",
}


class StaleCacheError(ValueError):
    """Raised when --pick is used but the cached `find` result is missing or stale."""


def _fts_phrase(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


def find(
    conn: sqlite3.Connection,
    query: str | None = None,
    platform: str | None = None,
    since: str | None = None,
    before: str | None = None,
    project: str | None = None,
    tag: str | None = None,
    has_attachments: bool = False,
    untagged: bool = False,
    limit: int = 20,
    sort: str = "recent",
) -> tuple[list[sqlite3.Row], int]:
    """Human-facing lister. No query = most recent first. With a query, titles
    match first, body text (FTS) second — returns (rows limited to `limit`,
    total match count)."""
    if sort not in _SORT_COLUMNS:
        raise ValueError(f"sort must be one of {sorted(_SORT_COLUMNS)}, got {sort!r}")

    clauses = ["1=1"]
    params: list = []
    if platform:
        clauses.append("c.platform = ?")
        params.append(platform)
    if since:
        clauses.append("c.started_at >= ?")
        params.append(since)
    if before:
        clauses.append("c.started_at < ?")
        params.append(before)
    if project:
        clauses.append("c.project = ?")
        params.append(project)
    if tag:
        tag_frag, tag_params = tags_mod.tag_match("t.name", tag)
        clauses.append(
            "EXISTS (SELECT 1 FROM tag_links tl JOIN tags t ON t.id = tl.tag_id "
            f"WHERE tl.target_type = 'conversation' AND tl.target_id = c.id AND {tag_frag})"
        )
        params.extend(tag_params)
    if has_attachments:
        clauses.append(
            "EXISTS (SELECT 1 FROM v_blocks b WHERE b.conv_id = c.id AND b.type = 'attachment')"
        )
    if untagged:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM tag_links tl WHERE tl.target_type = 'conversation' AND tl.target_id = c.id)"
        )
    where_sql = " AND ".join(clauses)

    def _fetch(extra_clause: str, extra_params: list) -> list[sqlite3.Row]:
        sql = f"""
            SELECT c.*,
              (SELECT COUNT(*) FROM v_blocks b WHERE b.conv_id = c.id AND b.type = 'attachment') AS attachment_count
            FROM v_conversations c
            WHERE {where_sql} {extra_clause}
            ORDER BY {_SORT_COLUMNS[sort]}
        """  # noqa: S608
        return conn.execute(sql, params + extra_params).fetchall()

    if query:
        title_rows = _fetch("AND c.title LIKE ?", [f"%{query}%"])
        title_ids = {r["id"] for r in title_rows}

        body_id_rows = conn.execute(
            f"""
            SELECT DISTINCT c.id FROM blocks_fts f
            JOIN v_blocks b ON b.id = f.rowid
            JOIN v_conversations c ON c.id = b.conv_id
            WHERE blocks_fts MATCH ? AND {where_sql}
            """,  # noqa: S608
            [_fts_phrase(query)] + params,
        ).fetchall()
        body_ids = [r["id"] for r in body_id_rows if r["id"] not in title_ids]

        body_rows = []
        if body_ids:
            placeholders = ",".join("?" * len(body_ids))
            body_rows = _fetch(f"AND c.id IN ({placeholders})", body_ids)

        all_rows = list(title_rows) + list(body_rows)
    else:
        all_rows = _fetch("", [])

    total = len(all_rows)
    return all_rows[:limit], total


def cache_path(config: Config) -> Path:
    return config.root / _CACHE_RELATIVE


def _db_fingerprint(conn: sqlite3.Connection, config: Config) -> dict:
    data_version = conn.execute("PRAGMA data_version").fetchone()[0]
    mtime = config.db_path.stat().st_mtime if config.db_path.exists() else None
    return {"data_version": data_version, "mtime": mtime}


def save_result_cache(
    conn: sqlite3.Connection, config: Config, rows: list[sqlite3.Row], query_params: dict
) -> None:
    path = cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        **_db_fingerprint(conn, config),
        "query": query_params,
        "rows": [{"row": i + 1, "conv_id": r["id"], "uid": r["uid"]} for i, r in enumerate(rows)],
    }
    path.write_text(json.dumps(data))


def resolve_pick(conn: sqlite3.Connection, config: Config, pick: str) -> list[int]:
    """Resolve a --pick spec ('2' or '1,3') against the cached last `find`
    result. Cache invalidation is content-based (PRAGMA data_version + db
    mtime), never time-based — a three-day-old result set is still valid if
    nothing was ingested since."""
    path = cache_path(config)
    if not path.exists():
        raise StaleCacheError("no cached `find` results — run `chatvault find` first")

    data = json.loads(path.read_text())
    current = _db_fingerprint(conn, config)
    if data.get("data_version") != current["data_version"] or data.get("mtime") != current["mtime"]:
        raise StaleCacheError(
            "cached `find` results are stale (database changed since) — re-run `chatvault find`"
        )

    by_row = {r["row"]: r["conv_id"] for r in data["rows"]}
    conv_ids = []
    for part in pick.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ValueError(f"invalid row number: {part!r}")
        n = int(part)
        if n not in by_row:
            raise ValueError(f"row {n} not in the last `find` results (1-{len(by_row)})")
        conv_ids.append(by_row[n])
    return conv_ids

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
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


# Block types that are provider boilerplate, not user/assistant content.
# Hidden from search at the query layer (the rows stay indexed).
HIDDEN_BLOCK_TYPES = ("injected_context", "meta")
HIDDEN_TYPES_SQL = "(" + ", ".join(f"'{t}'" for t in HIDDEN_BLOCK_TYPES) + ")"


def find(*args, **kwargs) -> tuple[list[sqlite3.Row], int]:
    """See `find_with_hidden`; returns just (rows, total)."""
    rows, total, _hidden = find_with_hidden(*args, **kwargs)
    return rows, total


def _filter_clauses(
    platform: str | tuple[str, ...] | None = None,
    since: str | None = None,
    before: str | None = None,
    project: str | None = None,
    tag: str | None = None,
    has_attachments: bool = False,
    untagged: bool = False,
) -> tuple[list[str], list]:
    """WHERE fragments (over `v_conversations c`) shared by find and latest."""
    clauses = ["1=1"]
    params: list = []
    if platform:
        names = (platform,) if isinstance(platform, str) else tuple(platform)
        if len(names) == 1:
            clauses.append("c.platform = ?")
            params.append(names[0])
        else:
            clauses.append(f"c.platform IN ({','.join('?' * len(names))})")
            params.extend(names)
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
            # unary + stops the planner choosing the block-type index (a full scan per conversation)
            "EXISTS (SELECT 1 FROM v_blocks b WHERE b.conv_id = c.id AND +b.type = 'attachment')"
        )
    if untagged:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM tag_links tl WHERE tl.target_type = 'conversation' AND tl.target_id = c.id)"
        )
    return clauses, params


def find_with_hidden(
    conn: sqlite3.Connection,
    query: str | None = None,
    platform: str | tuple[str, ...] | None = None,
    since: str | None = None,
    before: str | None = None,
    project: str | None = None,
    tag: str | None = None,
    has_attachments: bool = False,
    untagged: bool = False,
    limit: int = 20,
    sort: str = "recent",
    include_injected: bool = False,
) -> tuple[list[sqlite3.Row], int, int]:
    """Human-facing lister. No query = most recent first. With a query, titles
    match first, body text (FTS) second — returns (rows limited to `limit`,
    total match count, hidden count). Hidden = conversations matched only via
    injected_context/meta blocks and suppressed (always 0 with include_injected)."""
    if sort not in _SORT_COLUMNS:
        raise ValueError(f"sort must be one of {sorted(_SORT_COLUMNS)}, got {sort!r}")

    clauses, params = _filter_clauses(
        platform=platform, since=since, before=before, project=project, tag=tag,
        has_attachments=has_attachments, untagged=untagged,
    )
    where_sql = " AND ".join(clauses)
    order_sql = _SORT_COLUMNS[sort]

    def _ids(extra_clause: str, extra_params: list, cap: int | None = None) -> list[int]:
        sql = f"SELECT c.id FROM v_conversations c WHERE {where_sql} {extra_clause} ORDER BY {order_sql}"  # noqa: S608
        if cap is not None:
            sql += " LIMIT ?"
            extra_params = extra_params + [cap]
        return [r["id"] for r in conn.execute(sql, params + extra_params).fetchall()]

    hidden = 0
    if query:
        # Ids only until the page is known: the total is the true size of the
        # title+body union, but full rows are fetched for the `limit` shown.
        title_ids = _ids("AND c.title LIKE ?", [f"%{query}%"])
        title_set = set(title_ids)

        # One FTS pass: per conversation, did any matching block fall outside
        # the hidden types? (vis=0 means matched only via injected/meta.)
        matched = conn.execute(
            f"""
            SELECT c.id AS id, MAX(b.type NOT IN {HIDDEN_TYPES_SQL}) AS vis FROM blocks_fts f
            JOIN v_blocks b ON b.id = f.rowid
            JOIN v_conversations c ON c.id = b.conv_id
            WHERE blocks_fts MATCH ? AND {where_sql}
            GROUP BY c.id
            """,  # noqa: S608
            [_fts_phrase(query)] + params,
        ).fetchall()
        matched = [r for r in matched if r["id"] not in title_set]
        if include_injected:
            body_ids = [r["id"] for r in matched]
        else:
            body_ids = [r["id"] for r in matched if r["vis"]]
            hidden = len(matched) - len(body_ids)

        body_ordered: list[int] = []
        if body_ids:
            placeholders = ",".join("?" * len(body_ids))
            body_ordered = _ids(f"AND c.id IN ({placeholders})", body_ids)

        all_ids = title_ids + body_ordered
        total = len(all_ids)
        page_ids = all_ids[:limit]
    else:
        total = conn.execute(f"SELECT COUNT(*) FROM v_conversations c WHERE {where_sql}", params).fetchone()[0]  # noqa: S608
        page_ids = _ids("", [], cap=limit)

    return rows_by_ids(conn, page_ids), total, hidden


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


def _expand_pick_token(token: str) -> list[int]:
    if "-" in token:
        start_s, _, end_s = token.partition("-")
        if not (start_s.isdigit() and end_s.isdigit()):
            raise ValueError(f"invalid range: {token!r}")
        start, end = int(start_s), int(end_s)
        if start > end:
            raise ValueError(f"invalid range: {token!r} (start comes after end)")
        return list(range(start, end + 1))
    if not token.isdigit():
        raise ValueError(f"invalid row number: {token!r}")
    return [int(token)]


def parse_pick_spec(pick: str, all_rows: list[int]) -> list[int]:
    """Parse a --pick spec into the row numbers it selects, in resolution
    order (duplicates collapsed to their first mention). Grammar, comma
    separated:
      - a row number ('3')
      - an inclusive range ('1-5')
      - the literal 'all'
      - any of the above prefixed with '!' to exclude it (e.g. '!3', '!5-9')
    A spec made only of exclusions (e.g. '!3') implicitly starts from 'all'.
    Row numbers aren't validated against `all_rows` here beyond expanding
    'all' — the caller checks each one against its own row -> id mapping so
    the error names the right source ('find' vs 'stash')."""
    includes: list[int] = []
    excludes: set[int] = set()
    saw_include = False
    for part in pick.split(","):
        part = part.strip()
        if not part:
            continue
        negate = part.startswith("!")
        body = part[1:] if negate else part
        expanded = list(all_rows) if body.lower() == "all" else _expand_pick_token(body)
        if negate:
            excludes.update(expanded)
        else:
            saw_include = True
            for n in expanded:
                if n not in includes:
                    includes.append(n)
    if not saw_include:
        includes = list(all_rows)
    return [n for n in includes if n not in excludes]


def resolve_pick(conn: sqlite3.Connection, config: Config, pick: str) -> list[int]:
    """Resolve a --pick spec (see `parse_pick_spec`: '2', '1,3', '1-5',
    'all,!3', ...) against the cached last `find`/`search` result — both
    commands write the same cache, so `--pick` works after either one.
    Cache invalidation is content-based (PRAGMA data_version + db mtime),
    never time-based — a three-day-old result set is still valid if nothing
    was ingested since."""
    path = cache_path(config)
    if not path.exists():
        raise StaleCacheError("no cached results — run `chatvault find` or `chatvault search` first")

    data = json.loads(path.read_text())
    current = _db_fingerprint(conn, config)
    if data.get("data_version") != current["data_version"] or data.get("mtime") != current["mtime"]:
        raise StaleCacheError(
            "cached results are stale (database changed since) — re-run `chatvault find`/`chatvault search`"
        )

    by_row = {r["row"]: r["conv_id"] for r in data["rows"]}
    picked_rows = parse_pick_spec(pick, sorted(by_row))

    conv_ids = []
    for n in picked_rows:
        if n not in by_row:
            raise ValueError(f"row {n} not in the last results (1-{len(by_row)})")
        conv_ids.append(by_row[n])
    return conv_ids


_RELATIVE_RE = re.compile(r"^(\d+)(h|d|w|m|y)$")
# 'm' = 30 days and 'y' = 365 days: calendar-exact months are not worth a dependency.
_UNIT = {"h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1),
         "m": timedelta(days=30), "y": timedelta(days=365)}


def resolve_relative_since(
    since: str | None = None,
    last: str | None = None,
    today: bool = False,
    week: bool = False,
    now: datetime | None = None,
) -> str | None:
    """Fold --last/--today/--week into the one `since` string the existing
    --since plumbing takes. More than one of the four is an error — never a
    silent pick. `--last` is UTC-relative ('7d' = now minus 7 days);
    `--today` is the local calendar date."""
    given = [n for n, v in (("--since", since), ("--last", last), ("--today", today), ("--week", week)) if v]
    if len(given) > 1:
        raise ValueError(f"{' and '.join(given)} can't be combined — pick one")
    if since or not given:
        return since
    if today:
        return (now or datetime.now()).date().isoformat()
    if week:
        last = "7d"
    m = _RELATIVE_RE.match(last)
    if not m:
        raise ValueError(f"--last must look like 24h, 7d, 2w, 3m or 1y, got {last!r}")
    n, unit = int(m.group(1)), m.group(2)
    return ((now or datetime.now(timezone.utc)) - n * _UNIT[unit]).strftime("%Y-%m-%dT%H:%M:%S")


def _require_window_functions() -> None:
    if sqlite3.sqlite_version_info < (3, 25, 0):
        raise ValueError(
            f"--per-platform needs SQLite 3.25+ (window functions); this Python bundles {sqlite3.sqlite_version}"
        )


def latest(
    conn: sqlite3.Connection,
    limit: int = 10,
    per_platform: int | None = None,
    platform: str | tuple[str, ...] | None = None,
    since: str | None = None,
    before: str | None = None,
    project: str | None = None,
    tag: str | None = None,
    by_ingest: bool = False,
) -> tuple[list[sqlite3.Row], int]:
    """Recency listing with no text matching. Returns (rows, total matching
    the filters). Rows carry `days_ago` (whole days from the ordering column
    to now; NULL when that column is NULL) and `attachment_count`. With
    `per_platform=N`, the N newest of every platform, ordered platform then
    newest-first; otherwise the `limit` newest overall."""
    col = "ingested_at" if by_ingest else "started_at"
    clauses, params = _filter_clauses(platform=platform, since=since, before=before, project=project, tag=tag)
    where_sql = " AND ".join(clauses)
    total = conn.execute(f"SELECT COUNT(*) FROM v_conversations c WHERE {where_sql}", params).fetchone()[0]  # noqa: S608
    cols = (
        "c.id, c.uid, c.platform, c.title, c.project, c.started_at, c.ingested_at, c.message_count, "
        f"CAST(julianday('now') - julianday(c.{col}) AS INTEGER) AS days_ago, "
        "(SELECT COUNT(*) FROM v_blocks b WHERE b.conv_id = c.id AND b.type = 'attachment') AS attachment_count"
    )
    if per_platform is not None:
        _require_window_functions()
        sql = f"""
            SELECT * FROM (
              SELECT {cols}, ROW_NUMBER() OVER (PARTITION BY c.platform ORDER BY c.{col} DESC, c.id DESC) AS rn
              FROM v_conversations c WHERE {where_sql}
            ) WHERE rn <= ? ORDER BY platform, {col} DESC, id DESC
        """  # noqa: S608
        rows = conn.execute(sql, params + [per_platform]).fetchall()
    else:
        sql = f"SELECT {cols} FROM v_conversations c WHERE {where_sql} ORDER BY c.{col} DESC, c.id DESC LIMIT ?"  # noqa: S608
        rows = conn.execute(sql, params + [limit]).fetchall()
    return rows, total


def rows_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """Live conversations for `ids`, in the given order (ids that are not live —
    superseded or excluded — are dropped), with the same columns find shows."""
    if not ids:
        return []
    seen = list(dict.fromkeys(ids))
    out: dict[int, sqlite3.Row] = {}
    for i in range(0, len(seen), 500):
        chunk = seen[i:i + 500]
        rs = conn.execute(
            f"""
            SELECT c.id, c.uid, c.platform, c.title, c.project, c.started_at, c.message_count,
              (SELECT COUNT(*) FROM v_blocks b WHERE b.conv_id = c.id AND b.type = 'attachment') AS attachment_count
            FROM v_conversations c WHERE c.id IN ({",".join("?" * len(chunk))})
            """,  # noqa: S608
            chunk,
        ).fetchall()
        out.update({r["id"]: r for r in rs})
    return [out[i] for i in seen if i in out]

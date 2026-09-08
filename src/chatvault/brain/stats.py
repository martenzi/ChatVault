from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from chatvault.brain.core import now, record_history, sha256_text


def stats(conn: sqlite3.Connection) -> dict:
    by_kind = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) AS n FROM items WHERE status='active' GROUP BY kind")}
    by_confidence = {r["confidence"] or "(none)": r["n"] for r in conn.execute("SELECT confidence, COUNT(*) AS n FROM items WHERE status='active' GROUP BY confidence")}
    by_tag = {r["name"]: r["n"] for r in conn.execute(
        "SELECT t.name, COUNT(*) AS n FROM tag_links tl JOIN tags t ON t.id = tl.tag_id GROUP BY t.name ORDER BY n DESC"
    )}
    by_collection = {r["name"]: r["n"] for r in conn.execute(
        "SELECT c.name, COUNT(*) AS n FROM collection_items ci JOIN collections c ON c.id = ci.collection_id GROUP BY c.name"
    )}
    total_active = conn.execute("SELECT COUNT(*) FROM items WHERE status='active'").fetchone()[0]
    total_archived = conn.execute("SELECT COUNT(*) FROM items WHERE status='archived'").fetchone()[0]
    return {
        "total_active": total_active,
        "total_archived": total_archived,
        "by_kind": by_kind,
        "by_confidence": by_confidence,
        "by_tag": by_tag,
        "by_collection": by_collection,
    }


# docs/51_SPEC_brain_parity_and_naming.md §2.1 `stats --by <axis>` row:
# `stats` (the bare/default form above) already reports kind/confidence/
# tag/collection; `kind`/`confidence`/`collection` are handled here too so
# `stats --by <axis>` is one uniform entry point covering every axis in the
# spec's list, not just the two genuinely new ones (status, sensitivity).
_STATS_BY_COLUMNS = {"kind": "kind", "status": "status", "confidence": "confidence", "sensitivity": "sensitivity"}


def stats_by(conn: sqlite3.Connection, axis: str) -> dict:
    """`brain stats --by kind|tag|collection|status|confidence|sensitivity`.
    tag/collection go through their join tables (an item can carry more than
    one of each); the rest are a single column on `items` and group
    directly. Counts are over every item regardless of status/excluded —
    this is a health/composition report, not a working-set view, so an
    archived or excluded item's kind/tag/etc. still counts."""
    if axis in _STATS_BY_COLUMNS:
        column = _STATS_BY_COLUMNS[axis]
        rows = conn.execute(f"SELECT {column} AS k, COUNT(*) AS n FROM items GROUP BY {column} ORDER BY n DESC").fetchall()  # noqa: S608
        return {(r["k"] or "(none)"): r["n"] for r in rows}
    if axis == "tag":
        rows = conn.execute(
            "SELECT t.name AS k, COUNT(*) AS n FROM tag_links tl JOIN tags t ON t.id = tl.tag_id GROUP BY t.name ORDER BY n DESC"
        ).fetchall()
        return {r["k"]: r["n"] for r in rows}
    if axis == "collection":
        rows = conn.execute(
            "SELECT c.name AS k, COUNT(*) AS n FROM collection_items ci JOIN collections c ON c.id = ci.collection_id GROUP BY c.name ORDER BY n DESC"
        ).fetchall()
        return {r["k"]: r["n"] for r in rows}
    raise ValueError(f"--by must be one of kind, tag, collection, status, confidence, sensitivity, got {axis!r}")


def stats_edits(conn: sqlite3.Connection) -> dict:
    by_origin = {r["origin"]: r["n"] for r in conn.execute("SELECT origin, COUNT(*) AS n FROM history GROUP BY origin")}
    per_item = conn.execute(
        """
        SELECT i.id, i.ref, i.title, i.edit_count,
               (SELECT COUNT(*) FROM history h WHERE h.item_id = i.id AND h.origin = 'detected') AS detected_edits
        FROM items i WHERE i.edit_count > 0
        ORDER BY i.edit_count DESC
        """
    ).fetchall()
    return {"by_origin": by_origin, "per_item": [dict(r) for r in per_item]}


def stats_usage(conn: sqlite3.Connection) -> dict:
    most_used = conn.execute("SELECT id, ref, title, use_count FROM items WHERE status='active' ORDER BY use_count DESC, id LIMIT 20").fetchall()
    least_used = conn.execute("SELECT id, ref, title, use_count FROM items WHERE status='active' ORDER BY use_count ASC, id LIMIT 20").fetchall()
    return {"most_used": [dict(r) for r in most_used], "least_used": [dict(r) for r in least_used]}


def verify(conn: sqlite3.Connection) -> dict:
    """§1.6, §7 acceptance: dangling refs, unresolvable source_uid,
    drifted file bodies. Drift is 'detected on demand' — the honest version
    of tracking outside edits (BRAIN_FAQ.md §1.6): nothing observes a file
    changing until this runs, and when it does, the drift becomes a
    history row with origin='detected', same as any other edit."""
    dangling_aliases = conn.execute(
        "SELECT a.id, a.item_id, a.alias FROM aliases a LEFT JOIN items i ON i.id = a.item_id WHERE i.id IS NULL"
    ).fetchall()
    dangling_tag_links = conn.execute(
        "SELECT tl.tag_id, tl.item_id FROM tag_links tl LEFT JOIN items i ON i.id = tl.item_id WHERE i.id IS NULL"
    ).fetchall()
    dangling_collection_items = conn.execute(
        "SELECT ci.collection_id, ci.item_id FROM collection_items ci LEFT JOIN items i ON i.id = ci.item_id WHERE i.id IS NULL"
    ).fetchall()
    dangling_links = conn.execute(
        """
        SELECT il.from_id, il.to_id FROM item_links il
        LEFT JOIN items a ON a.id = il.from_id LEFT JOIN items b ON b.id = il.to_id
        WHERE a.id IS NULL OR b.id IS NULL
        """
    ).fetchall()
    dangling_parents = conn.execute(
        "SELECT i.id, i.parent_id FROM items i LEFT JOIN items p ON p.id = i.parent_id WHERE i.parent_id IS NOT NULL AND p.id IS NULL"
    ).fetchall()
    # item_embeddings is a vec0 virtual table (migrations/brain/003_semantic_search.sql)
    # and can't hold a real FK/CASCADE, so a hard delete has to clean it up
    # explicitly (remove_entry does) — this catches anything that slipped
    # through anyway (an older delete from before that cleanup existed) and
    # repairs it on the spot, same spirit as the drift resync below.
    dangling_embeddings = conn.execute(
        "SELECT ie.item_id FROM item_embeddings ie LEFT JOIN items i ON i.id = ie.item_id WHERE i.id IS NULL"
    ).fetchall()
    for row in dangling_embeddings:
        conn.execute("DELETE FROM item_embeddings WHERE item_id = ?", (row["item_id"],))
        conn.execute("DELETE FROM item_embedding_meta WHERE item_id = ?", (row["item_id"],))

    drifted = []
    rows = conn.execute("SELECT id, ref, title, body, body_sha256, source_path FROM items WHERE source_path IS NOT NULL").fetchall()
    for row in rows:
        path = Path(row["source_path"])
        if not path.exists():
            drifted.append({"item_id": row["id"], "title": row["title"], "issue": "source file missing"})
            continue
        current = path.read_text(encoding="utf-8", errors="replace")
        current_hash = sha256_text(current)
        if current_hash != row["body_sha256"]:
            record_history(conn, row["id"], "body", row["body"], current, origin="detected", note=f"drifted from {path}")
            conn.execute("UPDATE items SET body = ?, body_sha256 = ? WHERE id = ?", (current, current_hash, row["id"]))
            drifted.append({"item_id": row["id"], "title": row["title"], "issue": "body drifted, resynced"})
    conn.commit()

    return {
        "dangling_aliases": [dict(r) for r in dangling_aliases],
        "dangling_tag_links": [dict(r) for r in dangling_tag_links],
        "dangling_collection_items": [dict(r) for r in dangling_collection_items],
        "dangling_links": [dict(r) for r in dangling_links],
        "dangling_parents": [dict(r) for r in dangling_parents],
        "dangling_embeddings": [dict(r) for r in dangling_embeddings],
        "drifted": drifted,
    }


_EXPORT_FORMATS = {"json", "markdown", "yaml"}

# Extension used when writing an export to disk (docs/51_SPEC_brain_parity_and_naming.md
# §2.1 `export --tree` row). Kept separate from the `--format` name itself since
# "markdown" the format name and ".md" the extension are conventionally different strings.
EXPORT_EXTENSIONS = {"json": "json", "markdown": "md", "yaml": "yaml"}


def render_rows(rows: list[dict], fmt: str) -> str:
    """Renders a list of item dicts as json/yaml/markdown text — the same
    rendering `export_items` uses for the whole result set, factored out so
    `export --tree` can call it once per matched item to produce one file
    each, without duplicating the format-specific logic."""
    if fmt not in _EXPORT_FORMATS:
        raise ValueError(f"--format must be one of json, markdown, yaml, got {fmt!r}")

    if fmt == "json":
        return json.dumps(rows, indent=2)
    if fmt == "yaml":
        import yaml

        return yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)

    parts = []
    for r in rows:
        parts.append(f"# {r['title']}\n\n**kind:** {r['kind']}  \n**why:** {r['why']}\n\n{r['body'] or ''}\n")
    return "\n---\n\n".join(parts)


def export_items(conn: sqlite3.Connection, fmt: str = "json", collection: str | None = None, include_sensitive: bool = False) -> str:
    """§7 acceptance: excludes sensitivity != 'normal' unless
    --include-sensitive."""
    if fmt not in _EXPORT_FORMATS:
        raise ValueError(f"--format must be one of json, markdown, yaml, got {fmt!r}")

    sql = "SELECT * FROM items WHERE status = 'active' AND excluded = 0"
    params: list = []
    if not include_sensitive:
        sql += " AND sensitivity = 'normal'"
    if collection is not None:
        from chatvault.brain.core import resolve_collection

        coll = resolve_collection(conn, collection)
        sql += " AND id IN (SELECT item_id FROM collection_items WHERE collection_id = ?)"
        params.append(coll["id"])
    sql += " ORDER BY kind, id"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return render_rows(rows, fmt)


def get_items_for_tree(conn: sqlite3.Connection, tree: str, include_sensitive: bool = False) -> list[sqlite3.Row]:
    """Every (item, matching tag) pair for a prefix/glob like 'tech/**' — one
    row per match, not one per item, mirroring `chatvault.export`'s
    `get_conversations_for_tree` (docs/51_SPEC_brain_parity_and_naming.md
    §2.1 `export --tree` row: "reuses the same logic"). An item carrying two
    tags under the same tree lands under both when the tree is laid out on
    disk. Each row carries an extra `matched_tag` column alongside the
    normal item columns, and is filtered the same way `export_items` filters
    its own result set (active status, sensitivity unless --include-sensitive)."""
    from chatvault.tags import tag_match

    frag, params = tag_match("t.name", tree)
    sql = f"""
        SELECT i.*, t.name AS matched_tag
        FROM items i
        JOIN tag_links tl ON tl.item_id = i.id
        JOIN tags t ON t.id = tl.tag_id
        WHERE i.status = 'active' AND i.excluded = 0 AND {frag}
    """  # noqa: S608
    if not include_sensitive:
        sql += " AND i.sensitivity = 'normal'"
    sql += " ORDER BY t.name, i.id"
    return conn.execute(sql, params).fetchall()

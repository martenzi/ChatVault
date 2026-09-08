from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, resolve_item


def add_tag(conn: sqlite3.Connection, name: str, description: str | None = None) -> dict:
    existing = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        return {"tag_id": existing["id"], "action": "already_exists"}
    cur = conn.execute("INSERT INTO tags (name) VALUES (?)", (name,))
    conn.commit()
    return {"tag_id": cur.lastrowid, "action": "created"}


def _get_or_create_tag_id(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT INTO tags (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    return conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]


def tag_entry(conn: sqlite3.Connection, ref, tag_names: list[str]) -> dict:
    item = resolve_item(conn, ref)
    for name in tag_names:
        tag_id = _get_or_create_tag_id(conn, name)
        conn.execute("INSERT INTO tag_links (tag_id, item_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (tag_id, item["id"]))
    conn.commit()
    return {"item_id": item["id"], "tags": tag_names}


def untag_entry(
    conn: sqlite3.Connection,
    ref,
    tag_names: list[str] | None = None,
    source: str | None = None,
    prefix: str | None = None,
) -> dict:
    """Removes tags from one entry. With bare tag names, removes exactly
    those. `source` (docs/51_SPEC_brain_parity_and_naming.md §4.2) narrows
    to (or, with no tag_names, entirely replaces the selection with) tags
    whose tag_link carries that source — e.g. `--source chatvault` reverses
    a whole `copy conversation`/`copy stashed` tag carryover in one command,
    the same convention ChatVault's own `tag untag --source rule|llm` uses.
    `prefix` narrows to tag names starting with it (e.g. `vault/`)."""
    item = resolve_item(conn, ref)
    removed: list[str] = []

    if tag_names:
        for name in tag_names:
            row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            if row is None:
                continue
            clauses = ["tag_id = ?", "item_id = ?"]
            params: list = [row["id"], item["id"]]
            if source is not None:
                clauses.append("source = ?")
                params.append(source)
            conn.execute(f"DELETE FROM tag_links WHERE {' AND '.join(clauses)}", params)  # noqa: S608
            removed.append(name)
    elif source is not None or prefix is not None:
        clauses = ["tl.item_id = ?"]
        params = [item["id"]]
        if source is not None:
            clauses.append("tl.source = ?")
            params.append(source)
        if prefix is not None:
            clauses.append("t.name LIKE ?")
            params.append(f"{prefix}%")
        matching = conn.execute(
            f"SELECT tl.tag_id, t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE {' AND '.join(clauses)}",  # noqa: S608
            params,
        ).fetchall()
        for row in matching:
            conn.execute("DELETE FROM tag_links WHERE tag_id = ? AND item_id = ?", (row["tag_id"], item["id"]))
            removed.append(row["name"])
    else:
        raise BrainError("pass one or more tag names, --source, or --prefix")

    conn.commit()
    return {"item_id": item["id"], "removed": removed}


def list_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.id, t.name, COUNT(tl.item_id) AS item_count
        FROM tags t LEFT JOIN tag_links tl ON tl.tag_id = t.id
        GROUP BY t.id ORDER BY t.name
        """
    ).fetchall()


def tree_tags(conn: sqlite3.Connection) -> list[dict]:
    """Tags are hierarchical by naming convention alone (slash-delimited,
    same as the vault's own tags — chatvault/tags.py's tag_match doc
    comment). No parent_id needed: 'a/b/c' implies 'a' and 'a/b'."""
    rows = list_tags(conn)
    return [{"name": r["name"], "depth": r["name"].count("/"), "item_count": r["item_count"]} for r in rows]


def rename_tag(conn: sqlite3.Connection, old: str, new: str) -> dict:
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (old,)).fetchone()
    if row is None:
        raise BrainError(f"no tag named {old!r}")
    if conn.execute("SELECT id FROM tags WHERE name = ?", (new,)).fetchone() is not None:
        raise BrainError(f"tag {new!r} already exists — use `merge tags` instead")
    conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new, row["id"]))
    conn.commit()
    return {"tag_id": row["id"], "old": old, "new": new}


def merge_tags(conn: sqlite3.Connection, a: str, b: str) -> dict:
    """Merges tag `a` into tag `b`: every item tagged `a` becomes tagged
    `b` (if not already), then `a` is removed."""
    row_a = conn.execute("SELECT id FROM tags WHERE name = ?", (a,)).fetchone()
    row_b = conn.execute("SELECT id FROM tags WHERE name = ?", (b,)).fetchone()
    if row_a is None:
        raise BrainError(f"no tag named {a!r}")
    if row_b is None:
        raise BrainError(f"no tag named {b!r}")
    item_ids = [r["item_id"] for r in conn.execute("SELECT item_id FROM tag_links WHERE tag_id = ?", (row_a["id"],))]
    for item_id in item_ids:
        conn.execute("INSERT INTO tag_links (tag_id, item_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (row_b["id"], item_id))
    conn.execute("DELETE FROM tags WHERE id = ?", (row_a["id"],))
    conn.commit()
    return {"merged": a, "into": b, "item_count": len(item_ids)}

from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, now

# `brain tag rule` (docs/51_SPEC_brain_parity_and_naming.md §2.1 `tag rule`
# row) — auto-tagging on `add entry`/`accept`, mirroring ChatVault's own
# `tag rule add/list/remove/apply` (chatvault/tags.py's rule_add & friends)
# in command shape. Adapted to the schema migrations/brain/005_brain_parity.sql
# actually created: `pattern`/`field` (a plain case-insensitive substring
# match against one of an item's own text fields) rather than ChatVault's
# `where_sql`/`min_hits` (an arbitrary SQL predicate over its `blocks`
# table) — Brain items have no `blocks` table to run a SQL predicate
# against, so the rule shape here is the simplest thing that can match
# "does this item's title/body/why contain X" without inventing a new
# schema. Reversible via `untag entry --source rule` (chatvault/brain/tags.py
# already supports `--source`, from earlier parity work) because every
# rule-applied tag_link is stamped with source='rule'.

_FIELDS = ("title", "body", "why")


def add_rule(conn: sqlite3.Connection, pattern: str, tag: str, field: str = "title") -> dict:
    if field not in _FIELDS:
        raise BrainError(f"--field must be one of {', '.join(_FIELDS)}, got {field!r}")
    if not pattern or not pattern.strip():
        raise BrainError("pattern must not be empty")
    if not tag or not tag.strip():
        raise BrainError("tag must not be empty")
    cur = conn.execute(
        "INSERT INTO tag_rules (pattern, tag, field, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(pattern, tag, field) DO NOTHING",
        (pattern.strip(), tag.strip(), field, now()),
    )
    conn.commit()
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT id FROM tag_rules WHERE pattern = ? AND tag = ? AND field = ?", (pattern.strip(), tag.strip(), field)
        ).fetchone()
        return {"rule_id": row["id"], "action": "already_exists"}
    return {"rule_id": cur.lastrowid, "action": "created"}


def list_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tag_rules ORDER BY id").fetchall()


def remove_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM tag_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def _get_or_create_tag(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT INTO tags (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    return conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]


def apply_rules_to_item(conn: sqlite3.Connection, item_id: int) -> list[str]:
    """Evaluates every stored rule against one item (called automatically
    from `add_entry`/`accept` — §2.1's "Apply on `add entry` / `accept`").
    Idempotent via tag_links' (tag_id, item_id) unique constraint. Returns
    the tag names newly applied."""
    item = conn.execute("SELECT title, body, why FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return []
    applied: list[str] = []
    for rule in list_rules(conn):
        haystack = (item[rule["field"]] or "").lower()
        if rule["pattern"].lower() not in haystack:
            continue
        tag_id = _get_or_create_tag(conn, rule["tag"])
        cur = conn.execute(
            "INSERT INTO tag_links (tag_id, item_id, source) VALUES (?, ?, 'rule') ON CONFLICT(tag_id, item_id) DO NOTHING",
            (tag_id, item_id),
        )
        if cur.rowcount:
            applied.append(rule["tag"])
    if applied:
        conn.commit()
    return applied


def apply_rules_all(conn: sqlite3.Connection) -> int:
    """Backfill: (re-)evaluate every rule against every active item, not
    just new ones — mirrors ChatVault's `tag rule apply`."""
    item_ids = [r[0] for r in conn.execute("SELECT id FROM items WHERE status = 'active'").fetchall()]
    total = 0
    for item_id in item_ids:
        total += len(apply_rules_to_item(conn, item_id))
    return total

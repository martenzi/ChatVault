from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone


class BrainError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def known_kinds(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM kinds")}


def known_rels(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM rel_types")}


def record_history(
    conn: sqlite3.Connection,
    item_id: int,
    field: str,
    old_value,
    new_value,
    origin: str = "cli",
    note: str | None = None,
) -> None:
    """One row per changed field (docs/45_SPEC_brain_v2_full.md §1.6) — the
    history_bumps_edit_count trigger (migrations/brain/002_brain_v2.sql)
    increments items.edit_count and updated_at for every row inserted here,
    so edit_count is always len(history for this item), never tracked
    separately and never able to drift from it."""
    conn.execute(
        "INSERT INTO history (item_id, changed_at, field, old_value, new_value, origin, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, now(), field, None if old_value is None else str(old_value), None if new_value is None else str(new_value), origin, note),
    )


def _row_number_lookup(conn: sqlite3.Connection, n: int) -> sqlite3.Row | None:
    """Row `n` of the full, unfiltered, active entry list — the same 'n
    means row n of a list run fresh right now' contract stash/brain already
    use elsewhere in this codebase."""
    rows = conn.execute("SELECT * FROM items WHERE status = 'active' ORDER BY created_at DESC, id DESC").fetchall()
    if 1 <= n <= len(rows):
        return rows[n - 1]
    return None


def resolve_item(conn: sqlite3.Connection, ref) -> sqlite3.Row:
    """Resolves any `<ref>` argument for an entry-taking command
    (acceptance criterion: 'every command taking <ref> also accepts a row
    number and an alias'). Tried in order: items.ref exact match, then
    aliases.alias (case-insensitive), then — only when the whole string is
    digits — row number from the full active list. ref/alias take priority
    over row number so a numeric-looking ref never gets shadowed by a
    coincidentally-matching row position."""
    key = str(ref).strip()
    row = conn.execute("SELECT * FROM items WHERE ref = ?", (key,)).fetchone()
    if row is not None:
        return row
    row = conn.execute(
        "SELECT i.* FROM items i JOIN aliases a ON a.item_id = i.id WHERE a.alias = ? COLLATE NOCASE",
        (key,),
    ).fetchone()
    if row is not None:
        return row
    if key.isdigit():
        row = _row_number_lookup(conn, int(key))
        if row is not None:
            return row
    raise BrainError(f"no entry matches ref, alias, or row number {ref!r}")


def get_item_by_id(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    """Direct id lookup — for internal callers (promote, import) that
    already have the numeric primary key and shouldn't go through
    resolve_item's ref/alias/row-number guessing."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise BrainError(f"no item with id {item_id}")
    return row


def resolve_ref_or_id(conn: sqlite3.Connection, value) -> sqlite3.Row:
    """For `--parent <ref|id>` specifically (§3) — deliberately narrower
    than resolve_item: a raw digit string here means the item's actual
    primary key, not its position in `list entries`, because the spec's own
    option annotation says `<ref|id>`, not `<ref|alias|row>`."""
    key = str(value).strip()
    row = conn.execute("SELECT * FROM items WHERE ref = ?", (key,)).fetchone()
    if row is not None:
        return row
    if key.isdigit():
        row = conn.execute("SELECT * FROM items WHERE id = ?", (int(key),)).fetchone()
        if row is not None:
            return row
    raise BrainError(f"no entry matches ref or id {value!r}")


def resolve_collection(conn: sqlite3.Connection, ref) -> sqlite3.Row:
    key = str(ref).strip()
    row = conn.execute("SELECT * FROM collections WHERE ref = ? OR name = ?", (key, key)).fetchone()
    if row is not None:
        return row
    if key.isdigit():
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (int(key),)).fetchone()
        if row is not None:
            return row
    raise BrainError(f"no collection matches ref/name/id {ref!r}")

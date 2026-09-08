from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, known_kinds, now

# Quick capture (docs/46_AUDIT_second_brain_landscape.md §5) — `brain add
# entry` requires --why, which is right for curation and wrong for the
# moment of noticing something. `jot` relocates the why-discipline rather
# than weakening it: capture is instant (no --why), status='inbox' keeps
# it out of `list entries`/`find`/`export` until you deliberately `accept`
# it (which does require --why) or `reject` it. status is an existing
# unconstrained TEXT column — no migration needed for a third lifecycle
# state alongside active/archived.

_TITLE_MAX = 60


def _title_from_text(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "(empty jot)"
    if len(first_line) > _TITLE_MAX:
        return first_line[: _TITLE_MAX - 1].rstrip() + "…"
    return first_line


def jot(conn: sqlite3.Connection, text: str) -> dict:
    if text is None or not text.strip():
        raise BrainError("nothing to jot — pass some text")
    text = text.strip()
    ts = now()
    cur = conn.execute(
        """
        INSERT INTO items (kind, title, why, body, source_kind, status, created_at)
        VALUES ('note', ?, '', ?, 'manual', 'inbox', ?)
        """,
        (_title_from_text(text), text, ts),
    )
    conn.commit()
    return {"item_id": cur.lastrowid, "title": _title_from_text(text)}


def jot_file(conn: sqlite3.Connection, path) -> dict:
    """The 'drop a file into a folder' capture path (46_AUDIT §5), as an
    explicit one-shot command rather than a background watcher daemon —
    this codebase has no daemon infrastructure, and a CLI sweep of a folder
    gets the same "near-zero friction" result without adding one."""
    text = path.read_text(encoding="utf-8", errors="replace")
    result = jot(conn, text)
    conn.execute("UPDATE items SET title = ? WHERE id = ?", (path.stem, result["item_id"]))
    conn.commit()
    return {**result, "title": path.stem, "path": str(path)}


def list_inbox(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Fresh every call, newest first — same 'n means row n of a list run
    right now' contract every other brain list uses."""
    return conn.execute("SELECT * FROM items WHERE status = 'inbox' ORDER BY created_at DESC, id DESC").fetchall()


def _resolve_inbox_row(conn: sqlite3.Connection, n) -> sqlite3.Row:
    rows = list_inbox(conn)
    key = str(n).strip()
    if key.isdigit():
        idx = int(key)
        if 1 <= idx <= len(rows):
            return rows[idx - 1]
    raise BrainError(f"row {n} not in the inbox (1-{len(rows)})")


def accept(
    conn: sqlite3.Connection,
    n,
    why: str,
    kind: str | None = None,
    ref: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Promotes an inbox item into a real, curated Brain entry — the
    deliberate step the audit insists stays separate from capture. --why is
    mandatory here, same rule as `add entry`, just deferred to this point
    instead of blocking the jot itself."""
    if why is None or not why.strip():
        raise BrainError("--why is required and must not be empty")
    row = _resolve_inbox_row(conn, n)
    if kind is not None and kind not in known_kinds(conn):
        raise BrainError(f"kind must be one of {sorted(known_kinds(conn))}, got {kind!r}")
    if ref is not None and conn.execute("SELECT id FROM items WHERE ref = ?", (ref,)).fetchone():
        raise BrainError(f"ref {ref!r} is already used by another item")

    conn.execute(
        "UPDATE items SET status = 'active', why = ?, kind = COALESCE(?, kind), ref = COALESCE(?, ref), updated_at = ? WHERE id = ?",
        (why.strip(), kind, ref, now(), row["id"]),
    )
    if tags:
        for name in tags:
            conn.execute("INSERT INTO tags (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
            conn.execute("INSERT INTO tag_links (tag_id, item_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (tag_id, row["id"]))
    conn.commit()

    # docs/51_SPEC_brain_parity_and_naming.md §2.1 `tag rule` row: apply on
    # `add entry` *and* `accept` — an inbox item only really "arrives" in
    # the Brain proper at accept time.
    from chatvault.brain.tag_rules import apply_rules_to_item

    apply_rules_to_item(conn, row["id"])

    return {"item_id": row["id"], "title": row["title"]}


def reject(conn: sqlite3.Connection, n) -> dict:
    """Discards an inbox item outright — a narrower, lower-stakes hard
    delete than `remove entry --confirm`: nothing was curated yet, so there
    is nothing a --confirm gate is protecting. Only ever touches
    status='inbox' rows; refuses on anything already promoted."""
    row = _resolve_inbox_row(conn, n)
    conn.execute("DELETE FROM items WHERE id = ? AND status = 'inbox'", (row["id"],))
    conn.commit()
    return {"item_id": row["id"], "title": row["title"]}

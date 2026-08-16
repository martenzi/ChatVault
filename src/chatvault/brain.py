from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from chatvault import export as export_mod
from chatvault import stash as stash_mod
from chatvault import tags as tags_mod

# Second Brain, Layer 2 (docs/42_SPEC_brain_db.md). This round: schema
# (brain_db.py), promote (+ --from-stash), add, list, show, search. Not
# built yet, deliberately: --extract, relate, supersede, confidence
# workflows (spec §7 step 4), and the Datasette/MCP/launcher interfaces
# (step 5).

KINDS = {"conversation", "fact", "method", "decision", "note", "link", "snippet"}
PROMOTED_TAG = "stash/promoted"

# stash's kind vocabulary (method/decision/reference/thread) isn't the same
# set as the brain's (conversation/fact/method/decision/note/link/snippet) —
# method and decision line up directly; reference and thread don't exist on
# the brain side, so --from-stash maps them onto the closest fit rather than
# refusing every stashed item that used them.
_STASH_KIND_MAP = {"reference": "fact", "thread": "conversation"}


class BrainError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_meta(conv_row: sqlite3.Row) -> str:
    return json.dumps(
        {
            "platform": conv_row["platform"],
            "project": conv_row["project"],
            "started_at": conv_row["started_at"],
            "model_primary": conv_row["model_primary"],
            "message_count": conv_row["message_count"],
        }
    )


def _existing_promoted_item(brain_conn: sqlite3.Connection, source_uid: str) -> sqlite3.Row | None:
    return brain_conn.execute(
        "SELECT * FROM items WHERE source_uid = ? AND status = 'active' ORDER BY id LIMIT 1",
        (source_uid,),
    ).fetchone()


def _mark_promoted(vault_conn_rw: sqlite3.Connection, conv_id: int) -> None:
    tag_id = tags_mod.get_or_create_tag(vault_conn_rw, PROMOTED_TAG)
    vault_conn_rw.execute(
        """
        INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
        VALUES (?, 'conversation', ?, 'brain', ?)
        ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
        """,
        (tag_id, conv_id, _now()),
    )
    vault_conn_rw.commit()


def promote(
    brain_conn: sqlite3.Connection,
    vault_conn_ro: sqlite3.Connection,
    vault_conn_rw: sqlite3.Connection | None,
    uid: str,
    why: str,
    kind: str = "conversation",
) -> dict:
    """Reads the conversation from chatvault.sqlite (via a read-only
    connection — spec §4 step 1), renders it with the existing `lean`
    profile (step 2), and stores the result as a frozen `items.body` in
    brain.sqlite along with source_uid/source_meta (step 3). If
    `vault_conn_rw` is given, also marks the vault conversation
    stash/promoted (step 4) so `--from-stash` doesn't offer it twice.

    Idempotent (spec §8): promoting the same source_uid twice returns the
    existing item instead of creating a second one — checked before any
    read of the vault, so a repeat call is cheap and makes no writes at
    all, vault or brain."""
    if why is None or not why.strip():
        raise BrainError("--why is required and must not be empty")
    if kind not in KINDS:
        raise BrainError(f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    why = why.strip()

    existing = _existing_promoted_item(brain_conn, uid)
    if existing is not None:
        return {"item_id": existing["id"], "action": "already_promoted", "title": existing["title"]}

    row = vault_conn_ro.execute("SELECT * FROM v_conversations WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        raise BrainError(f"no conversation with uid {uid!r} in the vault")

    body = export_mod.render_profile(vault_conn_ro, row, profile="lean")
    now = _now()
    cur = brain_conn.execute(
        """
        INSERT INTO items (kind, title, why, body, source_kind, source_uid, source_meta, status, created_at)
        VALUES (?, ?, ?, ?, 'chatvault', ?, ?, 'active', ?)
        """,
        (kind, row["title"] or uid, why, body, uid, _source_meta(row), now),
    )
    brain_conn.commit()
    item_id = cur.lastrowid

    if vault_conn_rw is not None:
        _mark_promoted(vault_conn_rw, row["id"])

    return {"item_id": item_id, "action": "promoted", "title": row["title"] or uid}


def promote_from_stash(
    brain_conn: sqlite3.Connection, vault_conn_ro: sqlite3.Connection, vault_conn_rw: sqlite3.Connection
) -> list[dict]:
    """The batching path (spec §4): promotes every stash/-tagged
    conversation not already stash/promoted, one item per candidate. `why`
    comes from the stash entry's own annotation, never re-typed — Part A
    already made it mandatory there, so every candidate reaching this
    function already has one."""
    results = []
    for entry in stash_mod.list_all(vault_conn_ro):
        already = vault_conn_ro.execute(
            """
            SELECT 1 FROM tag_links tl JOIN tags t ON t.id = tl.tag_id
            WHERE tl.target_type = 'conversation' AND tl.target_id = ? AND t.name = ?
            """,
            (entry.conv_id, PROMOTED_TAG),
        ).fetchone()
        if already:
            continue
        kind = _STASH_KIND_MAP.get(entry.kind, entry.kind) if entry.kind else "conversation"
        result = promote(brain_conn, vault_conn_ro, vault_conn_rw, entry.uid, entry.why, kind=kind)
        results.append(result)
    return results


def add(brain_conn: sqlite3.Connection, title: str, why: str, kind: str = "note", body: str | None = None) -> dict:
    """Manual item creation — no vault involved at all (spec §4:
    `brain add "title" --why "…"`). `why` is mandatory here too, same rule
    as promote (spec §8)."""
    if why is None or not why.strip():
        raise BrainError("--why is required and must not be empty")
    if title is None or not title.strip():
        raise BrainError("title is required and must not be empty")
    if kind not in KINDS:
        raise BrainError(f"kind must be one of {sorted(KINDS)}, got {kind!r}")

    now = _now()
    cur = brain_conn.execute(
        "INSERT INTO items (kind, title, why, body, source_kind, status, created_at) VALUES (?, ?, ?, ?, 'manual', 'active', ?)",
        (kind, title.strip(), why.strip(), body, now),
    )
    brain_conn.commit()
    return {"item_id": cur.lastrowid, "title": title.strip()}


@dataclass
class BrainEntry:
    n: int
    item_id: int
    kind: str
    title: str
    why: str
    confidence: str | None
    status: str
    source_uid: str | None
    created_at: str


def _tag_matches(name: str, query: str) -> bool:
    for suffix in ("/**", "/*"):
        if query.endswith(suffix):
            prefix = query[: -len(suffix)]
            return name == prefix or name.startswith(prefix + "/")
    return name == query


def list_all(
    brain_conn: sqlite3.Connection,
    kind: str | None = None,
    tag: str | None = None,
    confidence: str | None = None,
) -> list[BrainEntry]:
    """The full brain, newest first. `n` is this list's own 1-based
    ordinal — recomputed fresh every call, same principle as stash.list_all:
    `brain show <n>`/(a future `brain edit <n>`) can never go stale."""
    rows = brain_conn.execute("SELECT * FROM v_items ORDER BY created_at DESC, id DESC").fetchall()
    entries = [
        BrainEntry(
            n=i,
            item_id=r["id"],
            kind=r["kind"],
            title=r["title"],
            why=r["why"],
            confidence=r["confidence"],
            status=r["status"],
            source_uid=r["source_uid"],
            created_at=r["created_at"],
        )
        for i, r in enumerate(rows, start=1)
    ]

    if kind is not None:
        entries = [e for e in entries if e.kind == kind]
    if confidence is not None:
        entries = [e for e in entries if e.confidence == confidence]
    if tag is not None:
        tag_rows = brain_conn.execute(
            "SELECT t.name, tl.item_id FROM tag_links tl JOIN tags t ON t.id = tl.tag_id"
        ).fetchall()
        tagged_ids = {r["item_id"] for r in tag_rows if _tag_matches(r["name"], tag)}
        entries = [e for e in entries if e.item_id in tagged_ids]
    return entries


def get_entry(brain_conn: sqlite3.Connection, n: int) -> BrainEntry:
    """Row n of the full, unfiltered brain list — same contract as
    stash.get_entry: n always means 'row n of `brain list` run fresh right
    now', regardless of what filtered view was last printed."""
    entries = list_all(brain_conn)
    for e in entries:
        if e.n == n:
            return e
    raise BrainError(f"row {n} not in the brain (1-{len(entries)})")


def get_item_row(brain_conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = brain_conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise BrainError(f"no item with id {item_id}")
    return row


def search(brain_conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    return brain_conn.execute(
        """
        SELECT items.*, bm25(items_fts) AS rank
        FROM items_fts JOIN items ON items.id = items_fts.rowid
        WHERE items_fts MATCH ? AND items.status = 'active'
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()

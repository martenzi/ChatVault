from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from chatvault import hashing
from chatvault import tags as tags_mod

# Second Brain v0, Part A (docs/40_SPEC_second_brain_v0.md §2).
# No new table: a reserved tag namespace + one annotation per stash, same
# tables ChatVault already has. Deliberately minimal — Parts C/D/E (a real
# stash_items table, the `brain` screen, --summarise) are out of scope until
# the stash habit itself is proven over two weeks of real use (spec §7).

ROOT_TAG = "stash/"
KINDS = {"method", "decision", "reference", "thread"}

# Every stash "why" annotation is stored with this exact prefix so it can be
# told apart from any other annotation a user might put on the same
# conversation via the pre-existing `chatvault note` command (curation.py) —
# annotations has no `kind`/`source` column to distinguish purpose, and the
# spec explicitly rules out a new table for Part A, so a literal marker on
# the note text is the only footprint-free way to keep "why I stashed this"
# unambiguous. Stripped back off before ever being shown to the user.
_WHY_PREFIX = "[stash/why] "


class StashError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conv_row(conn: sqlite3.Connection, conv_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM v_conversations WHERE id = ?", (conv_id,)).fetchone()
    if row is None:
        raise StashError(f"no conversation with id {conv_id}")
    return row


def add(conn: sqlite3.Connection, conv_id: int, why: str, kind: str | None = None) -> dict:
    """Tags `conv_id` stash/ (+ stash/<kind> if given) and records `why` as
    an annotation. Idempotent: re-adding the same why to an already-stashed
    conversation is a no-op on the annotation (dedup via note_hash, the same
    mechanism curation.py's note_add already uses) but still ensures the
    tags exist. Refuses an empty why — a stash entry without a reason is a
    bookmark, and bookmarks rot (spec §2)."""
    if why is None or not why.strip():
        raise StashError("--why is required and must not be empty")
    if kind is not None and kind not in KINDS:
        raise StashError(f"--kind must be one of {sorted(KINDS)}, got {kind!r}")

    _conv_row(conn, conv_id)  # raises if conv_id doesn't resolve
    why = why.strip()
    now = _now()

    root_tag_id = tags_mod.get_or_create_tag(conn, ROOT_TAG)
    conn.execute(
        """
        INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
        VALUES (?, 'conversation', ?, 'stash', ?)
        ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
        """,
        (root_tag_id, conv_id, now),
    )
    if kind:
        kind_tag_id = tags_mod.get_or_create_tag(conn, f"{ROOT_TAG}{kind}")
        conn.execute(
            """
            INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
            VALUES (?, 'conversation', ?, 'stash', ?)
            ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
            """,
            (kind_tag_id, conv_id, now),
        )

    note_text = _WHY_PREFIX + why
    note_hash = hashing.note_hash(note_text)
    exists = conn.execute(
        "SELECT 1 FROM annotations WHERE target_type = 'conversation' AND target_id = ? AND note_hash = ?",
        (conv_id, note_hash),
    ).fetchone()
    if exists is None:
        conn.execute(
            "INSERT INTO annotations (target_type, target_id, note, note_hash, created_at) VALUES ('conversation', ?, ?, ?, ?)",
            (conv_id, note_text, note_hash, now),
        )
    conn.commit()
    return {"conv_id": conv_id, "kind": kind, "why": why, "saved_at": now}


def get_why(conn: sqlite3.Connection, conv_id: int) -> str | None:
    """Most recent stash why for a conversation, or None if it isn't
    stashed. Annotations are additive-only project-wide (never overwritten
    in place — see curation_io.py), so a conversation can accumulate more
    than one stash why over time (e.g. re-stashed with a better reason);
    the latest one is the one that matters."""
    row = conn.execute(
        """
        SELECT note, created_at FROM annotations
        WHERE target_type = 'conversation' AND target_id = ? AND note LIKE ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (conv_id, _WHY_PREFIX + "%"),
    ).fetchone()
    if row is None:
        return None
    return row["note"][len(_WHY_PREFIX):]


def get_kind(conn: sqlite3.Connection, conv_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT t.name FROM tags t
        JOIN tag_links tl ON tl.tag_id = t.id
        WHERE tl.target_type = 'conversation' AND tl.target_id = ?
          AND t.name LIKE ? AND t.name != ?
        ORDER BY t.name LIMIT 1
        """,
        (conv_id, ROOT_TAG + "%", ROOT_TAG),
    ).fetchone()
    if row is None:
        return None
    return row["name"][len(ROOT_TAG):]


@dataclass
class StashEntry:
    n: int
    conv_id: int
    saved_at: str
    kind: str | None
    why: str
    platform: str
    started_at: str | None
    title: str | None
    uid: str


def list_all(conn: sqlite3.Connection, kind: str | None = None, since: str | None = None) -> list[StashEntry]:
    """The full stash, newest first. `n` is this list's own 1-based ordinal
    — deliberately recomputed fresh on every call (not a cache like `find`
    --pick's), so `stash show <n>`/`stash drop <n>` can never go stale: they
    always mean 'row n of `stash list` run right now', full and unfiltered.
    `kind`/`since` filter what's *displayed*; they never change what `n`
    means for an unfiltered call."""
    rows = conn.execute(
        """
        SELECT tl.target_id AS conv_id, tl.created_at AS saved_at
        FROM tag_links tl JOIN tags t ON t.id = tl.tag_id
        WHERE tl.target_type = 'conversation' AND t.name = ?
        ORDER BY tl.created_at DESC, tl.target_id DESC
        """,
        (ROOT_TAG,),
    ).fetchall()

    entries = []
    for i, r in enumerate(rows, start=1):
        conv_id = r["conv_id"]
        conv = conn.execute(
            "SELECT uid, platform, started_at, title FROM v_conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if conv is None:
            continue  # conversation excluded/superseded since being stashed
        entries.append(
            StashEntry(
                n=i,
                conv_id=conv_id,
                saved_at=r["saved_at"],
                kind=get_kind(conn, conv_id),
                why=get_why(conn, conv_id) or "",
                platform=conv["platform"],
                started_at=conv["started_at"],
                title=conv["title"],
                uid=conv["uid"],
            )
        )

    if kind is not None:
        entries = [e for e in entries if e.kind == kind]
    if since is not None:
        entries = [e for e in entries if e.saved_at >= since]
    return entries


def get_entry(conn: sqlite3.Connection, n: int) -> StashEntry:
    """Row n of the full, unfiltered stash list (see list_all)."""
    entries = list_all(conn)
    for e in entries:
        if e.n == n:
            return e
    raise StashError(f"row {n} not in the stash (1-{len(entries)})")


def resolve_pick(conn: sqlite3.Connection, pick: str) -> list[int]:
    """Resolves a `--pick` spec ('1' or '1,4') against the stash's own list
    (see list_all) — deliberately NOT `find`'s cached pick results.

    `find`'s cache invalidates on any database write (content-based, via
    PRAGMA data_version — see browse.resolve_pick), and `stash add` is
    itself a write. The natural sequence `find` -> `stash add --pick N` ->
    later `brief --pick M` would otherwise break on the very next command:
    the add that made an item worth briefing is the same write that
    invalidates the cache `brief` would need to find it again. Resolving
    against the stash's own list sidesteps the problem entirely — it's
    recomputed fresh every call, so it can never go stale."""
    entries_by_n = {e.n: e.conv_id for e in list_all(conn)}
    conv_ids = []
    for part in pick.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise StashError(f"invalid row number: {part!r}")
        n = int(part)
        if n not in entries_by_n:
            raise StashError(f"row {n} not in the stash (1-{len(entries_by_n)})")
        conv_ids.append(entries_by_n[n])
    return conv_ids


def drop(conn: sqlite3.Connection, n: int) -> dict:
    """Removes the stash/ + stash/<kind> tags and every stash-why annotation
    for row n — the conversation row itself is never touched (spec §2: 'the
    conversation itself untouched'; acceptance criterion: `chatvault stats`
    conversation counts identical before/after)."""
    entry = get_entry(conn, n)
    conv_id = entry.conv_id

    tag_ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM tags WHERE name = ? OR name LIKE ?", (ROOT_TAG, ROOT_TAG + "%")
        ).fetchall()
    ]
    removed_tags = 0
    if tag_ids:
        placeholders = ",".join("?" * len(tag_ids))
        cur = conn.execute(
            f"DELETE FROM tag_links WHERE target_type = 'conversation' AND target_id = ? AND tag_id IN ({placeholders})",  # noqa: S608
            (conv_id, *tag_ids),
        )
        removed_tags = cur.rowcount

    cur = conn.execute(
        "DELETE FROM annotations WHERE target_type = 'conversation' AND target_id = ? AND note LIKE ?",
        (conv_id, _WHY_PREFIX + "%"),
    )
    removed_notes = cur.rowcount
    conn.commit()
    return {"conv_id": conv_id, "removed_tags": removed_tags, "removed_notes": removed_notes}

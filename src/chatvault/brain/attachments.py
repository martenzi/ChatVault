from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
from pathlib import Path

from chatvault.brain.core import BrainError, now, resolve_item

# `brain attachments add/list/show` (docs/51_SPEC_brain_parity_and_naming.md
# §2.1 `attachments` row) — the `attachments` table was created by
# migrations/brain/002_brain_v2.sql (§1.8) but nothing ever wrote to it.
# Closes that gap, mirroring ChatVault's own attachments sub-typer's verb
# shape (`attachments add/list/show` here; ChatVault also has
# exclude/restore/redact/purge over its own attachments — out of scope for
# this batch, which only needed to close the "table exists, no CLI" gap).


def add_attachment(conn: sqlite3.Connection, ref, path: Path, store_content: bool = True) -> dict:
    """Attaches a local file to an entry. Reads the file's bytes to record
    sha256/size/mime, and (unless `store_content` is False) embeds small
    text content directly in the row — same "the Brain keeps its own copy"
    principle as `copy conversation`: an attachment should survive the
    original file moving or disappearing, wherever practical."""
    item = resolve_item(conn, ref)
    if not path.exists():
        raise BrainError(f"no file at {path}")
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    mime, _ = mimetypes.guess_type(str(path))
    content = None
    if store_content:
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            content = None  # binary file — keep the path/hash/metadata only
    cur = conn.execute(
        """
        INSERT INTO attachments (item_id, path, sha256, filename, mime, bytes, content, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (item["id"], str(path), sha256, path.name, mime, len(data), content, now()),
    )
    conn.commit()
    return {"attachment_id": cur.lastrowid, "item_id": item["id"], "filename": path.name, "bytes": len(data)}


def list_attachments(conn: sqlite3.Connection, ref) -> list[sqlite3.Row]:
    item = resolve_item(conn, ref)
    return conn.execute(
        "SELECT * FROM attachments WHERE item_id = ? ORDER BY added_at", (item["id"],)
    ).fetchall()


def show_attachment(conn: sqlite3.Connection, attachment_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None:
        raise BrainError(f"no attachment with id {attachment_id}")
    return row


def remove_attachment(conn: sqlite3.Connection, attachment_id: int) -> dict:
    row = show_attachment(conn, attachment_id)
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    conn.commit()
    return {"attachment_id": row["id"], "filename": row["filename"]}

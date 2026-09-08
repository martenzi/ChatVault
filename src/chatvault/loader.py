from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import orjson

from chatvault import hashing


def _json(obj) -> str:
    return orjson.dumps(obj, default=str).decode("utf-8")


@dataclass
class LoadResult:
    action: str  # inserted | noop | superseded | flagged_duplicate | failed
    conv_id: int | None
    uid: str
    detail: str = ""


def _compute_hashes(cir: dict) -> dict:
    """Mutate-free: returns per-message/per-block content hashes plus conversation
    aggregate hashes, without touching the input dict."""
    message_hashes: list[str] = []
    per_message_block_hashes: list[list[str]] = []

    for msg in cir["messages"]:
        block_hashes = [
            hashing.block_hash(b["type"], b.get("tool_name"), b["text"]) for b in msg["blocks"]
        ]
        per_message_block_hashes.append(block_hashes)
        message_hashes.append(hashing.message_hash(msg["role"], block_hashes))

    content_hash = hashing.conversation_content_hash(message_hashes)
    head_hash = hashing.conversation_head_hash(message_hashes)
    return {
        "message_hashes": message_hashes,
        "per_message_block_hashes": per_message_block_hashes,
        "content_hash": content_hash,
        "head_hash": head_hash,
    }


def _model_primary(cir: dict) -> str | None:
    models = [m["model"] for m in cir["messages"] if m.get("model")]
    if not models:
        return None
    return Counter(models).most_common(1)[0][0]


def _get_or_create_attachment(conn: sqlite3.Connection, platform: str, att: dict, now: str) -> int:
    """Dedup by (content_hash, filename) — the same file re-appearing across
    conversations (or in `files` vs `attachments`) gets one row, many links."""
    content = att.get("content") or None
    content_hash = hashing.attachment_content_hash(content)
    filename = att.get("filename")
    row = conn.execute(
        "SELECT id FROM attachments WHERE content_hash = ? AND filename IS ?",
        (content_hash, filename),
    ).fetchone()
    if row is not None:
        return row["id"]

    cur = conn.execute(
        """
        INSERT INTO attachments
            (platform, filename, mime, ext, bytes, content, content_hash, availability, store_path, sensitivity, first_seen_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'normal', ?, ?)
        """,
        (
            platform,
            filename,
            att.get("mime"),
            att.get("ext"),
            att.get("bytes"),
            content,
            content_hash,
            att.get("availability", "text"),
            att.get("store_path"),
            now,
            _json(att.get("raw")),
        ),
    )
    return cur.lastrowid


def _link_attachment(conn: sqlite3.Connection, attachment_id: int, target_type: str, target_id: int, role: str | None, now: str) -> None:
    conn.execute(
        """
        INSERT INTO attachment_links (attachment_id, target_type, target_id, role, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (attachment_id, target_type, target_id) DO NOTHING
        """,
        (attachment_id, target_type, target_id, role, now),
    )


def load_project(conn: sqlite3.Connection, project: dict, source_file_id: int | None) -> dict:
    """Upsert a project row (by platform+native_id uid) and link its docs as
    attachments (target_type='project', role='project_knowledge'). Distinct
    from load_conversation: projects aren't conversations and are addressed
    by their own uid, never content-hashed.

    `kind` was hardcoded to 'claude_project' (formerly 'desktop') here until
    2026-08-12 — fine while Claude Desktop's parse_projects() was the only
    caller, but wrong the moment a second platform-conceptual grouping shows
    up (Grok's workspace_id-keyed "Projects", conceptually a sibling of
    Claude Desktop's, not of an editor's filesystem workspace — see
    grok.py:parse_projects and docs/20_FIX_project_identity.md). Reading
    `kind` from the adapter's dict, defaulting to 'claude_project', keeps
    every existing caller's behavior byte-identical while letting a new one
    opt in. Migration 013 added validity triggers restricting `kind` to
    code_workspace | claude_project | grok_project | no_project."""
    now = datetime.now(timezone.utc).isoformat()
    native_id = project.get("native_id")
    uid = f"{project['platform']}:{native_id}" if native_id else f"{project['platform']}:h:{hashing.attachment_content_hash(project.get('name'))}"
    kind = project.get("kind") or "claude_project"

    existing = conn.execute("SELECT id FROM projects WHERE uid = ?", (uid,)).fetchone()
    if existing is not None:
        project_id = existing["id"]
        conn.execute(
            "UPDATE projects SET name = ?, description = ?, instructions = ?, created_at = ?, raw_json = ?, kind = ? WHERE id = ?",
            (
                project["name"],
                project.get("description"),
                project.get("instructions"),
                project.get("created_at"),
                _json(project.get("raw")),
                kind,
                project_id,
            ),
        )
        action = "updated"
    else:
        cur = conn.execute(
            """
            INSERT INTO projects (uid, platform, native_id, name, description, instructions, created_at, raw_json, kind, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                project["platform"],
                native_id,
                project["name"],
                project.get("description"),
                project.get("instructions"),
                project.get("created_at"),
                _json(project.get("raw")),
                kind,
                now,
            ),
        )
        project_id = cur.lastrowid
        action = "inserted"

    docs = 0
    for doc in project.get("docs", []):
        attachment_id = _get_or_create_attachment(conn, project["platform"], doc, now)
        _link_attachment(conn, attachment_id, "project", project_id, doc.get("role", "project_knowledge"), now)
        docs += 1

    return {"action": action, "project_id": project_id, "docs": docs}


def _backfill_attachments(conn: sqlite3.Connection, conv_id: int, cir: dict) -> int:
    """content_hash is computed from (type, tool_name, text) only — an adapter
    fix that adds attachment *metadata* to blocks whose text/type/tool_name
    are unchanged still lands here as 'identical content already present'
    (correctly: the conversation itself didn't change). Without this, that
    metadata could never reach an already-ingested conversation, since a noop
    never touches messages/blocks. Existing rows are matched positionally by
    (message idx, block idx) — safe because content_hash matching guarantees
    the same sequence of blocks in the same order. Only fills gaps: an
    existing block that already has attachment_id set is left alone."""
    existing_blocks = {
        (r["msg_idx"], r["blk_idx"]): (r["id"], r["msg_id"], r["attachment_id"])
        for r in conn.execute(
            """
            SELECT b.id AS id, b.msg_id AS msg_id, b.attachment_id AS attachment_id, m.idx AS msg_idx, b.idx AS blk_idx
            FROM blocks b JOIN messages m ON m.id = b.msg_id
            WHERE m.conv_id = ?
            """,
            (conv_id,),
        ).fetchall()
    }
    if not existing_blocks:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    for msg in cir["messages"]:
        for block in msg["blocks"]:
            att = block.get("attachment")
            if not att:
                continue
            key = (msg["idx"], block["idx"])
            if key not in existing_blocks:
                continue
            block_id, msg_row_id, current_attachment_id = existing_blocks[key]
            if current_attachment_id is not None:
                continue
            attachment_id = _get_or_create_attachment(conn, cir["platform"], att, now)
            conn.execute("UPDATE blocks SET attachment_id = ? WHERE id = ?", (attachment_id, block_id))
            _link_attachment(conn, attachment_id, "message", msg_row_id, att.get("role", "upload"), now)
            updated += 1

    return updated


def _insert_messages_and_blocks(conn: sqlite3.Connection, conv_id: int, cir: dict, hashes: dict) -> tuple[int, int, int]:
    message_count = 0
    block_count = 0
    char_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for msg, msg_hash, block_hashes in zip(cir["messages"], hashes["message_hashes"], hashes["per_message_block_hashes"]):
        msg_chars = sum(len(b["text"]) for b in msg["blocks"])
        cur = conn.execute(
            """
            INSERT INTO messages (conv_id, idx, role, model, created_at, content_hash, char_count, excluded, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conv_id,
                msg["idx"],
                msg["role"],
                msg.get("model"),
                msg.get("created_at"),
                msg_hash,
                msg_chars,
                int(msg.get("excluded", 0)),
                _json(msg.get("raw")),
            ),
        )
        msg_id = cur.lastrowid
        message_count += 1
        char_count += msg_chars

        for block, b_hash in zip(msg["blocks"], block_hashes):
            attachment_id = None
            att = block.get("attachment")
            if att:
                attachment_id = _get_or_create_attachment(conn, cir["platform"], att, now)
                _link_attachment(conn, attachment_id, "message", msg_id, att.get("role", "upload"), now)

            conn.execute(
                """
                INSERT INTO blocks (msg_id, conv_id, idx, type, tool_name, text, char_count, content_hash, raw_json, attachment_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    conv_id,
                    block["idx"],
                    block["type"],
                    block.get("tool_name"),
                    block["text"],
                    len(block["text"]),
                    b_hash,
                    _json(block.get("raw")),
                    attachment_id,
                ),
            )
            block_count += 1

    return message_count, block_count, char_count


def reprocess_conversation(
    conn: sqlite3.Connection,
    cir: dict,
    source_file_id: int,
    position: int,
) -> LoadResult:
    rows = conn.execute(
        "SELECT id, uid, native_id, content_hash FROM conversations WHERE source_file_id IS ? ORDER BY id",
        (source_file_id,),
    ).fetchall()
    native_id = cir.get("native_id")
    if native_id is not None:
        existing = next((row for row in rows if row["native_id"] == native_id), None)
        if existing is None:
            return load_conversation(conn, cir, source_file_id)
    elif position < len(rows) and rows[position]["native_id"] is None:
        existing = rows[position]
    else:
        return load_conversation(conn, cir, source_file_id)

    conv_id = existing["id"]
    hashes = _compute_hashes(cir)
    if existing["content_hash"] == hashes["content_hash"]:
        backfilled = _backfill_attachments(conn, conv_id, cir)
        detail = "identical content already present"
        if backfilled:
            detail += f"; backfilled attachment metadata on {backfilled} block(s)"
        return LoadResult("noop", conv_id, existing["uid"], detail)

    messages = conn.execute(
        "SELECT id, idx FROM messages WHERE conv_id = ? ORDER BY idx", (conv_id,)
    ).fetchall()
    if [row["idx"] for row in messages] != [msg["idx"] for msg in cir["messages"]]:
        raise ValueError(f"reprocess changed message structure for conversation {existing['uid']}")

    block_count = 0
    char_count = 0
    now = datetime.now(timezone.utc).isoformat()
    for msg, msg_row, msg_hash, block_hashes in zip(
        cir["messages"], messages, hashes["message_hashes"], hashes["per_message_block_hashes"]
    ):
        blocks = conn.execute(
            "SELECT id, idx, attachment_id FROM blocks WHERE msg_id = ? ORDER BY idx", (msg_row["id"],)
        ).fetchall()
        if len(msg["blocks"]) < len(blocks):
            raise ValueError(f"reprocess removed blocks from conversation {existing['uid']}")

        msg_chars = sum(len(block["text"]) for block in msg["blocks"])
        conn.execute(
            """
            UPDATE messages
            SET role = ?, model = ?, created_at = ?, content_hash = ?, char_count = ?, excluded = ?
            WHERE id = ?
            """,
            (
                msg["role"], msg.get("model"), msg.get("created_at"), msg_hash,
                msg_chars, int(msg.get("excluded", 0)), msg_row["id"],
            ),
        )
        char_count += msg_chars

        for block, block_row, block_hash in zip(msg["blocks"], blocks, block_hashes):
            conn.execute(
                """
                UPDATE blocks
                SET type = ?, tool_name = ?, text = ?, char_count = ?, content_hash = ?
                WHERE id = ?
                """,
                (
                    block["type"], block.get("tool_name"), block["text"],
                    len(block["text"]), block_hash, block_row["id"],
                ),
            )
            block_count += 1

        for block, block_hash in zip(msg["blocks"][len(blocks):], block_hashes[len(blocks):]):
            attachment_id = None
            att = block.get("attachment")
            if att:
                attachment_id = _get_or_create_attachment(conn, cir["platform"], att, now)
                _link_attachment(conn, attachment_id, "message", msg_row["id"], att.get("role", "upload"), now)
            conn.execute(
                """
                INSERT INTO blocks
                    (msg_id, conv_id, idx, type, tool_name, text, char_count, content_hash, raw_json, attachment_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_row["id"], conv_id, block["idx"], block["type"], block.get("tool_name"),
                    block["text"], len(block["text"]), block_hash, _json(block.get("raw")), attachment_id,
                ),
            )
            block_count += 1

    conn.execute(
        """
        UPDATE conversations
        SET platform = ?, native_id = ?, title = ?, project = ?, model_primary = ?,
            started_at = ?, ended_at = ?, message_count = ?, block_count = ?, char_count = ?,
            head_hash = ?, content_hash = ?
        WHERE id = ?
        """,
        (
            cir["platform"], cir.get("native_id"), cir.get("title"), cir.get("project"),
            _model_primary(cir), cir.get("started_at"), cir.get("ended_at"), len(messages),
            block_count, char_count, hashes["head_hash"], hashes["content_hash"], conv_id,
        ),
    )
    _backfill_attachments(conn, conv_id, cir)
    return LoadResult("reprocessed", conv_id, existing["uid"], "updated extracted content in place")


def load_conversation(conn: sqlite3.Connection, cir: dict, source_file_id: int) -> LoadResult:
    hashes = _compute_hashes(cir)
    content_hash = hashes["content_hash"]
    head_hash = hashes["head_hash"]
    uid = hashing.make_uid(cir["platform"], cir.get("native_id"), content_hash)
    now = datetime.now(timezone.utc).isoformat()
    message_count = len(cir["messages"])

    existing = conn.execute(
        "SELECT id, uid, content_hash, head_hash, message_count FROM conversations WHERE uid = ?",
        (uid,),
    ).fetchone()

    if existing is None:
        conv_id = _insert_conversation_row(conn, cir, uid, content_hash, head_hash, source_file_id, now, hashes)
        return LoadResult("inserted", conv_id, uid)

    if existing["content_hash"] == content_hash:
        backfilled = _backfill_attachments(conn, existing["id"], cir)
        detail = "identical content already present"
        if backfilled:
            detail += f"; backfilled attachment metadata on {backfilled} block(s)"
        return LoadResult("noop", existing["id"], uid, detail)

    is_continuation = False
    if message_count > existing["message_count"]:
        existing_msg_hashes = [
            row[0]
            for row in conn.execute(
                "SELECT content_hash FROM messages WHERE conv_id = ? ORDER BY idx", (existing["id"],)
            ).fetchall()
        ]
        is_continuation = hashes["message_hashes"][: len(existing_msg_hashes)] == existing_msg_hashes

    if is_continuation:
        renamed = f"{uid}#superseded-{existing['id']}"
        conn.execute("UPDATE conversations SET uid = ? WHERE id = ?", (renamed, existing["id"]))
        conv_id = _insert_conversation_row(conn, cir, uid, content_hash, head_hash, source_file_id, now, hashes)
        conn.execute("UPDATE conversations SET superseded_by = ? WHERE id = ?", (conv_id, existing["id"]))
        return LoadResult("superseded", conv_id, uid, f"superseded conversation id={existing['id']}")

    # Base alt_uid is deterministic (uid + content_hash prefix), which is right
    # for the common case: the same ambiguous duplicate re-ingested later
    # resolves to the same row instead of piling up copies. But three or more
    # *distinct source files* can independently produce byte-identical CIR
    # content that collides with this same uid (seen for real: sandboxed
    # Claude Desktop "local agent mode" sessions can carry duplicate
    # transcript copies) — the second occurrence's alt_uid is a legitimate
    # re-resolve to the same row (handled above via existing/content_hash
    # match on a *prior insert's own uid*), but a third+ file racing the
    # same base alt_uid before that row exists would otherwise hit a UNIQUE
    # constraint on conversations.uid. Disambiguate with an incrementing
    # suffix so ingest never crashes on this — still fully deterministic and
    # idempotent (a re-ingest of the same file set lands on the same uids).
    base_alt_uid = f"{uid}#dup-{content_hash[:8]}"
    alt_uid = base_alt_uid
    suffix = 1
    while conn.execute("SELECT 1 FROM conversations WHERE uid = ?", (alt_uid,)).fetchone() is not None:
        suffix += 1
        alt_uid = f"{base_alt_uid}-{suffix}"
    conv_id = _insert_conversation_row(conn, cir, alt_uid, content_hash, head_hash, source_file_id, now, hashes)
    return LoadResult(
        "flagged_duplicate",
        conv_id,
        alt_uid,
        f"uid collision with conversation id={existing['id']} but neither identical nor a clean continuation; "
        "review with `chatvault dedupe --report`",
    )


def _insert_conversation_row(
    conn: sqlite3.Connection,
    cir: dict,
    uid: str,
    content_hash: str,
    head_hash: str,
    source_file_id: int,
    now: str,
    hashes: dict,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO conversations (
            uid, platform, native_id, title, project, model_primary,
            started_at, ended_at, message_count, block_count, char_count,
            head_hash, content_hash, source_file_id, raw_json, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            cir["platform"],
            cir.get("native_id"),
            cir.get("title"),
            cir.get("project"),
            _model_primary(cir),
            cir.get("started_at"),
            cir.get("ended_at"),
            head_hash,
            content_hash,
            source_file_id,
            _json(cir.get("raw")),
            now,
        ),
    )
    conv_id = cur.lastrowid
    message_count, block_count, char_count = _insert_messages_and_blocks(conn, conv_id, cir, hashes)
    conn.execute(
        "UPDATE conversations SET message_count = ?, block_count = ?, char_count = ? WHERE id = ?",
        (message_count, block_count, char_count, conv_id),
    )
    return conv_id

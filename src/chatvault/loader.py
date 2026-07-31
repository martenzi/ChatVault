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


def _insert_messages_and_blocks(conn: sqlite3.Connection, conv_id: int, cir: dict, hashes: dict) -> tuple[int, int, int]:
    message_count = 0
    block_count = 0
    char_count = 0

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
            conn.execute(
                """
                INSERT INTO blocks (msg_id, conv_id, idx, type, tool_name, text, char_count, content_hash, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            block_count += 1

    return message_count, block_count, char_count


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
        return LoadResult("noop", existing["id"], uid, "identical content already present")

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

    alt_uid = f"{uid}#dup-{content_hash[:8]}"
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

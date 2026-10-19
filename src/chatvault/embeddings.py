from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

# nomic-embed-text via a local Ollama server: chosen over sentence-transformers
# because this machine (Python 3.14 on Intel macOS) has no installable torch
# or onnxruntime wheel at all — a real platform gap in the ML packaging
# ecosystem for this exact combination, not a preference. Ollama sidesteps
# the Python wheel problem entirely (it's a separate local server process,
# already installed on this machine with the model already pulled) while
# satisfying the actual requirement: fully offline, on-device, no per-query
# API cost. nomic-embed-text specifically (not a generic sentence-similarity
# model) is purpose-built and benchmarked for retrieval/semantic-search —
# arguably a better fit for "find this conversation by meaning" than a
# general-purpose small model would be. Tradeoff: 274MB on disk, 768-dim
# vectors (3KB/conversation uncompressed), a few hundred ms per embed call
# on CPU — fine for a one-time backfill over ~1,300 conversations and for
# incremental per-ingest use, not fine for embedding every block of every
# message (which is why this indexes one vector per conversation, not per
# block — see representative_text()).
OLLAMA_URL = "http://localhost:11434/api/embeddings"
MODEL = "nomic-embed-text"
DIM = 768
MAX_CHARS = 3000
_TIMEOUT_S = 30


class EmbeddingUnavailable(RuntimeError):
    """Ollama isn't reachable, or the model isn't pulled. Callers should
    degrade gracefully (skip embedding, keep going) rather than crash an
    ingest or the tagging screen over an optional feature being down."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def embed_text(text: str, model: str = MODEL, base_url: str = OLLAMA_URL) -> list[float]:
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(base_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise EmbeddingUnavailable(
            f"couldn't reach Ollama at {base_url} ({exc}) — "
            f"is `ollama serve` running, and has `{model}` been pulled (`ollama pull {model}`)?"
        ) from exc
    embedding = data.get("embedding")
    if not embedding:
        raise EmbeddingUnavailable(f"Ollama returned no embedding for model {model!r}: {data}")
    return embedding


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def representative_text(conn: sqlite3.Connection, conv_id: int) -> str:
    """Title + the conversation's earliest prose (user/assistant text
    blocks only — no thinking/tool noise) — enough to capture what a
    conversation is *about* without diluting the embedding with a 100-
    message thread's worth of unrelated turns. Capped to keep each embed
    call fast and focused."""
    conv = conn.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    title = (conv["title"] if conv else "") or ""
    rows = conn.execute(
        """
        SELECT b.text FROM messages m
        JOIN blocks b ON b.msg_id = m.id
        JOIN v_conversations c ON c.id = m.conv_id
        WHERE m.conv_id = ? AND m.excluded = 0 AND b.excluded = 0
          AND +b.type = 'text' AND m.role IN ('user', 'assistant')
        ORDER BY m.idx, b.idx
        LIMIT 6
        """,
        (conv_id,),
    ).fetchall()
    parts = [title] + [r["text"] for r in rows if r["text"]]
    return "\n".join(parts).strip()[:MAX_CHARS]


@dataclass
class IndexResult:
    embedded: int = 0
    skipped: int = 0
    failed: int = 0
    unavailable: bool = False


def find_pending(conn: sqlite3.Connection) -> tuple[list[int], int]:
    """(conv_ids to embed, unembeddable count) for live conversations with no
    embedding_meta row — a set-difference, not a per-conversation walk, so
    "nothing to do" costs milliseconds. Only the missing set (typically tens
    of rows) has representative_text computed."""
    missing = conn.execute(
        """
        SELECT v.id FROM v_conversations v
        LEFT JOIN embedding_meta em ON em.conv_id = v.id
        WHERE em.conv_id IS NULL
        """
    ).fetchall()
    pending: list[int] = []
    unembeddable = 0
    for row in missing:
        if representative_text(conn, row["id"]):
            pending.append(row["id"])
        else:
            unembeddable += 1
    return pending, unembeddable


def index_conversations(
    conn: sqlite3.Connection,
    conv_ids: list[int] | None = None,
    model: str = MODEL,
    force: bool = False,
    rescan: bool = False,
    progress: Callable[[int, int], None] | None = None,
    progress_every: int = 25,
) -> IndexResult:
    """Embeds `conv_ids` (default: every live conversation) that either have
    no embedding yet or whose representative text has changed since the last
    embed (by content hash — never by a re-ingest timestamp, same philosophy
    as everywhere else reversibility/staleness is tracked in this codebase).
    Stops and reports `unavailable=True` on the first Ollama-unreachable
    error rather than retrying per-conversation — if it's down, it's down
    for all of them. Commits after every embedded conversation, not just at
    the end — a full-corpus backfill is a slow, interruptible operation
    (roughly a second per conversation against a local Ollama server), and
    losing every already-computed embedding to a Ctrl-C or crash near the
    end would defeat the point of it being resumable at all.

    With no `conv_ids`, only live conversations that have no embedding yet are
    visited (see find_pending). `force` or `rescan` visit every live
    conversation instead; `rescan` re-checks each text hash and re-embeds
    only the ones that changed. `progress(done, total)` is called every
    `progress_every` conversations and at the end."""
    target_ids = conv_ids
    if target_ids is None:
        if force or rescan:
            target_ids = [r[0] for r in conn.execute("SELECT id FROM v_conversations").fetchall()]
        else:
            target_ids = find_pending(conn)[0]

    result = IndexResult()
    total = len(target_ids)
    for n, conv_id in enumerate(target_ids, start=1):
        if progress is not None and n % progress_every == 0:
            progress(n, total)
        text = representative_text(conn, conv_id)
        if not text:
            result.skipped += 1
            continue
        h = _text_hash(text)
        existing = conn.execute(
            "SELECT text_hash FROM embedding_meta WHERE conv_id = ?", (conv_id,)
        ).fetchone()
        if existing is not None and existing["text_hash"] == h and not force:
            result.skipped += 1
            continue

        try:
            vec = embed_text(text, model=model)
        except EmbeddingUnavailable:
            result.unavailable = True
            result.failed += 1
            break

        now = _now()
        conn.execute("DELETE FROM conv_embeddings WHERE conv_id = ?", (conv_id,))
        conn.execute(
            "INSERT INTO conv_embeddings (conv_id, embedding) VALUES (?, ?)", (conv_id, _pack(vec))
        )
        conn.execute(
            """
            INSERT INTO embedding_meta (conv_id, model, text_hash, embedded_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(conv_id) DO UPDATE SET
                model = excluded.model, text_hash = excluded.text_hash, embedded_at = excluded.embedded_at
            """,
            (conv_id, model, h, now),
        )
        conn.commit()
        result.embedded += 1

    conn.commit()
    if progress is not None and total:
        progress(total, total)
    return result


def status(conn: sqlite3.Connection) -> dict:
    """Every number is measured directly, never derived by subtraction —
    `embedded` was previously COUNT(embedding_meta) with no v_conversations
    join, so a supersede left stale rows that inflated it past `total`, and
    `pending = max(total - embedded, 0)` silently clamped that inconsistency
    to zero instead of surfacing it. `missing` conversations (no embedding
    row) split into `pending` (has representative_text, should be embedded)
    and `unembeddable` (empty text, e.g. a title-less zero-block
    conversation — index_conversations() already skips these, so they'd
    never leave `pending` on their own)."""
    total = conn.execute("SELECT COUNT(*) FROM v_conversations").fetchone()[0]

    embedded = conn.execute("SELECT COUNT(*) FROM v_embedding_meta").fetchone()[0]

    pending_ids, unembeddable = find_pending(conn)
    pending = len(pending_ids)

    stale = conn.execute(
        """
        SELECT COUNT(*) FROM embedding_meta em
        JOIN conversations c ON c.id = em.conv_id
        WHERE c.superseded_by IS NOT NULL OR c.excluded = 1
        """
    ).fetchone()[0]

    return {
        "total": total,
        "embedded": embedded,
        "pending": pending,
        "unembeddable": unembeddable,
        "stale": stale,
    }


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    model: str = MODEL,
    return_total: bool = False,
) -> list[sqlite3.Row] | tuple[list[sqlite3.Row], int]:
    """Embeds `query` and returns the nearest conversations by vector
    distance (cosine, via sqlite-vec), ordered nearest-first. Over-fetches
    from the vec0 index before joining against v_conversations, since a
    conversation excluded/superseded after being embedded would otherwise
    silently shrink the result count below `limit`.

    With `return_total=True`, returns `(rows, total)` instead of just
    `rows` — `total` is the count of valid (non-excluded) candidates found
    within the over-fetch window, before the final `limit` slice, mirroring
    `browse.find()`'s `(rows, total)` shape for the CLI's "showing N of M"
    display."""
    vec = embed_text(query, model=model)
    fetch_k = max(limit * 3, 50)
    hits = conn.execute(
        """
        SELECT conv_id, distance FROM conv_embeddings
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance
        """,
        (_pack(vec), fetch_k),
    ).fetchall()
    if not hits:
        return ([], 0) if return_total else []

    ordered_ids = [h["conv_id"] for h in hits]
    placeholders = ",".join("?" * len(ordered_ids))
    conv_rows = conn.execute(
        f"SELECT * FROM v_conversations WHERE id IN ({placeholders})", ordered_ids  # noqa: S608
    ).fetchall()
    by_id = {r["id"]: r for r in conv_rows}
    valid_ids = [cid for cid in ordered_ids if cid in by_id]
    rows = [by_id[cid] for cid in valid_ids][:limit]
    return (rows, len(valid_ids)) if return_total else rows

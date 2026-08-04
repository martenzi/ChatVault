from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import urllib.error
import urllib.request
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
        SELECT b.text FROM v_blocks b
        JOIN v_messages m ON m.id = b.msg_id
        WHERE b.conv_id = ? AND b.type = 'text' AND m.role IN ('user', 'assistant')
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


def index_conversations(
    conn: sqlite3.Connection,
    conv_ids: list[int] | None = None,
    model: str = MODEL,
    force: bool = False,
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
    end would defeat the point of it being resumable at all."""
    target_ids = conv_ids
    if target_ids is None:
        target_ids = [r[0] for r in conn.execute("SELECT id FROM v_conversations").fetchall()]

    result = IndexResult()
    for conv_id in target_ids:
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
    return result


def status(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM v_conversations").fetchone()[0]
    embedded = conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0]
    return {"total": total, "embedded": embedded, "pending": max(total - embedded, 0)}


def semantic_search(
    conn: sqlite3.Connection, query: str, limit: int = 20, model: str = MODEL
) -> list[sqlite3.Row]:
    """Embeds `query` and returns the nearest conversations by vector
    distance (cosine, via sqlite-vec), ordered nearest-first. Over-fetches
    from the vec0 index before joining against v_conversations, since a
    conversation excluded/superseded after being embedded would otherwise
    silently shrink the result count below `limit`."""
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
        return []

    ordered_ids = [h["conv_id"] for h in hits]
    placeholders = ",".join("?" * len(ordered_ids))
    conv_rows = conn.execute(
        f"SELECT * FROM v_conversations WHERE id IN ({placeholders})", ordered_ids  # noqa: S608
    ).fetchall()
    by_id = {r["id"]: r for r in conv_rows}
    return [by_id[cid] for cid in ordered_ids if cid in by_id][:limit]

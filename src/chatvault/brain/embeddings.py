from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass

from chatvault.brain.core import now, sha256_text
from chatvault.embeddings import MODEL, EmbeddingUnavailable, embed_text

# Semantic search for the Brain (docs/46_AUDIT_second_brain_landscape.md
# §4a) — deliberately the same infrastructure ChatVault's own semantic
# search already uses (chatvault/embeddings.py: local Ollama,
# nomic-embed-text, sqlite-vec cosine distance), not a second
# implementation of the same idea. Only representative_text()/status()/
# index_entries()/semantic_find() are Brain-specific, since an item's
# "what is this about" text (title/why/body) is a different shape from a
# conversation's (title + early prose blocks).

MAX_CHARS = 3000


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def representative_text(row: sqlite3.Row) -> str:
    """Title + why + body — everything on an item that carries meaning.
    Unlike a ChatVault conversation (which needs a query to pull in early
    message blocks), an item's meaning already lives entirely on the row
    itself."""
    parts = [row["title"] or "", row["why"] or "", row["body"] or ""]
    return "\n".join(p for p in parts if p).strip()[:MAX_CHARS]


@dataclass
class IndexResult:
    embedded: int = 0
    skipped: int = 0
    failed: int = 0
    unavailable: bool = False


def index_entries(
    conn: sqlite3.Connection,
    item_ids: list[int] | None = None,
    model: str = MODEL,
    force: bool = False,
) -> IndexResult:
    """Embeds `item_ids` (default: every active item) that either have no
    embedding yet or whose representative text changed since the last embed
    (by content hash, same as ChatVault's index_conversations — never by a
    timestamp). Commits after every embedded item, not just at the end, so
    a full-brain backfill is safely interruptible."""
    target_ids = item_ids
    if target_ids is None:
        target_ids = [r[0] for r in conn.execute("SELECT id FROM v_items").fetchall()]

    result = IndexResult()
    for item_id in target_ids:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            continue
        text = representative_text(row)
        if not text:
            result.skipped += 1
            continue
        h = sha256_text(text)
        existing = conn.execute("SELECT text_hash FROM item_embedding_meta WHERE item_id = ?", (item_id,)).fetchone()
        if existing is not None and existing["text_hash"] == h and not force:
            result.skipped += 1
            continue

        try:
            vec = embed_text(text, model=model)
        except EmbeddingUnavailable:
            result.unavailable = True
            result.failed += 1
            break

        ts = now()
        conn.execute("DELETE FROM item_embeddings WHERE item_id = ?", (item_id,))
        conn.execute("INSERT INTO item_embeddings (item_id, embedding) VALUES (?, ?)", (item_id, _pack(vec)))
        conn.execute(
            """
            INSERT INTO item_embedding_meta (item_id, model, text_hash, embedded_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                model = excluded.model, text_hash = excluded.text_hash, embedded_at = excluded.embedded_at
            """,
            (item_id, model, h, ts),
        )
        conn.commit()
        result.embedded += 1

    conn.commit()
    return result


def status(conn: sqlite3.Connection) -> dict:
    """Every number measured directly, never derived by subtraction — same
    discipline as chatvault.embeddings.status(), after that exact shortcut
    caused a real, previously-shipped counting bug there (STATUS.md
    2026-08-06)."""
    total = conn.execute("SELECT COUNT(*) FROM v_items").fetchone()[0]
    embedded = conn.execute("SELECT COUNT(*) FROM v_item_embedding_meta").fetchone()[0]

    missing = conn.execute(
        """
        SELECT v.id FROM v_items v
        LEFT JOIN item_embedding_meta em ON em.item_id = v.id
        WHERE em.item_id IS NULL
        """
    ).fetchall()
    pending = 0
    unembeddable = 0
    for row in missing:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (row["id"],)).fetchone()
        if representative_text(item):
            pending += 1
        else:
            unembeddable += 1

    stale = conn.execute(
        """
        SELECT COUNT(*) FROM item_embedding_meta em
        JOIN items i ON i.id = em.item_id
        WHERE i.status != 'active'
        """
    ).fetchone()[0]

    return {"total": total, "embedded": embedded, "pending": pending, "unembeddable": unembeddable, "stale": stale}


def semantic_find(conn: sqlite3.Connection, query: str, limit: int = 20, model: str = MODEL) -> list[sqlite3.Row]:
    """Embeds `query` and returns the nearest active items by cosine
    distance, nearest-first. Over-fetches from the vec0 index before
    joining against `items`, since an archived item that was embedded while
    still active would otherwise silently shrink the result count below
    `limit` (identical reasoning to chatvault.embeddings.semantic_search)."""
    vec = embed_text(query, model=model)
    fetch_k = max(limit * 3, 50)
    hits = conn.execute(
        "SELECT item_id, distance FROM item_embeddings WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (_pack(vec), fetch_k),
    ).fetchall()
    if not hits:
        return []

    ordered_ids = [h["item_id"] for h in hits]
    placeholders = ",".join("?" * len(ordered_ids))
    rows = conn.execute(
        f"SELECT * FROM items WHERE status = 'active' AND id IN ({placeholders})",  # noqa: S608
        ordered_ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ordered_ids if i in by_id][:limit]

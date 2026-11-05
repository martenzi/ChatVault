from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from chatvault import embeddings as _emb
from chatvault.embeddings import (
    DIM,  # noqa: F401 - re-exported for callers sizing the vec0 column
    MODEL,
    EmbeddingUnavailable,
    _env_int,
    _now,
    _pack,
    _text_hash,
    shrunk_count,
)

# Passage-level semantic search: one vector per substantial text block, built
# beside (never replacing) the per-conversation index in embeddings.py.
#
# What gets a vector: user/assistant `text` blocks of at least MIN_CHARS
# characters — the 53k shorter ones ("ok", "thanks") carry no meaning. Each
# vector is computed over a small window: the anchor block (first
# ANCHOR_CHARS) preceded by the WINDOW-1 text blocks before it in the same
# conversation (last CONTEXT_CHARS each, any length), so a short answer keeps
# the question that prompted it. All of it fits far inside the model's
# 2048-token context, so nothing is silently truncated.
MIN_CHARS = 200
# Window 3 chosen from a measured 1/2/3 comparison (60 conversations, title as
# query, rank by best passage): MRR 0.717 / 0.757 / 0.796, top-5 45 / 52 / 53
# of 60, for +~25% text per vector at 3 vs 2. See docs/reports/.
WINDOW = 3
ANCHOR_CHARS = 1500
CONTEXT_CHARS = 500
SNIPPET_CHARS = 240
# The chunk index is used by default only when it is (nearly) complete:
# otherwise unindexed conversations would silently vanish from results.
READY_MAX_PENDING_FRACTION = 0.01


@dataclass(frozen=True)
class ChunkSettings:
    window: int = WINDOW
    anchor_chars: int = ANCHOR_CHARS
    context_chars: int = CONTEXT_CHARS

    def label(self) -> str:
        return f"chunk;window={self.window};anchor={self.anchor_chars};context={self.context_chars}"


def chunk_settings() -> ChunkSettings:
    return ChunkSettings(
        window=_env_int("CHATVAULT_CHUNK_WINDOW", WINDOW, 1),
        anchor_chars=_env_int("CHATVAULT_CHUNK_ANCHOR_CHARS", ANCHOR_CHARS, 100),
        context_chars=_env_int("CHATVAULT_CHUNK_CONTEXT_CHARS", CONTEXT_CHARS, 50),
    )


@dataclass
class ChunkIndexResult:
    embedded: int = 0
    skipped: int = 0
    conversations: int = 0
    unavailable: bool = False
    error: str = ""
    shrunk: int = 0
    purged: int = 0  # stale passage vectors removed


@dataclass
class Match:
    """Why a conversation appeared: its best-matching passage."""
    distance: float
    block_id: int | None = None
    snippet: str = ""


# Live, user/assistant, non-excluded text blocks of live conversations, in
# reading order. `+b.type` keeps the planner off the type index, same as
# embeddings._TEXT_BLOCKS_SQL.
_CONV_BLOCKS_SQL = """
    SELECT b.id AS block_id, b.text AS text FROM messages m
    JOIN blocks b ON b.msg_id = m.id
    WHERE m.conv_id = ? AND m.excluded = 0 AND b.excluded = 0
      AND +b.type = 'text' AND m.role IN ('user', 'assistant')
      AND b.text IS NOT NULL AND b.text != ''
    ORDER BY m.idx, b.idx
"""

_ELIGIBLE_FROM = """
    FROM blocks b
    JOIN messages m ON m.id = b.msg_id
    JOIN v_conversations c ON c.id = m.conv_id
    WHERE m.excluded = 0 AND b.excluded = 0
      AND +b.type = 'text' AND m.role IN ('user', 'assistant')
      AND length(b.text) >= ?
"""


def _snippet(text: str, query: str, width: int = SNIPPET_CHARS) -> str:
    """The `width`-char stretch of the embedded part of the block that shares
    the most words with the query (start of the block if none do) — the
    vector matched on meaning somewhere in these chars, and this is the
    cheapest honest pointer to where."""
    flat = re.sub(r"\s+", " ", text[: chunk_settings().anchor_chars]).strip()
    if len(flat) <= width:
        return flat
    words = {w for w in re.findall(r"\w+", query.lower()) if len(w) >= 3}
    best_at, best = 0, 0
    for at in [*range(0, len(flat) - width, width // 4), len(flat) - width]:
        window = flat[at: at + width].lower()
        score = sum(1 for w in words if w in window)
        if score > best:
            best_at, best = at, score
    out = flat[best_at: best_at + width].strip()
    return ("…" if best_at else "") + out + "…"


def chunk_texts(blocks: list[tuple[int, str]], settings: ChunkSettings) -> list[tuple[int, str]]:
    """[(block_id, chunk_text)] for every eligible block of one conversation,
    given all its text blocks in reading order."""
    out: list[tuple[int, str]] = []
    for i, (block_id, text) in enumerate(blocks):
        if len(text) < MIN_CHARS:
            continue
        context = [t[-settings.context_chars:] for _, t in blocks[max(0, i - settings.window + 1): i]]
        out.append((block_id, "\n".join(context + [text[: settings.anchor_chars]])))
    return out


def pending_conv_ids(conn: sqlite3.Connection) -> list[int]:
    """Live conversations with at least one eligible block that has no vector."""
    rows = conn.execute(
        f"""
        SELECT DISTINCT m.conv_id {_ELIGIBLE_FROM}
          AND NOT EXISTS (SELECT 1 FROM block_embedding_meta bm WHERE bm.block_id = b.id)
        """,  # noqa: S608
        (MIN_CHARS,),
    ).fetchall()
    return [r[0] for r in rows]


_STALE_FROM = """
    FROM block_embedding_meta bm
    LEFT JOIN blocks b ON b.id = bm.block_id
    LEFT JOIN messages m ON m.id = b.msg_id
    LEFT JOIN v_conversations c ON c.id = m.conv_id
    WHERE b.id IS NULL OR c.id IS NULL OR m.excluded = 1 OR b.excluded = 1
       OR b.type != 'text' OR m.role NOT IN ('user', 'assistant') OR length(b.text) < ?
"""


def stale_block_ids(conn: sqlite3.Connection, conv_ids: list[int] | None = None) -> list[int]:
    """Passage vectors whose block is no longer eligible: the block or its
    message was excluded, the conversation is superseded/excluded, the role or
    type changed, or the text shrank below MIN_CHARS."""
    sql = f"SELECT bm.block_id {_STALE_FROM}"  # noqa: S608
    params: list = [MIN_CHARS]
    if conv_ids is not None:
        if not conv_ids:
            return []
        sql += f" AND bm.conv_id IN ({','.join('?' * len(conv_ids))})"
        params += list(conv_ids)
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def purge_stale(conn: sqlite3.Connection, conv_ids: list[int] | None = None) -> int:
    """Deletes the stale passage vectors (derived data, so deletion is fine —
    `embed --chunks` rebuilds whatever becomes eligible again). Returns how many."""
    ids = stale_block_ids(conn, conv_ids)
    for block_id in ids:
        conn.execute("DELETE FROM block_embeddings WHERE block_id = ?", (block_id,))
        conn.execute("DELETE FROM block_embedding_meta WHERE block_id = ?", (block_id,))
    conn.commit()
    return len(ids)


def status(conn: sqlite3.Connection) -> dict:
    eligible = conn.execute(f"SELECT COUNT(*) {_ELIGIBLE_FROM}", (MIN_CHARS,)).fetchone()[0]  # noqa: S608
    embedded = conn.execute(
        f"SELECT COUNT(*) {_ELIGIBLE_FROM} AND EXISTS (SELECT 1 FROM block_embedding_meta bm WHERE bm.block_id = b.id)",  # noqa: S608
        (MIN_CHARS,),
    ).fetchone()[0]
    total_rows = conn.execute("SELECT COUNT(*) FROM block_embedding_meta").fetchone()[0]
    return {
        "eligible": eligible,
        "embedded": embedded,
        "pending": eligible - embedded,
        "rows": total_rows,
        "stale": len(stale_block_ids(conn)),
        "settings": chunk_settings().label(),
    }


def chunks_ready(conn: sqlite3.Connection) -> bool:
    """Chunk index exists and is (nearly) complete, so search can use it."""
    if not conn.execute("SELECT 1 FROM block_embedding_meta LIMIT 1").fetchone():
        return False
    st = status(conn)
    return st["embedded"] > 0 and st["pending"] <= st["eligible"] * READY_MAX_PENDING_FRACTION


def index_chunks(
    conn: sqlite3.Connection,
    conv_ids: list[int] | None = None,
    model: str = MODEL,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
    progress_every: int = 10,
    retries: int = 0,
    on_wait: Callable[[int, int, float, Exception], None] | None = None,
) -> ChunkIndexResult:
    """Embeds the eligible blocks of `conv_ids` (default: every live
    conversation that has an eligible block without a vector, or all of them
    with `force`). Skips blocks whose chunk text hash is unchanged. Commits
    after every passage, so it is resumable, Ctrl-C loses at most one, and a
    concurrent `embed` on the same DB is never blocked for long. Stops on the first unreachable-Ollama error."""
    settings = chunk_settings()
    label = settings.label()
    scoped = conv_ids is not None
    if conv_ids is None:
        conv_ids = (
            [r[0] for r in conn.execute("SELECT id FROM v_conversations").fetchall()]
            if force else pending_conv_ids(conn)
        )
    result = ChunkIndexResult()
    result.purged = purge_stale(conn, list(conv_ids) if scoped else None)
    shrunk_before = shrunk_count()
    total = len(conv_ids)
    for n, conv_id in enumerate(conv_ids, start=1):
        if progress is not None and n % progress_every == 0:
            progress(n, total)
        blocks = [(r["block_id"], r["text"]) for r in conn.execute(_CONV_BLOCKS_SQL, (conv_id,)).fetchall()]
        touched = False
        for block_id, text in chunk_texts(blocks, settings):
            h = _text_hash(text)
            existing = conn.execute(
                "SELECT text_hash FROM block_embedding_meta WHERE block_id = ?", (block_id,)
            ).fetchone()
            if existing is not None and existing["text_hash"] == h and not force:
                result.skipped += 1
                continue
            try:
                vec = _emb.embed_text_retrying(text, model=model, retries=retries, on_wait=on_wait)
            except EmbeddingUnavailable as exc:
                result.unavailable = True
                result.error = str(exc)
                conn.commit()
                result.shrunk = shrunk_count() - shrunk_before
                return result
            conn.execute("DELETE FROM block_embeddings WHERE block_id = ?", (block_id,))
            conn.execute("INSERT INTO block_embeddings (block_id, embedding) VALUES (?, ?)", (block_id, _pack(vec)))
            conn.execute(
                """
                INSERT INTO block_embedding_meta (block_id, conv_id, model, text_hash, settings, embedded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(block_id) DO UPDATE SET
                    conv_id = excluded.conv_id, model = excluded.model, text_hash = excluded.text_hash,
                    settings = excluded.settings, embedded_at = excluded.embedded_at
                """,
                (block_id, conv_id, model, h, label, _now()),
            )
            conn.commit()  # per passage: never hold a write lock across a slow embed call
            result.embedded += 1
            touched = True
        conn.commit()
        result.conversations += 1 if touched else 0
    result.shrunk = shrunk_count() - shrunk_before
    if progress is not None and total:
        progress(total, total)
    return result


def search(
    conn: sqlite3.Connection, query_vec: list[float], limit: int, query: str = "",
) -> tuple[list[int], dict[int, Match]]:
    """Conversations ranked by their BEST matching chunk (min cosine
    distance = max similarity; never a mean, which would reintroduce the
    dilution this index exists to avoid). Returns (conv_ids nearest-first,
    {conv_id: Match}). Over-fetches because several chunks of one
    conversation can fill the vec0 window, and dead (excluded/superseded)
    conversations are dropped after the join."""
    fetch_k = min(max(limit * 10, 200), 4096)
    hits = conn.execute(
        "SELECT block_id, distance FROM block_embeddings WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (_pack(query_vec), fetch_k),
    ).fetchall()
    if not hits:
        return [], {}
    ids = [h["block_id"] for h in hits]
    marks = ",".join("?" * len(ids))
    live = {
        r["block_id"]: r
        for r in conn.execute(
            f"""
            SELECT bm.block_id, bm.conv_id, b.text FROM block_embedding_meta bm
            JOIN blocks b ON b.id = bm.block_id
            JOIN v_conversations c ON c.id = bm.conv_id
            WHERE bm.block_id IN ({marks}) AND b.excluded = 0
            """,  # noqa: S608
            ids,
        ).fetchall()
    }
    order: list[int] = []
    matches: dict[int, Match] = {}
    for h in hits:  # nearest-first, so the first hit per conversation is its best
        row = live.get(h["block_id"])
        if row is None or row["conv_id"] in matches:
            continue
        matches[row["conv_id"]] = Match(h["distance"], h["block_id"], _snippet(row["text"], query))
        order.append(row["conv_id"])
    return order, matches

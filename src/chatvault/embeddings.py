from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from chatvault import ollama

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
# message (which is why the conversation-level index is one vector per
# conversation — see representative_text(); passage-level search lives in
# block_embeddings, built selectively).
MODEL = "nomic-embed-text"
DIM = 768

# Representation settings — defaults, each overridable by env so a tuning
# experiment is `CHATVAULT_EMBED_MAX_CHARS=5000 chatvault embed --rescan`, not
# a code edit. Recorded per row in embedding_meta.settings.
#
# MAX_CHARS is 6000, not the 8000 the planning doc suggested: Ollama serves
# nomic-embed-text with a 2048-token effective context (measured: 8000 chars
# of prose = exactly 2048 tokens, 12000 and 20000 chars also = 2048, i.e.
# silently cut off at the END — which would discard the very tail this
# representation exists to include). 6000 chars of prose ≈ 1600 tokens leaves
# headroom for denser text; embed_text() still shrinks-from-the-middle if the
# server rejects an input as too long, so head and tail both survive.
MAX_CHARS = 6000
HEAD_BLOCKS = 8
TAIL_BLOCKS = 6
PER_BLOCK_CHARS = 2000
TAIL_RESERVE_PER_BLOCK = 400
# A title is a short label, but some sources store a whole pasted message as
# one (seen: 259,156 chars) — which swallowed the budget and overflowed the
# model. Generous cap, still "the title is never squeezed by the body".
TITLE_MAX_CHARS = 500
_SEPARATOR = "\n[...]\n"
_CONTEXT_SHRINK = 0.8
_MAX_SHRINKS = 8


class EmbeddingUnavailable(RuntimeError):
    """Ollama isn't reachable, or the model isn't pulled. Callers should
    degrade gracefully (skip embedding, keep going) rather than crash an
    ingest or the tagging screen over an optional feature being down.
    `transient` marks errors worth retrying (connection refused/timeout,
    HTTP 5xx such as a model reload or a restarted service); a missing model
    or a bad request will not fix itself."""

    def __init__(self, message: str = "", transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class EmbedSettings:
    max_chars: int = MAX_CHARS
    head_blocks: int = HEAD_BLOCKS
    tail_blocks: int = TAIL_BLOCKS
    per_block_chars: int = PER_BLOCK_CHARS

    def label(self) -> str:
        return (
            f"max={self.max_chars};head={self.head_blocks};"
            f"tail={self.tail_blocks};perblock={self.per_block_chars}"
        )


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None


def embed_settings() -> EmbedSettings:
    return EmbedSettings(
        max_chars=_env_int("CHATVAULT_EMBED_MAX_CHARS", MAX_CHARS, 200),
        head_blocks=_env_int("CHATVAULT_EMBED_HEAD_BLOCKS", HEAD_BLOCKS, 1),
        tail_blocks=_env_int("CHATVAULT_EMBED_TAIL_BLOCKS", TAIL_BLOCKS, 0),
        per_block_chars=_env_int("CHATVAULT_EMBED_PER_BLOCK_CHARS", PER_BLOCK_CHARS, 50),
    )


# Inputs the server rejected as over-context and we re-sent shortened; the
# index run reports it so a too-generous MAX_CHARS is visible, not silent.
_shrunk = 0


def shrunk_count() -> int:
    return _shrunk


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shrink_middle(text: str) -> str:
    """Drop the middle, keep the start (title + opening) and the end (the
    conclusion) — same shape as the representation itself."""
    keep = int(len(text) * _CONTEXT_SHRINK)
    head = keep // 2
    return text[:head] + _SEPARATOR + text[-(keep - head):]


def embed_text(text: str, model: str = MODEL, base_url: str | None = None) -> list[float]:
    """One vector from the configured Ollama server (CHATVAULT_OLLAMA_URL,
    default local). Uses /api/embed with truncate=false so an over-long input
    is an error we handle (shrink the middle) rather than a silent cut of the
    tail; keep_alive keeps the model loaded across a long run. Batching
    several inputs per request was measured and gives no gain (CPU-bound)."""
    global _shrunk
    base = ollama.base_url() if base_url is None else base_url.rstrip("/")
    where = ollama.where(base)
    for attempt in range(_MAX_SHRINKS + 1):
        payload = json.dumps(
            {"model": model, "input": text, "truncate": False, "keep_alive": ollama.keep_alive()}
        ).encode("utf-8")
        req = urllib.request.Request(
            base + "/api/embed", data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=ollama.embed_timeout_s(base)) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 400 and "context length" in body and attempt < _MAX_SHRINKS:
                text = _shrink_middle(text)
                _shrunk += 1
                continue
            hint = f" — has `{model}` been pulled there (`ollama pull {model}`)?" if exc.code == 404 else ""
            raise EmbeddingUnavailable(
                f"Ollama at {where} answered HTTP {exc.code}: {body}{hint}", transient=exc.code >= 500
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise EmbeddingUnavailable(
                f"couldn't reach Ollama at {where} ({exc}) — "
                f"is `ollama serve` running there, and has `{model}` been pulled (`ollama pull {model}`)?",
                transient=True,
            ) from exc
    vectors = data.get("embeddings")
    if not vectors or not vectors[0]:
        raise EmbeddingUnavailable(f"Ollama at {where} returned no embedding for model {model!r}: {data}")
    return vectors[0]


RETRY_WAIT_S = 60
DEFAULT_RETRIES = 10
_sleep = time.sleep


def embed_text_retrying(
    text: str, model: str = MODEL, retries: int = 0, wait_s: float | None = None,
    on_wait: Callable[[int, int, float, Exception], None] | None = None,
) -> list[float]:
    """embed_text, but a *transient* failure (server restarting, model
    reloading, laptop briefly asleep) waits `wait_s` (default 60 s,
    CHATVAULT_EMBED_RETRY_WAIT_S) and tries again, up to `retries` times,
    before raising. Non-transient errors raise immediately. For long
    backfills only — interactive search and ingest keep retries=0 so they
    fail fast."""
    wait = float(os.environ.get("CHATVAULT_EMBED_RETRY_WAIT_S") or RETRY_WAIT_S) if wait_s is None else wait_s
    for attempt in range(retries + 1):
        try:
            return embed_text(text, model=model)
        except EmbeddingUnavailable as exc:
            if not exc.transient or attempt == retries:
                raise
            if on_wait is not None:
                on_wait(attempt + 1, retries, wait, exc)
            _sleep(wait)
    raise AssertionError("unreachable")


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_TEXT_BLOCKS_SQL = """
    SELECT b.text FROM messages m
    JOIN blocks b ON b.msg_id = m.id
    JOIN v_conversations c ON c.id = m.conv_id
    WHERE m.conv_id = ? AND m.excluded = 0 AND b.excluded = 0
      AND +b.type = 'text' AND m.role IN ('user', 'assistant')
      AND b.text IS NOT NULL AND b.text != ''
"""


def _fill(blocks: list[str], budget: int, per_block: int, from_end: bool) -> list[str]:
    """Spend `budget` chars across `blocks` (each clipped to `per_block`),
    taking the final block that doesn't fit *partially* rather than skipping
    it. `from_end` fills last-first so a tight tail budget keeps the
    conclusion; the result is returned in chronological order. Each block
    costs its text plus one separator char."""
    out: list[str] = []
    for text in (reversed(blocks) if from_end else blocks):
        room = budget - (1 if out else 0)
        if room <= 0:
            break
        piece = text[: min(per_block, room)]
        out.append(piece)
        budget -= len(piece) + (1 if len(out) > 1 else 0)
    return out[::-1] if from_end else out


def representative_text(conn: sqlite3.Connection, conv_id: int, settings: EmbedSettings | None = None) -> str:
    """Title + the start and the end of the conversation's prose (user/
    assistant text blocks only — no thinking/tool noise). Long threads drift
    through side topics and land on a decision: the opening says what was
    asked, the ending where it got to, the middle is where a single vector
    gets diluted. So: title (capped at TITLE_MAX_CHARS, never squeezed by the body), then up to HEAD_BLOCKS from
    the start, then up to TAIL_BLOCKS from the end — only blocks the head
    didn't take, so head and tail never overlap (duplicated text would
    distort the vector); a `[...]` line marks the gap when blocks were left out.

    Budget: no block may exceed PER_BLOCK_CHARS (one huge opening can't
    starve the rest); TAIL_BLOCKS*400 chars are reserved for the tail before
    the head is filled (a verbose opening can't starve the ending); head
    leftovers flow to the tail; the last block is partial-filled."""
    s = settings or embed_settings()
    conv = conn.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    title = ((conv["title"] if conv else "") or "").strip()[:TITLE_MAX_CHARS]

    head = [r["text"] for r in conn.execute(
        _TEXT_BLOCKS_SQL + " ORDER BY m.idx, b.idx LIMIT ?", (conv_id, s.head_blocks)).fetchall()]
    tail: list[str] = []
    gap = False  # blocks omitted between head and tail -> show a separator
    if s.tail_blocks and len(head) == s.head_blocks:
        total = conn.execute(f"SELECT COUNT(*) FROM ({_TEXT_BLOCKS_SQL})", (conv_id,)).fetchone()[0]
        # Only blocks the head didn't take: never overlaps, and a conversation
        # of 9..HEAD+TAIL blocks still gets its ending (a bare "skip the tail
        # when total <= HEAD+TAIL" would silently drop those last blocks).
        n_tail = min(s.tail_blocks, total - s.head_blocks)
        if n_tail > 0:
            tail = [r["text"] for r in conn.execute(
                _TEXT_BLOCKS_SQL + " ORDER BY m.idx DESC, b.idx DESC LIMIT ?",
                (conv_id, n_tail)).fetchall()][::-1]
            gap = total - s.head_blocks > n_tail

    remaining = s.max_chars - (len(title) + 1 if title else 0)
    if tail:
        remaining -= len(_SEPARATOR) if gap else 1
    remaining = max(remaining, 0)
    reserve = min(len(tail) * TAIL_RESERVE_PER_BLOCK, remaining // 2) if tail else 0

    head_parts = _fill(head, remaining - reserve, s.per_block_chars, from_end=False)
    spent = sum(len(p) for p in head_parts) + max(len(head_parts) - 1, 0)
    tail_parts = _fill(tail, remaining - spent, s.per_block_chars, from_end=True) if tail else []

    pieces = [title] if title else []
    if head_parts:
        pieces.append("\n".join(head_parts))
    text = "\n".join(pieces)
    if tail_parts:
        text += (_SEPARATOR if gap else "\n") + "\n".join(tail_parts)
    return text.strip()


@dataclass
class IndexResult:
    embedded: int = 0
    skipped: int = 0
    failed: int = 0
    unavailable: bool = False
    error: str = ""  # last EmbeddingUnavailable message, so the CLI can say why it stopped
    shrunk: int = 0  # inputs re-sent shortened after the server said "too long"


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
    retries: int = 0,
    on_wait: Callable[[int, int, float, Exception], None] | None = None,
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
    `progress_every` conversations and at the end. With `retries`, a transient
    Ollama failure waits (60 s by default) and retries that conversation
    before giving up — see embed_text_retrying."""
    target_ids = conv_ids
    if target_ids is None:
        if force or rescan:
            target_ids = [r[0] for r in conn.execute("SELECT id FROM v_conversations").fetchall()]
        else:
            target_ids = find_pending(conn)[0]

    result = IndexResult()
    settings = embed_settings()
    shrunk_before = _shrunk
    total = len(target_ids)
    for n, conv_id in enumerate(target_ids, start=1):
        if progress is not None and n % progress_every == 0:
            progress(n, total)
        text = representative_text(conn, conv_id, settings)
        if not text:
            result.skipped += 1
            continue
        h = _text_hash(text)
        existing = conn.execute(
            "SELECT text_hash, settings FROM embedding_meta WHERE conv_id = ?", (conv_id,)
        ).fetchone()
        if existing is not None and existing["text_hash"] == h and not force:
            # Same text in => same vector out, so nothing to embed; just keep
            # the "which settings produced this" record truthful (rows from
            # before migration 018 carry NULL).
            if existing["settings"] != settings.label():
                conn.execute(
                    "UPDATE embedding_meta SET settings = ? WHERE conv_id = ?", (settings.label(), conv_id)
                )
                conn.commit()
            result.skipped += 1
            continue

        try:
            vec = embed_text_retrying(text, model=model, retries=retries, on_wait=on_wait)
        except EmbeddingUnavailable as exc:
            result.unavailable = True
            result.error = str(exc)
            result.failed += 1
            break

        now = _now()
        conn.execute("DELETE FROM conv_embeddings WHERE conv_id = ?", (conv_id,))
        conn.execute(
            "INSERT INTO conv_embeddings (conv_id, embedding) VALUES (?, ?)", (conv_id, _pack(vec))
        )
        conn.execute(
            """
            INSERT INTO embedding_meta (conv_id, model, text_hash, embedded_at, settings) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(conv_id) DO UPDATE SET
                model = excluded.model, text_hash = excluded.text_hash,
                embedded_at = excluded.embedded_at, settings = excluded.settings
            """,
            (conv_id, model, h, now, settings.label()),
        )
        conn.commit()
        result.embedded += 1

    conn.commit()
    result.shrunk = _shrunk - shrunk_before
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

    current = embed_settings().label()
    other_settings = conn.execute(
        "SELECT COUNT(*) FROM v_embedding_meta WHERE COALESCE(settings, '') != ?", (current,)
    ).fetchone()[0]

    return {
        "settings": current,
        "other_settings": other_settings,
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
    whole_conversation: bool = False,
    matches: dict | None = None,
) -> list[sqlite3.Row] | tuple[list[sqlite3.Row], int]:
    """Embeds `query` and returns the nearest conversations, nearest-first.

    By default, when the passage index (block_embeddings, see
    chunk_embeddings) is built, a conversation is ranked by its BEST-matching
    passage; `whole_conversation=True` — or an absent/incomplete passage
    index — uses the one-vector-per-conversation index (cosine, via
    sqlite-vec) exactly as before. Over-fetches before joining against
    v_conversations, since a conversation excluded/superseded after being
    embedded would otherwise silently shrink the result count below `limit`.

    With `return_total=True`, returns `(rows, total)` instead of just
    `rows` — `total` is the count of valid (non-excluded) candidates found
    within the over-fetch window, before the final `limit` slice, mirroring
    `browse.find()`'s `(rows, total)` shape for the CLI's "showing N of M"
    display. If a dict is passed as `matches`, it is filled with
    `{conv_id: chunk_embeddings.Match}` (distance, and for passage search the
    winning block's snippet) so callers can show why a result appeared."""
    from chatvault import chunk_embeddings

    vec = embed_text(query, model=model)
    if not whole_conversation and chunk_embeddings.chunks_ready(conn):
        ordered_ids, found = chunk_embeddings.search(conn, vec, limit, query)
        if matches is not None:
            matches.update(found)
    else:
        ordered_ids = None
    if ordered_ids is None:
        fetch_k = max(limit * 3, 50)
        hits = conn.execute(
            """
            SELECT conv_id, distance FROM conv_embeddings
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (_pack(vec), fetch_k),
        ).fetchall()
        ordered_ids = [h["conv_id"] for h in hits]
        if matches is not None:
            matches.update({h["conv_id"]: chunk_embeddings.Match(h["distance"]) for h in hits})
    if not ordered_ids:
        return ([], 0) if return_total else []

    placeholders = ",".join("?" * len(ordered_ids))
    conv_rows = conn.execute(
        f"SELECT * FROM v_conversations WHERE id IN ({placeholders})", ordered_ids  # noqa: S608
    ).fetchall()
    by_id = {r["id"]: r for r in conv_rows}
    valid_ids = [cid for cid in ordered_ids if cid in by_id]
    rows = [by_id[cid] for cid in valid_ids][:limit]
    return (rows, len(valid_ids)) if return_total else rows

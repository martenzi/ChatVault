-- ─────────────── semantic search (Spec v1.5 Taxonomy §5 Layer 2) ───────────────
-- One embedding per conversation (title + early prose blocks — see
-- chatvault.embeddings.representative_text), not per block: the goal is
-- "find this conversation by what it's about," not fine-grained passage
-- retrieval. embedding_meta tracks the hash of the text that was actually
-- embedded so re-ingest/backfill only re-embeds conversations that changed.
CREATE TABLE embedding_meta (
  conv_id      INTEGER PRIMARY KEY REFERENCES conversations(id),
  model        TEXT NOT NULL,
  text_hash    TEXT NOT NULL,
  embedded_at  TEXT NOT NULL
);

-- sqlite-vec virtual table. 768 dims = nomic-embed-text's output size (see
-- chatvault/embeddings.py for why that model). distance_metric=cosine is
-- deliberate, not the vec0 default (L2/Euclidean) — verified empirically
-- against real nomic-embed-text output before picking this: embedding norms
-- varied by roughly 15% across real conversation texts of different lengths
-- (~19.5 to ~23), so raw L2 distance would not reliably track meaning-
-- similarity the way cosine does for a model whose vectors aren't
-- guaranteed unit-length. A vec0 table has no ordinary FOREIGN KEY support,
-- so embedding_meta above is the source of truth for "does this
-- conversation have an embedding" and for cascade bookkeeping;
-- conversations are never hard-deleted in this codebase, so an orphaned
-- vec0 row is a purely theoretical concern, not one that comes up.
CREATE VIRTUAL TABLE conv_embeddings USING vec0(
  conv_id INTEGER PRIMARY KEY,
  embedding FLOAT[768] distance_metric=cosine
);

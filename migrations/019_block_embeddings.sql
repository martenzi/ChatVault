-- ─────────────── passage-level semantic search (additive) ───────────────
-- One vector per conversation (conv_embeddings) can only say what a
-- conversation is broadly about. block_embeddings holds one vector per
-- substantial text block (>= 200 chars), embedded together with the block(s)
-- just before it so a short answer keeps the question that prompted it.
-- conv_embeddings is untouched: `search --whole-conversation` still uses it.
--
-- Same pairing as conv_embeddings/embedding_meta: the vec0 table holds the
-- vectors, the plain table is the source of truth for "is this block
-- embedded" and carries conv_id / text_hash / settings.
CREATE TABLE block_embedding_meta (
  block_id     INTEGER PRIMARY KEY REFERENCES blocks(id),
  conv_id      INTEGER NOT NULL REFERENCES conversations(id),
  model        TEXT NOT NULL,
  text_hash    TEXT NOT NULL,
  settings     TEXT,
  embedded_at  TEXT NOT NULL
);
CREATE INDEX ix_bem_conv ON block_embedding_meta(conv_id);

CREATE VIRTUAL TABLE block_embeddings USING vec0(
  block_id INTEGER PRIMARY KEY,
  embedding FLOAT[768] distance_metric=cosine
);

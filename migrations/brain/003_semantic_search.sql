-- Semantic search for the Brain (docs/46_AUDIT_second_brain_landscape.md §4a,
-- §9 item 1) — the exact same pattern ChatVault's own semantic search
-- already uses (migrations/008_embeddings.sql/009_embedding_meta_view.sql),
-- ported rather than reinvented: one embedding per item (not per field),
-- sqlite-vec vec0 table, cosine distance (same empirical reasoning as
-- ChatVault's — nomic-embed-text output isn't unit-length), a *_meta table
-- tracking the hash of the text actually embedded so re-embedding only
-- touches items that changed, and a v_* view so counts always read through
-- the active-only scope (item_status consistency bug class already fixed
-- twice on the ChatVault side — see STATUS.md 2026-08-06/2026-08-12 — never
-- introduced here in the first place).

CREATE TABLE item_embedding_meta (
  item_id      INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  model        TEXT NOT NULL,
  text_hash    TEXT NOT NULL,
  embedded_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE item_embeddings USING vec0(
  item_id INTEGER PRIMARY KEY,
  embedding FLOAT[768] distance_metric=cosine
);

CREATE VIEW v_item_embedding_meta AS
  SELECT em.* FROM item_embedding_meta em
  JOIN v_items i ON i.id = em.item_id;

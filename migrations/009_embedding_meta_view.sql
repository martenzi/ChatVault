-- ─────────────── count-scope consistency fix ───────────────
-- embedding_meta was the one core table missing a v_* view, which is how a
-- raw `COUNT(*) FROM embedding_meta` in embeddings.status() ended up
-- counting stale rows left behind by dedupe.supersede() alongside live
-- ones. Every count shown to a user should read through a v_* view;
-- counts over base tables are reserved for integrity checks that
-- deliberately span all rows (see audit.py).
CREATE VIEW v_embedding_meta AS
  SELECT em.* FROM embedding_meta em
  JOIN v_conversations v ON v.id = em.conv_id;

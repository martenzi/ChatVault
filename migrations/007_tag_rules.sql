-- ─────────────── auto-tagging rules (Spec v1.5 Taxonomy §6, §9 item 5) ───────────────
-- Evaluated against a conversation's own blocks on ingest (see chatvault.tags.apply_rules).
-- where_sql is a predicate over the real `blocks` table (not a view — spec's own
-- example writes `blocks.text LIKE '%docker%'` unaliased), scoped to one
-- conversation's blocks at evaluation time. Always reversible: resulting
-- tag_links carry source='rule', so `chatvault tag untag --source rule` undoes
-- any of it in bulk without touching the rule definitions themselves.
CREATE TABLE tag_rules (
  id          INTEGER PRIMARY KEY,
  tag_name    TEXT NOT NULL,
  where_sql   TEXT NOT NULL,
  min_hits    INTEGER NOT NULL DEFAULT 1,
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL
);
CREATE INDEX ix_tag_rules_enabled ON tag_rules(enabled);

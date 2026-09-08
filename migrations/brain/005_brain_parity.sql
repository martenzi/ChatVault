-- Brain parity, batch A schema (docs/51_SPEC_brain_parity_and_naming.md §3).
--
-- Note: the spec's own text assumed this migration would be "003" — by the
-- time this was written 002/003/004 were all taken (002_brain_v2.sql,
-- 003_semantic_search.sql, 004_tag_link_source.sql), so this is 005. Same
-- situation as 004's own header comment; see docs/51_SPEC §8 Q9 and
-- STATUS.md.

-- Soft exclusion (D3 parity with ChatVault's `exclude`/`restore`) — distinct
-- from `status` (archive/restore lifecycle, "this is done"): `excluded`
-- just hides an item from views without asserting that claim (§8 Q6).
ALTER TABLE items ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0;

-- v_items must gain `AND excluded = 0`. SQLite views don't auto-update when
-- the underlying table gains a column that changes filtering intent, so
-- this is dropped and recreated explicitly rather than assumed — verified
-- in tests/test_brain.py by counting rows before/after with a deliberately
-- excluded row.
DROP VIEW IF EXISTS v_items;
CREATE VIEW v_items AS SELECT * FROM items WHERE status = 'active' AND excluded = 0;

-- Free-form running commentary, distinct from the single mandatory `why`
-- set once at creation, and from `history` (which only records field
-- edits, not standalone remarks).
CREATE TABLE IF NOT EXISTS notes (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  text       TEXT    NOT NULL,
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notes_item ON notes(item_id);

-- Saved queries (`brain view` — the CLI command is a later batch; this
-- table is built now so that later work has somewhere to land).
CREATE TABLE IF NOT EXISTS views (
  id         INTEGER PRIMARY KEY,
  name       TEXT    NOT NULL UNIQUE,
  query      TEXT    NOT NULL,
  description TEXT,
  created_at TEXT    NOT NULL
);

-- Auto-tag rules (mirrors ChatVault's migrations/007_tag_rules.sql in
-- shape, adapted to the spec's own column set here — `pattern`/`field`
-- rather than ChatVault's `where_sql`/`min_hits`, since Brain items don't
-- have a `blocks` table to run a SQL predicate against; a rule instead
-- matches a plain substring/pattern against one of an item's own text
-- fields. `tag rule` the CLI command is a later batch — this table exists
-- now so it has somewhere to land.)
CREATE TABLE IF NOT EXISTS tag_rules (
  id         INTEGER PRIMARY KEY,
  pattern    TEXT    NOT NULL,
  tag        TEXT    NOT NULL,
  field      TEXT    NOT NULL DEFAULT 'title',   -- title | body | why
  created_at TEXT    NOT NULL,
  UNIQUE(pattern, tag, field)
);

-- Provenance ledger (`brain ledger`). Populated by copy conversation/copy
-- stashed/summarise conversation/summarise entry — see
-- chatvault/brain/entries.py and chatvault/brain/distill.py.
ALTER TABLE items ADD COLUMN origin_detail TEXT;  -- e.g. 'copy:chatvault', 'summarise:mistral:latest', 'import:markdown', 'jot'

-- Brain v2 (docs/45_SPEC_brain_v2_full.md). Additive on top of the Layer 2
-- schema (docs/42_SPEC_brain_db.md) that brain_db.py's base SCHEMA_SQL
-- already created: items, item_links, tags, tag_links, items_fts, v_items.
-- Nothing existing is dropped or renamed.
--
-- Two deliberate deviations from the spec's literal SQL, both load-bearing:
--
-- 1. items_fts: the spec's `content=''` (contentless) FTS5 table cannot be
--    kept in sync with plain INSERT/UPDATE/DELETE — FTS5 rejects DML on a
--    contentless table outright, and its special 'delete' command needs the
--    exact old column values, which aren't recoverable for aliases/tags
--    (they live in other tables, not columns of items). A standalone FTS5
--    table (no `content=` at all) stores its own copy of the indexed text
--    and supports plain DML, so triggers can resync it after any change to
--    items, aliases, or tag_links. Functionally identical to the spec's
--    intent (search over title/why/body/ref/aliases/tags); just a table
--    definition that Sqlite's FTS5 implementation actually allows to be
--    trigger-maintained.
--
-- 2. item_links.rel -> rel_types(name): `rel` is an existing column with
--    live rows possible; SQLite can only attach a FK via ADD COLUMN (new
--    columns only), so adding one retroactively means dropping and
--    recreating the table. That's real surgery on a table other tables
--    reference (ON DELETE CASCADE), inside a migration that also flips on
--    kinds/rel_types seed data. Enforcing "rel must be a known rel_types
--    name" at the application layer (chatvault/brain/links.py) gets the
--    same guarantee without that risk.

-- 1.1 Core table -------------------------------------------------------

ALTER TABLE items ADD COLUMN ref          TEXT;
ALTER TABLE items ADD COLUMN parent_id    INTEGER REFERENCES items(id);
ALTER TABLE items ADD COLUMN rank         INTEGER;
ALTER TABLE items ADD COLUMN meta         TEXT;
ALTER TABLE items ADD COLUMN body_format  TEXT NOT NULL DEFAULT 'markdown';
ALTER TABLE items ADD COLUMN sensitivity  TEXT NOT NULL DEFAULT 'normal';
ALTER TABLE items ADD COLUMN vetted_at    TEXT;
ALTER TABLE items ADD COLUMN last_used_at TEXT;
ALTER TABLE items ADD COLUMN use_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE items ADD COLUMN edit_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE items ADD COLUMN body_sha256  TEXT;   -- §1.6 drift detection, paired with the existing source_path

CREATE UNIQUE INDEX IF NOT EXISTS ix_items_ref ON items(ref) WHERE ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_items_parent  ON items(parent_id);
CREATE INDEX IF NOT EXISTS ix_items_rank    ON items(parent_id, rank);
CREATE INDEX IF NOT EXISTS ix_items_sens    ON items(sensitivity);
CREATE INDEX IF NOT EXISTS ix_items_used    ON items(last_used_at);

ALTER TABLE items ADD COLUMN length   TEXT GENERATED ALWAYS AS (json_extract(meta,'$.length'))   VIRTUAL;
ALTER TABLE items ADD COLUMN lang     TEXT GENERATED ALWAYS AS (json_extract(meta,'$.lang'))     VIRTUAL;
ALTER TABLE items ADD COLUMN audience TEXT GENERATED ALWAYS AS (json_extract(meta,'$.audience')) VIRTUAL;
CREATE INDEX IF NOT EXISTS ix_items_length ON items(length);
CREATE INDEX IF NOT EXISTS ix_items_lang   ON items(lang);

-- 1.2 Kinds --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS kinds (
  name        TEXT PRIMARY KEY,
  description TEXT,
  sort_order  INTEGER NOT NULL DEFAULT 100,
  builtin     INTEGER NOT NULL DEFAULT 0
);

INSERT INTO kinds (name, description, sort_order, builtin) VALUES
  ('entity',       'A company, person, tool, place — something claims attach to', 10, 1),
  ('claim',        'A point you make about an entity',                            20, 1),
  ('variant',      'One phrasing of a claim',                                     30, 1),
  ('fact',         'Something established to be true',                            40, 1),
  ('method',       'A way of doing something, repeatable',                        50, 1),
  ('decision',     'A conclusion reached, with what was rejected',                60, 1),
  ('note',         'Free-form thinking',                                          70, 1),
  ('link',         'An external URL worth keeping',                               80, 1),
  ('snippet',      'Code or command worth reusing',                               90, 1),
  ('conversation', 'A promoted ChatVault conversation',                          100, 1),
  ('list',         'A container whose children are the entries',                 110, 1);

-- 1.3 Aliases --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aliases (
  id      INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  alias   TEXT NOT NULL,
  note    TEXT,
  UNIQUE(item_id, alias)
);
CREATE INDEX IF NOT EXISTS ix_aliases_alias ON aliases(alias COLLATE NOCASE);

-- 1.4 Collections ------------------------------------------------------

CREATE TABLE IF NOT EXISTS collections (
  id         INTEGER PRIMARY KEY,
  ref        TEXT UNIQUE,
  name       TEXT NOT NULL,
  why        TEXT NOT NULL,
  meta       TEXT,
  created_at TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS collection_items (
  collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  position      INTEGER NOT NULL,
  note          TEXT,
  PRIMARY KEY (collection_id, item_id)
);

-- 1.5 Links --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rel_types (
  name        TEXT PRIMARY KEY,
  description TEXT,
  symmetric   INTEGER NOT NULL DEFAULT 0,
  inverse     TEXT
);

INSERT INTO rel_types (name, description, symmetric, inverse) VALUES
  ('extends',       'B adds to A',                       0, 'extended_by'),
  ('contradicts',   'B conflicts with A',                1, NULL),
  ('supersedes',    'B replaces A',                      0, 'superseded_by'),
  ('derived_from',  'A was extracted from B',            0, 'source_of'),
  ('see_also',      'related, no direction',             1, NULL),
  ('example_of',    'A is an instance of B',             0, 'has_example'),
  ('depends_on',    'A requires B',                      0, 'required_by'),
  ('variant_of',    'A is another phrasing of B',        0, 'has_variant');

ALTER TABLE item_links ADD COLUMN created_at TEXT;

-- 1.6 History ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS history (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  changed_at TEXT NOT NULL,
  field      TEXT NOT NULL,
  old_value  TEXT,
  new_value  TEXT,
  origin     TEXT NOT NULL DEFAULT 'cli',
  note       TEXT
);
CREATE INDEX IF NOT EXISTS ix_history_item ON history(item_id, changed_at);

CREATE TRIGGER IF NOT EXISTS history_bumps_edit_count AFTER INSERT ON history BEGIN
  UPDATE items SET edit_count = edit_count + 1, updated_at = new.changed_at WHERE id = new.item_id;
END;

-- 1.7 Reviews --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reviews (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  review_at  TEXT NOT NULL,
  reason     TEXT,
  done_at    TEXT,
  outcome    TEXT
);
CREATE INDEX IF NOT EXISTS ix_reviews_due ON reviews(review_at) WHERE done_at IS NULL;

-- 1.8 Attachments ------------------------------------------------------

CREATE TABLE IF NOT EXISTS attachments (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  path       TEXT,
  sha256     TEXT,
  filename   TEXT,
  mime       TEXT,
  bytes      INTEGER,
  content    TEXT,
  added_at   TEXT NOT NULL
);

-- 1.9 Search — see deviation (1) at the top of this file ------------------

DROP TRIGGER IF EXISTS items_ai;
DROP TRIGGER IF EXISTS items_ad;
DROP TRIGGER IF EXISTS items_au;
DROP TABLE IF EXISTS items_fts;

CREATE VIRTUAL TABLE items_fts USING fts5(
  title, why, body, ref, aliases, tags,
  tokenize='porter unicode61'
);

INSERT INTO items_fts (rowid, title, why, body, ref, aliases, tags)
SELECT i.id, i.title, i.why, i.body, i.ref,
       (SELECT group_concat(a.alias, ' ') FROM aliases a WHERE a.item_id = i.id),
       (SELECT group_concat(t.name, ' ') FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = i.id)
FROM items i;

CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts (rowid, title, why, body, ref, aliases, tags)
  VALUES (new.id, new.title, new.why, new.body, new.ref, NULL, NULL);
END;

CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
  DELETE FROM items_fts WHERE rowid = old.id;
END;

CREATE TRIGGER items_au AFTER UPDATE OF title, why, body, ref ON items BEGIN
  UPDATE items_fts SET title = new.title, why = new.why, body = new.body, ref = new.ref WHERE rowid = new.id;
END;

CREATE TRIGGER aliases_ai AFTER INSERT ON aliases BEGIN
  UPDATE items_fts SET aliases = (SELECT group_concat(alias, ' ') FROM aliases WHERE item_id = new.item_id)
  WHERE rowid = new.item_id;
END;

CREATE TRIGGER aliases_ad AFTER DELETE ON aliases BEGIN
  UPDATE items_fts SET aliases = (SELECT group_concat(alias, ' ') FROM aliases WHERE item_id = old.item_id)
  WHERE rowid = old.item_id;
END;

CREATE TRIGGER aliases_au AFTER UPDATE ON aliases BEGIN
  UPDATE items_fts SET aliases = (SELECT group_concat(alias, ' ') FROM aliases WHERE item_id = new.item_id)
  WHERE rowid = new.item_id;
END;

CREATE TRIGGER tag_links_ai AFTER INSERT ON tag_links BEGIN
  UPDATE items_fts SET tags = (SELECT group_concat(t.name, ' ') FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = new.item_id)
  WHERE rowid = new.item_id;
END;

CREATE TRIGGER tag_links_ad AFTER DELETE ON tag_links BEGIN
  UPDATE items_fts SET tags = (SELECT group_concat(t.name, ' ') FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = old.item_id)
  WHERE rowid = old.item_id;
END;

CREATE TRIGGER tags_au AFTER UPDATE OF name ON tags BEGIN
  UPDATE items_fts
  SET tags = (SELECT group_concat(t.name, ' ') FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = items_fts.rowid)
  WHERE rowid IN (SELECT item_id FROM tag_links WHERE tag_id = new.id);
END;

-- 1.10 Views ---------------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_browse AS
SELECT i.id, i.ref, i.kind, i.title,
       substr(i.why,1,60) AS why,
       (SELECT title FROM items p WHERE p.id = i.parent_id) AS parent,
       i.rank, i.length, i.confidence, i.sensitivity,
       (SELECT group_concat(t.name,', ') FROM tag_links tl JOIN tags t ON t.id=tl.tag_id WHERE tl.item_id=i.id) AS tags,
       (SELECT group_concat(a.alias,', ') FROM aliases a WHERE a.item_id=i.id) AS aliases,
       date(i.created_at) AS created, date(i.vetted_at) AS vetted,
       i.use_count, i.edit_count
FROM items i WHERE i.status='active'
ORDER BY i.kind, i.parent_id, i.rank;

CREATE VIEW IF NOT EXISTS v_orphans AS
SELECT i.* FROM v_items i WHERE i.parent_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM tag_links WHERE item_id=i.id)
  AND NOT EXISTS (SELECT 1 FROM collection_items WHERE item_id=i.id);

CREATE VIEW IF NOT EXISTS v_stale AS
SELECT * FROM v_items WHERE use_count = 0 AND created_at < datetime('now','-90 days');

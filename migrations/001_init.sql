-- ─────────────── provenance ───────────────
CREATE TABLE source_files (
  id           INTEGER PRIMARY KEY,
  path         TEXT    NOT NULL,
  sha256       TEXT    NOT NULL UNIQUE,
  platform     TEXT    NOT NULL,
  adapter      TEXT    NOT NULL,
  bytes        INTEGER,
  ingested_at  TEXT    NOT NULL,
  status       TEXT    NOT NULL DEFAULT 'ok',
  error        TEXT
);

-- ─────────────── conversations ───────────────
CREATE TABLE conversations (
  id             INTEGER PRIMARY KEY,
  uid            TEXT    NOT NULL UNIQUE,
  platform       TEXT    NOT NULL,
  native_id      TEXT,
  title          TEXT,
  project        TEXT,
  model_primary  TEXT,
  started_at     TEXT,
  ended_at       TEXT,
  message_count  INTEGER NOT NULL DEFAULT 0,
  block_count    INTEGER NOT NULL DEFAULT 0,
  char_count     INTEGER NOT NULL DEFAULT 0,
  head_hash      TEXT,
  content_hash   TEXT,
  source_file_id INTEGER REFERENCES source_files(id),
  superseded_by  INTEGER REFERENCES conversations(id),
  excluded       INTEGER NOT NULL DEFAULT 0,
  starred        INTEGER NOT NULL DEFAULT 0,
  raw_json       TEXT,
  ingested_at    TEXT    NOT NULL
);
CREATE INDEX ix_conv_platform   ON conversations(platform);
CREATE INDEX ix_conv_started    ON conversations(started_at);
CREATE INDEX ix_conv_model      ON conversations(model_primary);
CREATE INDEX ix_conv_project    ON conversations(project);
CREATE INDEX ix_conv_head_hash  ON conversations(head_hash);
CREATE INDEX ix_conv_content_h  ON conversations(content_hash);

-- ─────────────── messages ───────────────
CREATE TABLE messages (
  id            INTEGER PRIMARY KEY,
  conv_id       INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  idx           INTEGER NOT NULL,
  role          TEXT    NOT NULL,
  model         TEXT,
  created_at    TEXT,
  content_hash  TEXT    NOT NULL,
  char_count    INTEGER NOT NULL DEFAULT 0,
  excluded      INTEGER NOT NULL DEFAULT 0,
  raw_json      TEXT,
  UNIQUE(conv_id, idx)
);
CREATE INDEX ix_msg_conv  ON messages(conv_id);
CREATE INDEX ix_msg_model ON messages(model);
CREATE INDEX ix_msg_role  ON messages(role);
CREATE INDEX ix_msg_hash  ON messages(content_hash);

-- ─────────────── blocks — the atomic unit ───────────────
CREATE TABLE blocks (
  id            INTEGER PRIMARY KEY,
  msg_id        INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  conv_id       INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  idx           INTEGER NOT NULL,
  type          TEXT    NOT NULL,
  tool_name     TEXT,
  text          TEXT    NOT NULL DEFAULT '',
  char_count    INTEGER NOT NULL DEFAULT 0,
  content_hash  TEXT    NOT NULL,
  excluded      INTEGER NOT NULL DEFAULT 0,
  raw_json      TEXT,
  UNIQUE(msg_id, idx)
);
CREATE INDEX ix_blk_msg   ON blocks(msg_id);
CREATE INDEX ix_blk_conv  ON blocks(conv_id);
CREATE INDEX ix_blk_type  ON blocks(type);
CREATE INDEX ix_blk_tool  ON blocks(tool_name);
CREATE INDEX ix_blk_hash  ON blocks(content_hash);

-- ─────────────── your layer: tags, notes, exclusion reasons ───────────────
CREATE TABLE tags (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  colour     TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE tag_links (
  tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  target_type TEXT    NOT NULL,
  target_id   INTEGER NOT NULL,
  source      TEXT    NOT NULL DEFAULT 'manual',
  created_at  TEXT    NOT NULL,
  PRIMARY KEY (tag_id, target_type, target_id)
);
CREATE INDEX ix_taglink_target ON tag_links(target_type, target_id);

CREATE TABLE annotations (
  id          INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id   INTEGER NOT NULL,
  note        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT
);
CREATE INDEX ix_anno_target ON annotations(target_type, target_id);

CREATE TABLE exclusion_log (
  id          INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id   INTEGER NOT NULL,
  action      TEXT NOT NULL,
  reason      TEXT,
  rule        TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE saved_views (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT,
  sql         TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

-- ─────────────── full-text search ───────────────
CREATE VIRTUAL TABLE blocks_fts USING fts5(
  text,
  content='blocks',
  content_rowid='id',
  tokenize='porter unicode61'
);

CREATE TRIGGER blocks_ai AFTER INSERT ON blocks BEGIN
  INSERT INTO blocks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER blocks_ad AFTER DELETE ON blocks BEGIN
  INSERT INTO blocks_fts(blocks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER blocks_au AFTER UPDATE OF text ON blocks BEGIN
  INSERT INTO blocks_fts(blocks_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO blocks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ─────────────── read views (ALWAYS query these, not base tables) ───────────────
CREATE VIEW v_conversations AS
  SELECT * FROM conversations WHERE excluded = 0 AND superseded_by IS NULL;

CREATE VIEW v_messages AS
  SELECT m.* FROM messages m
  JOIN v_conversations c ON c.id = m.conv_id
  WHERE m.excluded = 0;

CREATE VIEW v_blocks AS
  SELECT b.* FROM blocks b
  JOIN v_messages m ON m.id = b.msg_id
  WHERE b.excluded = 0;

CREATE VIEW v_schema_version AS SELECT 1 AS version;

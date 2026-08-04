-- ─────────────── attachments (Spec v1.4 REVISED Part A) ───────────────
-- Content-addressed by (content_hash, filename), text-only for now — no
-- content-addressed blob store. The probe found real sources carry inline
-- extracted text, not binary files; store_path/availability='file' are kept
-- for a future source that does, but nothing builds against that yet.
CREATE TABLE attachments (
  id             INTEGER PRIMARY KEY,
  platform       TEXT NOT NULL,
  filename       TEXT,
  mime           TEXT,
  ext            TEXT,
  bytes          INTEGER,
  content        TEXT,
  content_hash   TEXT NOT NULL,
  availability   TEXT NOT NULL DEFAULT 'text',
      -- text    : we have the text content
      -- file    : we have actual bytes on disk (future)
      -- missing : recorded as having existed, content unavailable
  store_path     TEXT,
  sensitivity    TEXT NOT NULL DEFAULT 'normal',
  first_seen_at  TEXT NOT NULL,
  excluded       INTEGER NOT NULL DEFAULT 0,
  raw_json       TEXT,
  UNIQUE(content_hash, filename)
);

CREATE TABLE attachment_links (
  attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
  target_type   TEXT NOT NULL,   -- 'message' | 'conversation' | 'project'
  target_id     INTEGER NOT NULL,
  role          TEXT,            -- 'upload' | 'artifact' | 'project_knowledge'
  created_at    TEXT NOT NULL,
  PRIMARY KEY (attachment_id, target_type, target_id)
);
CREATE INDEX ix_attlink_target ON attachment_links(target_type, target_id);

ALTER TABLE blocks ADD COLUMN attachment_id INTEGER REFERENCES attachments(id);

CREATE VIRTUAL TABLE attachments_fts USING fts5(
  content, filename,
  content='attachments', content_rowid='id',
  tokenize='porter unicode61'
);

CREATE TRIGGER attachments_ai AFTER INSERT ON attachments BEGIN
  INSERT INTO attachments_fts(rowid, content, filename) VALUES (new.id, new.content, new.filename);
END;
CREATE TRIGGER attachments_ad AFTER DELETE ON attachments BEGIN
  INSERT INTO attachments_fts(attachments_fts, rowid, content, filename) VALUES ('delete', old.id, old.content, old.filename);
END;
CREATE TRIGGER attachments_au AFTER UPDATE OF content, filename ON attachments BEGIN
  INSERT INTO attachments_fts(attachments_fts, rowid, content, filename) VALUES ('delete', old.id, old.content, old.filename);
  INSERT INTO attachments_fts(rowid, content, filename) VALUES (new.id, new.content, new.filename);
END;

CREATE VIEW v_attachments AS SELECT * FROM attachments WHERE excluded = 0;

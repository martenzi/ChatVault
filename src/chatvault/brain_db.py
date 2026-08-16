from __future__ import annotations

import sqlite3

from chatvault.config import Config

# docs/42_SPEC_brain_db.md §3 — verbatim from the spec, plus FTS sync triggers
# following the exact pattern migrations/001_init.sql already uses for
# blocks_fts (insert/delete/update-of-tracked-columns), and a few lookup
# indexes the spec's schema block didn't list but its own query patterns
# (list --kind, list --tag, verify's source_uid lookup) need.
#
# Deliberately no migrations framework, unlike chatvault.sqlite: this is a
# new, small, single-purpose database. `CREATE ... IF NOT EXISTS` on every
# statement makes connect() itself idempotent — schema changes later can
# graduate to real migrations if the brain ever needs them, but there's
# nothing to version yet.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL,
  why           TEXT NOT NULL,
  body          TEXT,
  url           TEXT,

  source_kind   TEXT,
  source_uid    TEXT,
  source_meta   TEXT,
  source_path   TEXT,

  status        TEXT NOT NULL DEFAULT 'active',
  superseded_by INTEGER REFERENCES items(id),
  confidence    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS item_links (
  from_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  to_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  rel      TEXT NOT NULL,
  note     TEXT,
  PRIMARY KEY (from_id, to_id, rel)
);

CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS tag_links (
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  PRIMARY KEY (tag_id, item_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, why, body, content='items', content_rowid='id',
  tokenize='porter unicode61');

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, why, body) VALUES (new.id, new.title, new.why, new.body);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, why, body) VALUES ('delete', old.id, old.title, old.why, old.body);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF title, why, body ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, why, body) VALUES ('delete', old.id, old.title, old.why, old.body);
  INSERT INTO items_fts(rowid, title, why, body) VALUES (new.id, new.title, new.why, new.body);
END;

CREATE VIEW IF NOT EXISTS v_items AS SELECT * FROM items WHERE status = 'active';

CREATE INDEX IF NOT EXISTS ix_items_source_uid ON items(source_uid);
CREATE INDEX IF NOT EXISTS ix_items_kind       ON items(kind);
CREATE INDEX IF NOT EXISTS ix_items_status     ON items(status);
CREATE INDEX IF NOT EXISTS ix_itemlinks_to     ON item_links(to_id);
"""


def connect(config: Config) -> sqlite3.Connection:
    """Opens (creating on first use) brain.sqlite — self-sufficient by
    construction: this never touches chatvault.sqlite, so every `brain`
    command that doesn't explicitly promote from the vault works with the
    vault absent, renamed, or deleted (spec §8's self-sufficiency and
    portability criteria)."""
    path = config.brain_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def connect_vault_readonly(chatvault_db_path) -> sqlite3.Connection:
    """A genuinely read-only connection to chatvault.sqlite (SQLite's own
    `mode=ro` URI flag — any write against this connection raises at the
    SQLite level, not just by convention) for `brain promote` to read the
    conversation to snapshot. Deliberately a plain second connection rather
    than `ATTACH`ing it onto the brain connection: export.render_profile()
    and every v_conversations/v_messages/v_blocks query it calls are
    unqualified (no schema prefix), so ATTACH would require rewriting all of
    export.py to be attach-alias-aware for a purely internal implementation
    choice — the same read-only safety guarantee is available without that,
    via a second ordinary connection opened in mode=ro."""
    uri = f"file:{chatvault_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn

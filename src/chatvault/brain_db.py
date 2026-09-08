from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from chatvault.config import Config

BRAIN_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "brain"

# docs/42_SPEC_brain_db.md §3 — verbatim from the spec, plus FTS sync triggers
# following the exact pattern migrations/001_init.sql already uses for
# blocks_fts (insert/delete/update-of-tracked-columns), and a few lookup
# indexes the spec's schema block didn't list but its own query patterns
# (list --kind, list --tag, verify's source_uid lookup) need.
#
# This base schema is still the original idempotent CREATE-IF-NOT-EXISTS
# bootstrap; everything from migrations/brain/002_brain_v2.sql onward is a
# real, versioned migration (see migrate() below) — the "no migrations
# framework yet" framing this comment used to carry stopped being true once
# 45_SPEC_brain_v2_full.md needed one.
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


def _pending_migrations(conn: sqlite3.Connection) -> list[Path]:
    """Versioned migrations layered on top of the base SCHEMA_SQL above
    (docs/45_SPEC_brain_v2_full.md §6 phase 1 onward). The base schema stays
    the idempotent CREATE-IF-NOT-EXISTS bootstrap it always was — every
    migration from 002 on is additive to it and, unlike the base schema,
    runs exactly once, tracked in its own schema_migrations table (same
    pattern as chatvault.sqlite's, in db.py, but a separate table in a
    separate file — brain.sqlite's versioning is independent of the vault's)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    all_migrations = sorted(BRAIN_MIGRATIONS_DIR.glob("*.sql"))
    return [m for m in all_migrations if m.name not in applied]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Applies pending brain migrations against an already-open connection.
    Returns the filenames applied (empty if already current)."""
    pending = _pending_migrations(conn)
    applied: list[str] = []
    for path in pending:
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (path.name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        applied.append(path.name)
    return applied


def connect(config: Config, auto_migrate: bool = True) -> sqlite3.Connection:
    """Opens (creating on first use) brain.sqlite — self-sufficient by
    construction: this never touches chatvault.sqlite, so every `brain`
    command that doesn't explicitly promote from the vault works with the
    vault absent, renamed, or deleted (spec §8's self-sufficiency and
    portability criteria).

    Applies pending versioned migrations automatically (auto_migrate=True,
    the default): brain.sqlite is single-user, local, and never has
    concurrent writers, so there's no migration race to guard against —
    every `brain` command self-heals to the latest schema the way base
    SCHEMA_SQL already did. Callers that need to control migration timing
    explicitly (backup-then-migrate, tests inspecting pre-migration state)
    pass auto_migrate=False and call migrate() themselves."""
    path = config.brain_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # migrations/brain/003_semantic_search.sql's item_embeddings is a
    # sqlite-vec vec0 virtual table (same reasoning as db.py's identical
    # load for the vault's own conv_embeddings) — loaded unconditionally so
    # every brain command, not just embed/find --semantic, can open a
    # connection without "no such module: vec0".
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    if auto_migrate:
        migrate(conn)
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

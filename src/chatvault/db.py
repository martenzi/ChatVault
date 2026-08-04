from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from chatvault import hashing
from chatvault.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def connect(config: Config) -> sqlite3.Connection:
    config.db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    # Needed for migration 008's `conv_embeddings` vec0 virtual table and any
    # query against it (semantic search) — loaded unconditionally, same as
    # every other extension-backed feature (FTS5) already baked into SQLite
    # itself. If this fails, it fails loudly here rather than as a confusing
    # "no such module: vec0" deep inside a migration or a search call.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _pending_migrations(conn: sqlite3.Connection) -> list[Path]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    all_migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [m for m in all_migrations if m.name not in applied]


def migrate(config: Config) -> list[str]:
    """Apply pending migrations. Backs up the DB file first if it already exists.

    Returns the list of migration filenames that were applied.
    """
    db_existed = config.db_path.exists()
    if db_existed:
        backup = config.db_dir / f"chatvault.sqlite.bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(config.db_path, backup)

    conn = connect(config)
    try:
        pending = _pending_migrations(conn)
        applied: list[str] = []
        for path in pending:
            sql = path.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (path.name, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            applied.append(path.name)

        if applied:
            _backfill_note_hashes(conn)
            _backfill_first_seen_at(conn)

        return applied
    finally:
        conn.close()


def _backfill_note_hashes(conn: sqlite3.Connection) -> None:
    """Migration 002 adds annotations.note_hash but can't populate it in pure
    SQL (no built-in SHA-256). Runs after every migrate() call — a no-op once
    every row has a hash, and a no-op entirely on a database that predates
    migration 002 having ever run (the column won't exist yet's not possible
    here since this only runs after applying pending migrations, so 002 is
    always present by this point if it was ever pending)."""
    try:
        rows = conn.execute("SELECT id, note FROM annotations WHERE note_hash IS NULL").fetchall()
    except sqlite3.OperationalError:
        return  # annotations.note_hash doesn't exist yet (shouldn't happen post-migrate, defensive only)
    for row in rows:
        conn.execute(
            "UPDATE annotations SET note_hash = ? WHERE id = ?",
            (hashing.note_hash(row["note"]), row["id"]),
        )
    if rows:
        conn.commit()


def _backfill_first_seen_at(conn: sqlite3.Connection) -> None:
    """Migration 003 adds source_files.first_seen_at with no way to populate it
    from historical data other than treating the original ingested_at as the
    best available proxy. No-op once every row has a value."""
    try:
        cur = conn.execute(
            "UPDATE source_files SET first_seen_at = ingested_at WHERE first_seen_at IS NULL"
        )
    except sqlite3.OperationalError:
        return  # source_files.first_seen_at doesn't exist yet (shouldn't happen post-migrate, defensive only)
    if cur.rowcount:
        conn.commit()

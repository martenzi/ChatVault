from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from chatvault.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def connect(config: Config) -> sqlite3.Connection:
    config.db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
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
        return applied
    finally:
        conn.close()

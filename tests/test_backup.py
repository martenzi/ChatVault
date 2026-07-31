from __future__ import annotations

import sqlite3

from chatvault import backup, ingest
from chatvault.db import connect
from tests.conftest import FIXTURES


def _counts(c: sqlite3.Connection) -> dict:
    return {
        table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("source_files", "conversations", "messages", "blocks", "tags")
    }


def test_backup_during_open_write_transaction_is_valid(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")

    writer = connect(vault)
    writer.execute("BEGIN")
    writer.execute(
        "INSERT INTO tags (name, created_at) VALUES ('mid-write', '2026-01-01T00:00:00Z')"
    )
    # deliberately not committed — backup must still succeed and produce a valid file

    info = backup.create_backup(vault)

    writer.rollback()
    writer.close()

    import gzip
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        restored = Path(tmp) / "check.sqlite"
        if info.path.suffix == ".zst":
            subprocess.run(["zstd", "-q", "-f", "-d", str(info.path), "-o", str(restored)], check=True)
        else:
            with gzip.open(info.path, "rb") as fh_in, open(restored, "wb") as fh_out:
                fh_out.write(fh_in.read())
        check_conn = sqlite3.connect(restored)
        result = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        check_conn.close()
    assert result == "ok"


def test_restore_round_trips_row_counts(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    before = _counts(conn)
    info = backup.create_backup(vault)
    conn.close()

    # mutate the live DB
    mutator = connect(vault)
    mutator.execute("DELETE FROM conversations")
    mutator.commit()
    mutator.close()

    backup.restore_backup(vault, info.path, force=True)

    restored_conn = connect(vault)
    after = _counts(restored_conn)
    restored_conn.close()
    assert after == before


def test_restore_refuses_without_force(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    info = backup.create_backup(vault)
    raised = False
    try:
        backup.restore_backup(vault, info.path, force=False)
    except FileExistsError:
        raised = True
    assert raised


def test_restore_backs_up_current_db_first(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    info = backup.create_backup(vault)
    conn.close()

    backup.restore_backup(vault, info.path, force=True)

    labels = [b.label for b in backup.list_backups(vault)]
    assert "pre-restore" in labels


def test_prune_keeps_n_most_recent(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    for i in range(5):
        backup.create_backup(vault, label=f"snap-{i}")

    deleted = backup.prune_backups(vault, keep=2)
    assert len(deleted) == 3
    assert len(backup.list_backups(vault)) == 2


def test_zstd_fallback_to_gzip(vault, conn, monkeypatch):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    monkeypatch.setattr("chatvault.backup.shutil.which", lambda name: None)

    info = backup.create_backup(vault, label="no-zstd")
    assert info.path.suffix == ".gz"

    restored = backup.restore_backup(vault, info.path, to=vault.root / "restored.sqlite", force=True)
    restored_conn = sqlite3.connect(restored)
    assert restored_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored_conn.close()

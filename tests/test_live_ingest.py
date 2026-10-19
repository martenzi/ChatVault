from __future__ import annotations

from typer.testing import CliRunner

from chatvault import sources
from chatvault.cli import app


def _add(conn, n, path, root, kind="live", presence="present"):
    conn.execute(
        "INSERT INTO source_files (path, sha256, platform, adapter, ingested_at, kind, root_dir, presence, last_seen_at) "
        "VALUES (?, ?, 'x', 'x', '2026-01-01T00:00:00+00:00', ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (path, f"sha{n}", kind, root, presence),
    )
    conn.commit()


def test_live_roots_collapse_nested_and_skip_non_live(conn):
    _add(conn, 1, "/a/x.jsonl", "/a")
    _add(conn, 2, "/a/sub/y.jsonl", "/a/sub")
    _add(conn, 3, "/b/z.jsonl", "/b")
    _add(conn, 4, "/c/w.jsonl", "/c", kind="frozen")
    _add(conn, 5, "/d/v.jsonl", "/d", presence="retired")
    assert sources.live_roots(conn) == ["/a", "/b"]
    st = sources.live_root_status(conn, "/a")
    assert st.files == 2 and not st.exists


def test_ingest_live_fails_loudly_on_vanished_root(vault, conn, tmp_path, monkeypatch):
    _add(conn, 1, "/nonexistent/x.jsonl", "/nonexistent")
    conn.close()
    monkeypatch.setenv("CHATVAULT_ROOT", str(vault.root))
    result = CliRunner().invoke(app, ["ingest", "--live"])
    assert result.exit_code == 1
    assert "MISSING" in result.output and "/nonexistent" in result.output

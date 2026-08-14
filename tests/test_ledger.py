from __future__ import annotations

from chatvault import ingest, ledger, sources
from tests.conftest import FIXTURES


def test_rows_scoped_through_v_conversations(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    rows = ledger.rows(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["platform"] == "claude_code"
    assert r["kind"] == "frozen"
    assert r["files"] == 1
    assert r["convs"] == 1


def test_server_side_backfill_and_kind_trigger(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "grok" / "prod-grok-backend.json")
    kinds = {r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM source_files WHERE platform = 'grok'")}
    assert kinds == {"server_side"}

    raised = False
    try:
        conn.execute(
            "UPDATE source_files SET kind = 'bogus' WHERE platform = 'grok'"
        )
    except Exception:
        raised = True
    assert raised


def test_mark_kind_accepts_server_side(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    root = str((FIXTURES / "claude_code").resolve())
    n = sources.mark_kind(conn, root, "server_side")
    assert n >= 1
    row = conn.execute("SELECT kind FROM source_files WHERE root_dir = ?", (root,)).fetchone()
    assert row["kind"] == "server_side"


def test_render_markdown_table_has_expected_columns(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    table = ledger.render_markdown_table(ledger.rows(conn))
    assert table.startswith("| Ingested | Platform | Kind | Files | Convs | Source |")
    assert "`claude_code`" in table


def test_regenerate_markdown_replaces_section_1_only(vault, conn, tmp_path):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    doc = tmp_path / "ledger_doc.md"
    doc.write_text(
        "# Ingestion Ledger\n\n"
        "## 1. What's ingested\n\n"
        "stale hand-written table here\n\n"
        "---\n\n"
        "## 2. Some other section\n\n"
        "unrelated content that must survive\n"
    )
    ledger.regenerate_markdown(conn, doc)
    text = doc.read_text()
    assert "stale hand-written table here" not in text
    assert "unrelated content that must survive" in text
    assert "`claude_code`" in text
    assert "## 2. Some other section" in text


def test_regenerate_markdown_raises_without_section_markers(vault, conn, tmp_path):
    doc = tmp_path / "no_sections.md"
    doc.write_text("# Not a ledger doc\n\nno section 1 or 2 here\n")
    raised = False
    try:
        ledger.regenerate_markdown(conn, doc)
    except ValueError:
        raised = True
    assert raised


def test_stale_flags_old_server_side_sources(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "grok" / "prod-grok-backend.json")
    conn.execute(
        "UPDATE source_files SET last_seen_at = '2020-01-01T00:00:00+00:00' WHERE platform = 'grok'"
    )
    conn.commit()
    stale_rows = ledger.stale(conn, days=30)
    assert any(s.platform == "grok" for s in stale_rows)
    assert all(s.days_stale >= 30 for s in stale_rows if s.days_stale is not None)


def test_stale_ignores_recent_server_side_sources(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "grok" / "prod-grok-backend.json")
    stale_rows = ledger.stale(conn, days=30)
    assert stale_rows == []


def test_stale_ignores_non_server_side_kinds(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    conn.execute("UPDATE source_files SET last_seen_at = '2020-01-01T00:00:00+00:00' WHERE platform = 'claude_code'")
    conn.commit()
    stale_rows = ledger.stale(conn, days=30)
    assert stale_rows == []

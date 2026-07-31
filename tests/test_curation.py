from __future__ import annotations

from chatvault import curation, ingest, search
from tests.conftest import FIXTURES


def test_exclude_then_restore_is_byte_equivalent(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    before = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id").fetchall()]

    n = curation.exclude(conn, "message", "role = 'user'", reason="test")
    assert n == 1
    mid_visible = conn.execute("SELECT COUNT(*) FROM v_messages").fetchone()[0]
    assert mid_visible == 1  # only the assistant message remains visible

    curation.restore(conn, "message", "role = 'user'")
    after = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id").fetchall()]
    assert before == after

    log_actions = [r["action"] for r in conn.execute("SELECT action FROM exclusion_log ORDER BY id").fetchall()]
    assert log_actions == ["exclude", "restore"]


def test_tag_add_is_idempotent(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_web" / "conversations.json")
    curation.tag_add(conn, "starred", "1=1", "conversation")
    curation.tag_add(conn, "starred", "1=1", "conversation")
    rows = curation.tag_list(conn)
    assert len(rows) == 1
    assert rows[0]["uses"] == 1


def test_search_respects_exclusion(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_web" / "conversations.json")
    hits = search.search(conn, "graph schema")
    assert len(hits) == 1

    conv = conn.execute("SELECT id FROM conversations").fetchone()
    curation.exclude(conn, "conversation", "1=1", reason="test")
    hits_after = search.search(conn, "graph schema")
    assert hits_after == []

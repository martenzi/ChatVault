from __future__ import annotations

from chatvault import ingest
from tests.conftest import FIXTURES


def _counts(conn):
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("source_files", "conversations", "messages", "blocks")
    }


def test_idempotent_ingest_is_a_noop_on_rerun(conn):
    path = FIXTURES / "claude_code" / "session1.jsonl"

    summary1 = ingest.ingest_path(conn, path)
    assert summary1.totals["inserted"] == 1
    first_counts = _counts(conn)

    summary2 = ingest.ingest_path(conn, path)
    assert summary2.totals == {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
    assert _counts(conn) == first_counts


def test_continuation_supersedes_shorter_version(conn):
    short = FIXTURES / "claude_code" / "session1.jsonl"
    long = FIXTURES / "claude_code" / "session1_continued.jsonl"

    ingest.ingest_path(conn, short)
    summary = ingest.ingest_path(conn, long)

    assert summary.totals["superseded"] == 1

    visible = conn.execute("SELECT uid, message_count FROM v_conversations").fetchall()
    assert len(visible) == 1
    assert visible[0]["message_count"] == 4

    all_rows = conn.execute("SELECT id, superseded_by FROM conversations ORDER BY id").fetchall()
    assert all_rows[0]["superseded_by"] == all_rows[1]["id"]


def test_claude_web_adapter_ingests(conn):
    path = FIXTURES / "claude_web" / "conversations.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1
    row = conn.execute("SELECT platform, message_count FROM conversations").fetchone()
    assert row["platform"] == "claude_web"
    assert row["message_count"] == 2


def test_chatgpt_adapter_linearises_active_branch_and_keeps_abandoned(conn):
    path = FIXTURES / "chatgpt" / "conversations.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT id, message_count FROM conversations").fetchone()
    # active branch = 2 real turns + 1 synthetic "abandoned" message
    assert conv["message_count"] == 3

    excluded = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conv_id = ? AND excluded = 1", (conv["id"],)
    ).fetchone()[0]
    assert excluded == 1

    visible_texts = [
        r[0]
        for r in conn.execute(
            "SELECT text FROM v_blocks WHERE conv_id = ?", (conv["id"],)
        ).fetchall()
    ]
    assert any("hash prefix continuation" in t for t in visible_texts)
    assert not any("abandoned/regenerated" in t for t in visible_texts)


def test_generic_jsonl_fallback_ingests(conn):
    path = FIXTURES / "generic" / "toolsession.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1
    row = conn.execute("SELECT platform, message_count FROM conversations").fetchone()
    assert row["platform"] == "generic"
    assert row["message_count"] == 2

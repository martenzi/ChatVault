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


def test_windsurf_legacy_adapter_maps_full_schema_correctly(conn):
    """Regression test for the windsurf_legacy_full schema (protobuf-wire-full-v1),
    not the thin dialogue-only windsurf_legacy/ export. Covers: per-message model
    recovery, role bucketing for the full Cascade role vocabulary, tool_name/
    tool_args extraction, and the type_N fallback for unmapped msg_types."""
    path = FIXTURES / "windsurf_legacy" / "full_schema_sample.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["platform"] == "windsurf"
    assert conv["native_id"] == "aaaa1111-2222-3333-4444-555566667777"
    assert conv["title"] == "Fix the flaky ingest test"
    assert conv["project"] == "project-x"
    assert conv["message_count"] == 10
    # per-message model IS present in the *_full schema (unlike the thin export)
    assert conv["model_primary"] == "swe-1-6"

    rows = conn.execute(
        "SELECT role, model FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
    ).fetchall()
    roles = [r["role"] for r in rows]
    assert roles == ["system", "system", "user", "assistant", "system", "system", "tool", "tool", "tool", "system"]
    assert rows[2]["model"] == "swe-1-6"  # user message carries its model
    assert rows[3]["model"] == "swe-1-6"  # assistant message carries its model

    blocks = conn.execute(
        "SELECT type, tool_name, text FROM blocks WHERE conv_id = ? ORDER BY id", (conv["id"],)
    ).fetchall()
    block_types = [b["type"] for b in blocks]
    assert block_types == [
        "system",  # memory
        "system",  # system prompt
        "text",  # user
        "text",  # assistant
        "thinking",  # planner
        "attachment",  # context
        "tool_use",  # tool_call
        "tool_result",
        "error",  # tool_error
        "unknown",  # type_99 fallback
    ]
    tool_call_block = blocks[6]
    assert tool_call_block["tool_name"] == "run_tests"
    assert tool_call_block["text"] == "pytest tests/test_ingest.py -q"


def test_cursor_legacy_adapter_bundles_text_and_tool_blocks(conn):
    """One Cursor bubble can carry text AND a tool call at once — both must
    become separate blocks on a single message, not separate messages."""
    path = FIXTURES / "cursor_legacy" / "sample_composer.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["platform"] == "cursor"
    assert conv["native_id"] == "c0mp0ser-1111-2222-3333-444455556666"
    assert conv["title"] == "Fix docker compose networking"
    assert conv["project"] == "project-x"
    assert conv["message_count"] == 3
    assert conv["model_primary"] == "claude-sonnet-4-6"

    tool_msg = conn.execute(
        "SELECT id FROM messages WHERE conv_id = ? AND idx = 1", (conv["id"],)
    ).fetchone()
    blocks = conn.execute(
        "SELECT type, tool_name, text FROM blocks WHERE msg_id = ? ORDER BY idx", (tool_msg["id"],)
    ).fetchall()
    assert [b["type"] for b in blocks] == ["text", "tool_use", "tool_result"]
    assert blocks[1]["tool_name"] == "read_file"
    assert blocks[2]["type"] == "tool_result"


def test_vscode_legacy_adapter_classifies_events_by_role_and_kind(conn):
    path = FIXTURES / "vscode_legacy" / "sample_session.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["platform"] == "vscode"
    assert conv["native_id"] == "vsc0de-1111-2222-3333-444455556666"
    assert conv["title"] == "Rename detective script"
    assert conv["message_count"] == 5

    rows = conn.execute(
        "SELECT m.role, b.type, b.tool_name FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx", (conv["id"],)
    ).fetchall()
    assert [(r["role"], r["type"]) for r in rows] == [
        ("user", "text"),
        ("assistant", "thinking"),
        ("tool", "tool_use"),
        ("assistant", "text"),
        ("system", "unknown"),  # the trailing per-request "result" blob
    ]
    assert rows[2]["tool_name"] == "copilot_renameFile"


def test_cursor_legacy_copilot_adapter_handles_both_subformats(conn):
    session_path = FIXTURES / "cursor_legacy_copilot" / "sample_session.jsonl"
    prompts_path = FIXTURES / "cursor_legacy_copilot" / "sample_prompts.jsonl"

    summary = ingest.ingest_path(conn, session_path)
    assert summary.totals["inserted"] == 1
    session_conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("cpsess-1111-2222-3333-444455556666",)
    ).fetchone()
    assert session_conv["platform"] == "cursor_copilot"
    assert session_conv["message_count"] == 3
    roles = [
        r[0]
        for r in conn.execute(
            "SELECT role FROM messages WHERE conv_id = ? ORDER BY idx", (session_conv["id"],)
        ).fetchall()
    ]
    assert roles == ["user", "tool", "assistant"]

    summary2 = ingest.ingest_path(conn, prompts_path)
    assert summary2.totals["inserted"] == 1
    prompts_conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("def456_prompts",)
    ).fetchone()
    # a raw prompt dump has no replies — every message is a standalone user turn
    assert prompts_conv["message_count"] == 2
    prompt_roles = [
        r[0]
        for r in conn.execute(
            "SELECT role FROM messages WHERE conv_id = ? ORDER BY idx", (prompts_conv["id"],)
        ).fetchall()
    ]
    assert prompt_roles == ["user", "user"]
    texts = [
        r[0]
        for r in conn.execute(
            "SELECT text FROM v_blocks WHERE conv_id = ? ORDER BY id", (prompts_conv["id"],)
        ).fetchall()
    ]
    assert texts == ["add a retry with backoff to the fetch loop", "now write a test for the retry logic"]


def test_chatgpt_legacy_recovered_adapter_infers_role_from_id_scheme_a(conn):
    """scheme A: root / root-nextPrompt / <uuid> / <uuid>-nextPrompt."""
    path = FIXTURES / "chatgpt_legacy_recovered" / "scheme_a_single.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["platform"] == "chatgpt"
    assert conv["native_id"] == "aaaaaaaa-1111-2222-3333-444444444444"
    assert conv["title"] == "Debugging a flaky test"
    assert conv["model_primary"] is None  # genuinely absent from this source, not a parsing gap
    assert conv["message_count"] == 4  # "root" placeholder excluded

    roles = [
        r[0]
        for r in conn.execute(
            "SELECT role FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
        ).fetchall()
    ]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_chatgpt_legacy_recovered_adapter_infers_role_from_id_scheme_b(conn):
    """scheme B: aaa1<hex> placeholder / aaa2<hex> user / plain uuid assistant."""
    path = FIXTURES / "chatgpt_legacy_recovered" / "scheme_b_single.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["message_count"] == 4  # "aaa1..." placeholder excluded

    roles = [
        r[0]
        for r in conn.execute(
            "SELECT role FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
        ).fetchall()
    ]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_chatgpt_legacy_recovered_adapter_handles_bulk_list_and_skips_empty(conn):
    path = FIXTURES / "chatgpt_legacy_recovered" / "bulk_export.json"
    summary = ingest.ingest_path(conn, path)
    # the second conversation in the list has an empty messages array and
    # must be skipped, not inserted as an empty shell
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["native_id"] == "cccccccc-1111-2222-3333-444444444444"
    assert conv["message_count"] == 2  # "root" placeholder excluded, 2 real turns remain

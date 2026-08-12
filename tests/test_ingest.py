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


def test_claude_desktop_adapter_ingests(conn):
    path = FIXTURES / "claude_desktop" / "conversations.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1
    row = conn.execute("SELECT platform, message_count FROM conversations").fetchone()
    assert row["platform"] == "claude_desktop"
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


def test_windsurf_legacy_adapter_surfaces_thinking_field(conn):
    """Regression test for the 2026-08-05 history-r pbwire fix: assistant
    records can now carry a `thinking` field alongside (or instead of) their
    final-answer text. An assistant turn with both gets a dedicated
    `thinking` block ahead of its `text` block (new — this was previously
    discarded entirely, present only in raw_json). A turn whose only text
    *is* its thinking trace (no separate final answer) gets one block typed
    `thinking`, not `text` — no duplication, since there is no separate
    final-answer content."""
    path = FIXTURES / "windsurf_legacy" / "thinking_field_sample.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["message_count"] == 5

    messages = conn.execute(
        "SELECT id, role FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
    ).fetchall()

    def blocks_for(msg_id):
        return conn.execute(
            "SELECT type, text FROM blocks WHERE msg_id = ? ORDER BY idx", (msg_id,)
        ).fetchall()

    # idx 1: thinking + final answer -> two blocks, thinking first
    both_blocks = blocks_for(messages[1]["id"])
    assert [b["type"] for b in both_blocks] == ["thinking", "text"]
    assert both_blocks[0]["text"] == "The user wants a plan. I should check the config before proposing anything."
    assert both_blocks[1]["text"] == "I'll check the config first."

    # idx 2: thinking only, no separate final answer -> one block, typed thinking
    thinking_only_blocks = blocks_for(messages[2]["id"])
    assert [b["type"] for b in thinking_only_blocks] == ["thinking"]
    assert thinking_only_blocks[0]["text"] == "This turn is pure tool-call scaffolding with no separate final answer."

    # idx 4: final answer only, no thinking field at all -> unchanged, single text block
    plain_blocks = blocks_for(messages[4]["id"])
    assert [b["type"] for b in plain_blocks] == ["text"]
    assert plain_blocks[0]["text"] == "You're welcome."


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


def test_openclaw_adapter_maps_role_model_and_timestamps(conn):
    """Regression test for the generic_jsonl fallback's total failure on this
    format: role lives nested under message.role (not a top-level key), and
    model is carried by separate model_change/custom(model-snapshot) events,
    not a per-message "model" key. Forcing generic_jsonl on this fixture
    would produce role="unknown" and model=NULL for every row; timestamp
    happens to work by accident since "timestamp" is one of its _TIME_KEYS."""
    path = FIXTURES / "openclaw" / "session1.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["platform"] == "openclaw"
    assert conv["native_id"] == "aaaa1111-2222-3333-4444-555566667777"
    assert conv["project"] == "workspace"
    assert conv["title"] == "Can you check the deployment status?"
    assert conv["started_at"] == "2026-01-01T00:00:00.000Z"
    assert conv["ended_at"] == "2026-01-01T00:00:07.000Z"
    # session/model_change/thinking_level_change lines are bookkeeping, not
    # turns: 4 "message" events + 1 compaction + 1 unmapped-role message = 6.
    assert conv["message_count"] == 6

    rows = conn.execute(
        "SELECT role, model FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
    ).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant", "system", "system"]
    assert rows[0]["model"] is None  # user messages never carry a model
    assert rows[1]["model"] == "anthropic/claude-haiku-4-5"  # from model_change
    assert rows[2]["model"] is None  # toolResult
    assert rows[3]["model"] == "google-vertex/gemini-3-flash-preview"  # from custom/model-snapshot
    assert rows[4]["model"] is None  # compaction, synthesised as system
    assert rows[5]["model"] is None  # unmapped "developer" role -> system, no model

    blocks = conn.execute(
        "SELECT type, tool_name, text FROM blocks WHERE conv_id = ? ORDER BY id", (conv["id"],)
    ).fetchall()
    assert [b["type"] for b in blocks] == ["text", "tool_use", "tool_result", "text", "system", "unknown"]
    assert blocks[1]["tool_name"] == "session_status"
    assert blocks[2]["text"] == "All systems nominal."
    # the unmapped content-item type must be preserved losslessly in raw_json
    unknown_raw = conn.execute(
        "SELECT raw_json FROM blocks WHERE conv_id = ? AND type = 'unknown'", (conv["id"],)
    ).fetchone()["raw_json"]
    assert "scratch notes" in unknown_raw


def test_openclaw_adapter_discovers_reset_backup_with_compound_extension(conn):
    """OpenClaw writes pre-reset backups as "<uuid>.jsonl.reset[.<ts>]" —
    Path.suffix on that is NOT ".jsonl", so both ingest.py's file-discovery
    gate and a naive sniff() would silently drop it. Must be found via glob
    pattern on the filename, without renaming anything on disk."""
    path = (
        FIXTURES
        / "openclaw"
        / "bbbb2222-3333-4444-5555-666677778888.jsonl.reset.2026-01-02T00-00-00.000Z"
    )
    assert path.exists()  # sanity: the odd-extension fixture really has no ".jsonl" suffix
    assert path.suffix != ".jsonl"

    # ingest_path(<parent dir>) must discover this file via directory walk,
    # not just direct-file ingestion — that's the actual discovery gate.
    summary = ingest.ingest_path(conn, path.parent)
    result = next(f for f in summary.files if f.path == path)
    assert result.status == "ok"
    assert result.adapter == "openclaw"

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?",
        ("bbbb2222-3333-4444-5555-666677778888",),
    ).fetchone()
    assert conv is not None
    assert conv["message_count"] == 2
    assert conv["model_primary"] == "openai/gpt-5-mini"
    roles = [
        r[0]
        for r in conn.execute(
            "SELECT role FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
        ).fetchall()
    ]
    assert roles == ["user", "assistant"]


def test_openclaw_adapter_ingest_is_idempotent(conn):
    path = FIXTURES / "openclaw" / "session1.jsonl"
    ingest.ingest_path(conn, path)
    first_counts = _counts(conn)

    summary2 = ingest.ingest_path(conn, path)
    assert summary2.totals == {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
    assert summary2.files[0].status == "skipped_dup"
    assert _counts(conn) == first_counts


def test_hermes_adapter_parses_bare_conversation_dump(conn):
    """hermes_conversation_*.json is a single pretty-printed JSON object, not
    JSONL — generic_jsonl.parse() reads it line-by-line, every line fails to
    parse standalone, and it silently yields zero conversations (total data
    loss, not a role/model/timestamp mismapping)."""
    path = FIXTURES / "hermes" / "hermes_conversation_20260101_010101.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["platform"] == "hermes"
    assert conv["native_id"] == "20260101_010101"
    assert conv["title"] == "What is your name?"
    assert conv["started_at"] == "2026-01-01T01:01:01.000000"
    assert conv["ended_at"] is None  # genuinely absent from this shape, not a parsing gap
    assert conv["message_count"] == 2
    assert conv["model_primary"] == "models/gemini-3.1-flash-lite-preview"

    roles = [
        r[0]
        for r in conn.execute(
            "SELECT role FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
        ).fetchall()
    ]
    assert roles == ["user", "assistant"]


def test_hermes_adapter_parses_session_envelope_shape(conn):
    path = FIXTURES / "hermes" / "session_20260101_020202_abc123.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["native_id"] == "20260101_020202_abc123"
    assert conv["project"] == "cli"
    assert conv["started_at"] == "2026-01-01T02:02:02.000000"
    assert conv["ended_at"] == "2026-01-01T02:02:30.000000"
    assert conv["model_primary"] == "gemini-2.5-pro"


def test_hermes_adapter_parses_jsonl_transcript_with_tool_calls(conn):
    path = FIXTURES / "hermes" / "20260101_030303_def456.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["native_id"] == "20260101_030303_def456"
    assert conv["message_count"] == 5
    assert conv["model_primary"] == "gemini-3-flash-preview"

    rows = conn.execute(
        "SELECT role, model FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
    ).fetchall()
    assert [r["role"] for r in rows] == ["system", "user", "assistant", "tool", "assistant"]
    assert rows[0]["model"] is None  # session_meta
    assert rows[2]["model"] == "gemini-3-flash-preview"  # assistant carries session model
    assert rows[3]["model"] is None  # tool result

    blocks = conn.execute(
        "SELECT m.idx, b.type, b.tool_name, b.text FROM blocks b JOIN messages m ON m.id = b.msg_id "
        "WHERE b.conv_id = ? ORDER BY m.idx, b.idx",
        (conv["id"],),
    ).fetchall()
    assistant_call_blocks = [b for b in blocks if b["idx"] == 2]
    assert [b["type"] for b in assistant_call_blocks] == ["text", "tool_use"]
    assert assistant_call_blocks[1]["tool_name"] == "execute_code"
    tool_result_block = next(b for b in blocks if b["idx"] == 3)
    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["text"] == "4"


def test_hermes_adapter_ingest_is_idempotent(conn):
    dir_path = FIXTURES / "hermes"
    ingest.ingest_path(conn, dir_path)
    first_counts = _counts(conn)

    summary2 = ingest.ingest_path(conn, dir_path)
    assert summary2.totals == {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
    assert all(f.status == "skipped_dup" for f in summary2.files)
    assert _counts(conn) == first_counts


def test_deepseek_adapter_ingests_and_is_not_shadowed_by_chatgpt(conn):
    """Regression test for the bug where a real DeepSeek export ("mapping" +
    "fragments") satisfied chatgpt.sniff's old "mapping"-only check and
    chatgpt won by registry order, silently mislabeling every DeepSeek file."""
    path = FIXTURES / "deepseek" / "conversations.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1
    assert summary.files[0].adapter == "deepseek"

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv["platform"] == "deepseek"
    assert conv["native_id"] == "ds-conv-1"
    assert conv["project"] is None  # verified: no workspace/project concept in DeepSeek's export
    assert conv["model_primary"] == "deepseek-v3"

    rows = conn.execute(
        "SELECT role, model FROM messages WHERE conv_id = ? ORDER BY idx", (conv["id"],)
    ).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]

    blocks = conn.execute(
        "SELECT type, text FROM blocks WHERE conv_id = ? ORDER BY id", (conv["id"],)
    ).fetchall()
    assert [b["type"] for b in blocks] == ["text", "thinking", "text"]


def test_gemini_takeout_adapter_ingests_and_flags_model_unknown(conn):
    path = FIXTURES / "gemini_takeout" / "MyActivity.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 2  # 1 real dialogue + 1 standalone event

    convs = conn.execute("SELECT id, platform, model_primary, title FROM conversations ORDER BY id").fetchall()
    assert all(c["platform"] == "gemini" for c in convs)
    assert all(c["model_primary"] is None for c in convs)  # no model field exists anywhere in this export

    tagged = conn.execute(
        """
        SELECT c.id FROM conversations c
        JOIN tag_links tl ON tl.target_type = 'conversation' AND tl.target_id = c.id
        JOIN tags t ON t.id = tl.tag_id
        WHERE t.name = 'review/model-unknown'
        """
    ).fetchall()
    assert {r["id"] for r in tagged} == {c["id"] for c in convs}


def test_grok_mixed_file_ingests_both_conversations_and_projects(conn):
    """Regression test for the ingest.py dispatch bug: detect_projects()
    matching a file used to `continue` past conversation ingestion entirely,
    which silently dropped every conversation in a file (like Grok's real
    export) that carries both `conversations` and `projects` in one file."""
    path = FIXTURES / "grok" / "prod-grok-backend.json"
    summary = ingest.ingest_path(conn, path)

    # both a conversations-adapter result and a projects result for the same file
    adapters_seen = {f.adapter for f in summary.files}
    assert "grok" in adapters_seen
    assert "grok (projects)" in adapters_seen

    conv = conn.execute("SELECT * FROM conversations").fetchone()
    assert conv is not None
    assert conv["platform"] == "grok"
    assert conv["native_id"] == "grok-conv-1"
    assert conv["project"] == "ws-1"
    assert conv["model_primary"] == "grok-4"

    project = conn.execute("SELECT * FROM projects").fetchone()
    assert project is not None
    assert project["kind"] == "grok_project"
    assert project["native_id"] == "ws-1"
    assert project["name"] == "job-hunter"

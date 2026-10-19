from __future__ import annotations

import json
import shutil
from pathlib import Path

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
    assert conv["project"] == "example-project"
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


def test_windsurf_legacy_thinking_and_text_blocks_stay_isolated(conn):
    """Regression guard for the reasoning-locator sweep's flagged row
    (docs/audit-shapes/reasoning-sweep.md: "windsurf | $.thinking | assistant
    | text | 849 | 970064"). Investigation against production (block ids
    101528/101529 and 847 other identical pairs) showed this is NOT a live
    misclassification: every one of those 849 `text`-typed blocks is the
    correctly-paired final-answer sibling of a `thinking`-typed block built
    from the exact same underlying record (raw_json is shared between
    siblings by design, which is what made the sweep's raw_json-based path
    search see `$.thinking` "inside" the text block too). The actual `text`
    column of the text-typed sibling must come from `content`/`text` only —
    never from `thinking` — which is what this test pins down explicitly,
    using the `thinking_field_sample.jsonl` fixture's idx-1 record (the same
    thinking+content shape as the production pairs)."""
    path = FIXTURES / "windsurf_legacy" / "thinking_field_sample.jsonl"
    ingest.ingest_path(conn, path)

    conv = conn.execute("SELECT id FROM conversations").fetchone()
    msg = conn.execute(
        "SELECT id FROM messages WHERE conv_id = ? AND idx = 1", (conv["id"],)
    ).fetchone()
    blocks = conn.execute(
        "SELECT idx, type, text FROM blocks WHERE msg_id = ? ORDER BY idx", (msg["id"],)
    ).fetchall()

    thinking_block, text_block = blocks
    assert thinking_block["type"] == "thinking"
    assert text_block["type"] == "text"
    # The text block's own `text` must be the final answer, not a copy of
    # (or containing) the thinking trace.
    assert text_block["text"] == "I'll check the config first."
    assert "plan" not in text_block["text"].lower()
    assert thinking_block["text"] not in text_block["text"]


def test_windsurf_legacy_unknown_shape_fixes(conn):
    """Regression test for the two evidenced windsurf `unknown`-block fixes
    (docs/audit-shapes/windsurf-unknown.md):

    - `type_84` (11 occurrences in production, tool_name/tool_id/tool_args +
      content/text all present) is a completed tool invocation, aliased to
      `tool_result` rather than left an anonymous enum leak.
    - every other numeric `type_NN` role stays `unknown` — a content-bearing
      one (`type_38`, evidenced by `$.content`/`$.text`) gets its `text`
      populated from that leaf; a content-less one (`type_89`, the largest
      single unknown shape at 470/727 production occurrences) stays
      `unknown` with empty text, honestly, rather than a guessed label.

    Also covers spec §7.9 / the injected-context fix: a `memory`-role and a
    `context`-role record both carrying the real
    `<SYSTEM-RETRIEVED-MEMORY[uuid]>` marker (confirmed against production:
    100% of windsurf blocks containing this marker have raw role "memory" or
    "context") must classify as `injected_context`, not their default
    `system`/`attachment` bucket — while a plain `memory` record with no
    marker keeps its ordinary `system` classification (no false positive)."""
    path = FIXTURES / "windsurf_legacy" / "unknown_shape_fixes_sample.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute("SELECT id FROM conversations").fetchone()
    blocks = conn.execute(
        "SELECT b.idx, b.type, b.tool_name, b.text "
        "FROM blocks b JOIN messages m ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx", (conv["id"],),
    ).fetchall()

    # `b.idx` is the block's index within its own message (always 0 here,
    # since every fixture message is single-block) — order by message idx
    # instead, which the query above already does.
    ordered = blocks

    type_84_block = ordered[0]
    assert type_84_block["type"] == "tool_result"
    assert type_84_block["tool_name"] == "create_code_map"
    assert type_84_block["text"] == '{"title":"Repo Map","nodes":3}'

    type_38_block = ordered[1]
    assert type_38_block["type"] == "unknown"
    assert type_38_block["text"] == "Some content-bearing but semantically undiscriminated protobuf enum leak."

    type_89_block = ordered[2]
    assert type_89_block["type"] == "unknown"
    assert type_89_block["text"] == ""

    memory_injected = ordered[3]
    assert memory_injected["type"] == "injected_context"

    context_injected = ordered[4]
    assert context_injected["type"] == "injected_context"

    plain_memory = ordered[5]
    assert plain_memory["type"] == "system"
    assert plain_memory["text"] == "Ordinary recalled memory note with no injection marker."


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
    assert conv["project"] == "example-project"
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


def test_cursor_legacy_adapter_extracts_thinking_and_flags_genuine_unknowns(conn):
    """docs/50_SPEC_ingestion_fidelity_v1.md Phase 3/§4 fix for
    docs/audit-shapes/cursor-unknown.md: the 1501/1512-block shape's
    `thinking.text` must surface as a `thinking` block (not stay
    `unknown`), its sibling `thinking.signature` must never leak into
    rendered text, ambiguous-but-content-bearing shapes (a conversation
    summary) stay `unknown` with populated text, an unambiguous one
    (errorDetails) gets classified `error`, and a truly empty bubble stays
    `unknown` with empty text."""
    path = FIXTURES / "cursor_legacy" / "sample_unknown_shapes.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("c0mp0ser-unkn0wn-shapes-0000",)
    ).fetchone()
    assert conv["message_count"] == 4

    rows = conn.execute(
        "SELECT b.type, b.text FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx, b.idx", (conv["id"],)
    ).fetchall()
    assert [r["type"] for r in rows] == ["thinking", "unknown", "error", "unknown"]

    thinking_text = rows[0]["text"]
    assert "Checking network config" in thinking_text
    # The opaque Fernet-prefixed signature blob must never reach blocks.text.
    assert "gAAAAA" not in thinking_text
    assert "encrypted_content" not in thinking_text

    assert "Primary Request and Intent" in rows[1]["text"]

    assert "resource_exhausted" in rows[2]["text"]

    assert rows[3]["text"] == ""

    # raw_json stays byte-identical for the reasoning block: the signature
    # is preserved verbatim in raw even though it's excluded from text.
    raw = json.loads(
        conn.execute(
            "SELECT b.raw_json FROM messages m JOIN blocks b ON b.msg_id = m.id "
            "WHERE m.conv_id = ? ORDER BY m.idx, b.idx LIMIT 1", (conv["id"],)
        ).fetchone()[0]
    )
    assert "gAAAAA" in raw["signature"]


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


def test_vscode_legacy_adapter_classifies_previously_unknown_shapes(conn):
    """Regression coverage for docs/50_SPEC_ingestion_fidelity_v1.md §4 —
    the 11 shapes vscode-unknown.md enumerated behind `type=unknown`
    (role='result' bundles and the 10 role='assistant' `kind` values), plus
    the vscode-specific reasoning-sweep fix (tool 'thought' -> thinking)."""
    path = FIXTURES / "vscode_legacy" / "sample_session_shapes.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("vsc0de-shapes-0001",)
    ).fetchone()
    assert conv is not None

    rows = conn.execute(
        "SELECT m.idx as msg_idx, b.idx as blk_idx, m.role, b.type, b.tool_name, b.text "
        "FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx, b.idx",
        (conv["id"],),
    ).fetchall()
    by_msg: dict[int, list] = {}
    for r in rows:
        by_msg.setdefault(r["msg_idx"], []).append(r)

    # inlineReference (idx 1) -> attachment, text = the referenced path.
    assert by_msg[1][0]["type"] == "attachment"
    assert "xAI_positions" in by_msg[1][0]["text"]

    # codeblockUri (idx 2) -> attachment, text = the code block's resource.
    assert by_msg[2][0]["type"] == "attachment"
    assert "export.sh" in by_msg[2][0]["text"]

    # undoStop (idx 3) -> meta, no real prose content.
    assert by_msg[3][0]["type"] == "meta"

    # mcpServersStarting (idx 4) -> meta, genuinely zero content.
    assert by_msg[4][0]["type"] == "meta"

    # progressTask (idx 5) -> system, carries the compaction-style summary.
    assert by_msg[5][0]["type"] == "system"
    assert "Summarized conversation history" in by_msg[5][0]["text"]

    # confirmation (idx 6) -> system, the confirmation prompt text.
    assert by_msg[6][0]["type"] == "system"
    assert "Continue to iterate" in by_msg[6][0]["text"]

    # elicitation (idx 7) -> system.
    assert by_msg[7][0]["type"] == "system"
    assert "process_thesis_pdfs" in by_msg[7][0]["text"]

    # command (idx 8) -> system, the command's title.
    assert by_msg[8][0]["type"] == "system"
    assert "Create File" in by_msg[8][0]["text"]

    # tool record (idx 9) carries a toolSpecificData.rawInput.thought
    # (reasoning-sweep.md's vscode finding) -> a leading thinking block,
    # then the tool_use block, tool_name preserved.
    tool_blocks = by_msg[9]
    assert [b["type"] for b in tool_blocks] == ["thinking", "tool_use"]
    assert "fetch the careers page" in tool_blocks[0]["text"]
    assert tool_blocks[1]["tool_name"] == "fetch_webpage"

    # role='result' with rich metadata (idx 10) decomposes into several
    # typed blocks instead of one empty `unknown` block.
    result_blocks = {b["type"]: b["text"] for b in by_msg[10]}
    assert "injected_context" in result_blocks
    assert "current date is 2 January 2026" in result_blocks["injected_context"]
    assert "thinking" in result_blocks
    assert "fetch the careers page" in result_blocks["thinking"] or "careers page" in result_blocks["thinking"]
    assert "tool_result" in {b["type"] for b in by_msg[10]}
    tool_result_texts = [b["text"] for b in by_msg[10] if b["type"] == "tool_result"]
    assert any("ai_file_detective.py" in t for t in tool_result_texts)  # edits bundle
    assert any("open-roles" in t or "Data Scientist" in t for t in tool_result_texts)  # toolCallResults
    assert "system" in result_blocks
    assert "Chronological Review" in result_blocks["system"]
    assert "text" in result_blocks
    assert "departments" in result_blocks["text"] or "anthropic folder" in result_blocks["text"]
    assert "unknown" not in result_blocks

    # role='result' with empty metadata (idx 11): no evidenced leaf matched,
    # so it stays `unknown` per spec §4.4 rule 2 — but with populated text
    # (never a silent empty unknown block), and the conversation gets
    # tagged for review instead of a guessed label.
    fallback_blocks = by_msg[11]
    assert fallback_blocks[0]["type"] == "unknown"
    assert fallback_blocks[0]["text"] != ""
    assert "500" in fallback_blocks[0]["text"]

    tag_row = conn.execute(
        """
        SELECT 1 FROM tag_links tl
        JOIN tags t ON t.id = tl.tag_id
        WHERE t.name = 'review/shape-unclear' AND tl.target_type = 'conversation' AND tl.target_id = ?
        """,
        (conv["id"],),
    ).fetchone()
    assert tag_row is not None


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


def test_cursor_legacy_copilot_adapter_classifies_previously_unknown_shapes(conn):
    """Regression coverage for docs/50_SPEC_ingestion_fidelity_v1.md §4 —
    the 7 `role='assistant'` `kind` shapes docs/audit-shapes/
    cursor_copilot-unknown.md enumerated behind `type=unknown` (620 blocks),
    plus the fallback path for a kind outside that inventory."""
    path = FIXTURES / "cursor_legacy_copilot" / "sample_session_shapes.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("cpsess-shapes-0001",)
    ).fetchone()
    assert conv is not None

    rows = conn.execute(
        "SELECT m.idx as msg_idx, m.role, b.type, b.tool_name, b.text "
        "FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx, b.idx",
        (conv["id"],),
    ).fetchall()
    by_msg = {r["msg_idx"]: r for r in rows}

    # codeblockUri (idx 1) -> attachment, text = the code block's resource.
    assert by_msg[1]["type"] == "attachment"
    assert "build-info.sh" in by_msg[1]["text"]

    # inlineReference, direct variant (idx 2) -> attachment.
    assert by_msg[2]["type"] == "attachment"
    assert "/bin/zsh" in by_msg[2]["text"]

    # inlineReference, location/symbol variant (idx 3) -> attachment, prefers
    # the symbol name over the nested location path.
    assert by_msg[3]["type"] == "attachment"
    assert by_msg[3]["text"] == "compare-commits.sh"

    # textEditGroup (idx 4) -> tool_result, the reconstructed code edit.
    assert by_msg[4]["type"] == "tool_result"
    assert "import sys" in by_msg[4]["text"]
    assert "def main(checkpoint_dir)" in by_msg[4]["text"]
    assert "build-info.sh" in by_msg[4]["text"]

    # progressMessage (idx 5) -> meta, the transient status text.
    assert by_msg[5]["type"] == "meta"
    assert "Refining question" in by_msg[5]["text"]

    # command (idx 6) -> system, the command's title.
    assert by_msg[6]["type"] == "system"
    assert "Show in Settings Editor" in by_msg[6]["text"]

    # confirmation (idx 7) -> system, joining title + message + the replayed
    # prompt that triggered it.
    assert by_msg[7]["type"] == "system"
    assert "Enable Claude 3.7 Sonnet" in by_msg[7]["text"]
    assert "CrewAI" in by_msg[7]["text"]

    # kind='text' (idx 8) is actually a working-set folder/file tree, not
    # prose -> attachment, not the misleading 'text' block type.
    assert by_msg[8]["type"] == "attachment"
    assert "pdf-ocr-project" in by_msg[8]["text"]
    assert "main.py" in by_msg[8]["text"]

    # A kind outside the audit's 7-shape inventory (idx 9) stays `unknown`
    # per spec §4.4 rule 2, but with populated text (never silently empty).
    assert by_msg[9]["type"] == "unknown"
    assert by_msg[9]["text"] != ""
    assert "mysteryFutureKind" in by_msg[9]["text"]

    # role='tool' records are unaffected by this change.
    assert by_msg[10]["type"] == "tool_use"
    assert by_msg[10]["tool_name"] == "run_terminal"

    tag_row = conn.execute(
        """
        SELECT 1 FROM tag_links tl
        JOIN tags t ON t.id = tl.tag_id
        WHERE t.name = 'review/shape-unclear' AND tl.target_type = 'conversation' AND tl.target_id = ?
        """,
        (conv["id"],),
    ).fetchone()
    assert tag_row is not None


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


def test_gemini_html_month_first_12h_timestamp(conn):
    """Pre-Oct-16 export shape: 'Sep 16, 2026, 7:08:02 PM CEST'."""
    path = FIXTURES / "gemini_takeout" / "MyActivity-month-first.html"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    msg = conn.execute("SELECT created_at FROM messages WHERE role = 'assistant'").fetchone()
    assert msg["created_at"] == "2026-09-16T17:08:02Z"  # CEST = UTC+2


def test_gemini_html_day_first_24h_with_sept_abbrev(conn):
    """Oct-16 export shape: '5 Sept 2026, 09:12:30 CEST' — day-first, 24h,
    and 'Sept' as a four-letter month token (observed 294x in the real
    file). %b only accepts 'Sep', so the parser normalises it."""
    path = FIXTURES / "gemini_takeout" / "MyActivity-day-first-sept.html"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    msg = conn.execute("SELECT created_at FROM messages WHERE role = 'assistant'").fetchone()
    assert msg["created_at"] == "2026-09-05T07:12:30Z"


def test_gemini_html_quoted_date_in_prompt_is_not_the_timestamp(conn):
    """Regression: the cell's real timestamp is <br>-delimited; a date string
    inside prompt prose must never be parsed as the record's timestamp (a
    date quoted in a real Oct-16 response crashed the original parser)."""
    path = FIXTURES / "gemini_takeout" / "MyActivity-quoted-date.html"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    user = conn.execute("SELECT created_at FROM messages WHERE role = 'user'").fetchone()
    assert user["created_at"] == "2026-10-16T14:34:52Z"  # the real cell ts

    prompt = conn.execute(
        "SELECT text FROM blocks b JOIN messages m ON m.id = b.msg_id WHERE m.role = 'user'"
    ).fetchone()
    assert "July 26, 2025" in prompt["text"]  # quoted date survives as content


def test_gemini_html_unparseable_timestamp_degrades_and_flags(conn):
    """B2a contract: an unparseable cell timestamp must not fail the file —
    the conversation ingests with created_at=NULL, the file's ingest result
    carries a warning, and the conversation is tagged
    review/timestamp-unparsed for a human."""
    path = FIXTURES / "gemini_takeout" / "MyActivity-bad-ts.html"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1
    assert summary.files[0].status == "ok"
    assert any("timestamp(s) unparseable" in w for w in summary.files[0].warnings)

    conv = conn.execute("SELECT id, started_at FROM conversations").fetchone()
    assert conv["started_at"] is None
    msg = conn.execute("SELECT created_at, raw_json FROM messages").fetchone()
    assert msg["created_at"] is None

    tagged = conn.execute(
        """
        SELECT c.id FROM conversations c
        JOIN tag_links tl ON tl.target_type = 'conversation' AND tl.target_id = c.id
        JOIN tags t ON t.id = tl.tag_id
        WHERE t.name = 'review/timestamp-unparsed'
        """
    ).fetchall()
    assert {r["id"] for r in tagged} == {conv["id"]}


def test_gemini_html_unknown_timezone_counted_and_treated_as_utc():
    """B2b contract: an unrecognised timezone abbreviation is counted in
    parse_stats (surfaced as an ingest warning) instead of silently writing
    a timestamp wrong by hours."""
    from chatvault.adapters import gemini_takeout

    gemini_takeout.parse_stats["unknown_timezones"] = {}
    iso = gemini_takeout._parse_html_timestamp("16 Oct 2026, 16:34:52 PST")
    assert iso == "2026-10-16T16:34:52Z"  # falls back to UTC
    assert gemini_takeout.parse_stats["unknown_timezones"] == {"PST": 1}


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


def test_grok_adapter_classifies_previously_unknown_shapes(conn):
    """docs/50_SPEC_ingestion_fidelity_v1.md Phase 3/§4 fix for
    docs/audit-shapes/grok-unknown.md: a bare top-level `thinking_trace`
    (not nested under `agent_thinking_traces`) must surface as `thinking`,
    a top-level `error` string must classify as `error`, and a genuinely
    content-free response (partial/manual, no message/thinking/error) must
    stay `unknown` with empty text rather than being guessed at."""
    path = FIXTURES / "grok" / "unknown_shapes.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("grok-conv-unknown-shapes",)
    ).fetchone()
    assert conv is not None

    rows = conn.execute(
        "SELECT b.type, b.text FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx, b.idx", (conv["id"],)
    ).fetchall()
    assert [r["type"] for r in rows] == ["text", "thinking", "error", "unknown"]
    assert "Bare top-level thinking_trace" in rows[1]["text"]
    assert rows[2]["text"] == "Failed to send request."
    assert rows[3]["text"] == ""


def test_chatgpt_adapter_leaves_bare_id_text_shape_unknown(conn):
    """docs/50_SPEC_ingestion_fidelity_v1.md Phase 3/§4 fix for
    docs/audit-shapes/chatgpt-unknown.md: a bare `{id, text}` content dict
    (no `content_type` at all) has no content-bearing leaf when `text` is
    empty, so it correctly stays `unknown` with empty text rather than
    surfacing the `id` UUID as if it were dialogue."""
    path = FIXTURES / "chatgpt" / "bare_id_text_shape.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("conv-bare-shape",)
    ).fetchone()
    assert conv is not None

    block = conn.execute(
        "SELECT b.type, b.text FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx DESC LIMIT 1", (conv["id"],)
    ).fetchone()
    assert block["type"] == "unknown"
    assert block["text"] == ""


def test_claude_code_adapter_classifies_fallback_and_pdf_document(conn):
    """docs/50_SPEC_ingestion_fidelity_v1.md Phase 3/§4 fix for
    docs/audit-shapes/claude_code-unknown.md: a `type='fallback'` content
    item (a model-routing event) becomes `meta` with a human-readable
    summary, and a `type='document'` base64-PDF content item becomes an
    `attachment` block (not `unknown` text dumped with raw base64)."""
    path = FIXTURES / "claude_code" / "unknown_shapes.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("s-unknown",)
    ).fetchone()
    assert conv is not None

    rows = conn.execute(
        "SELECT b.type, b.text, b.attachment_id FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx, b.idx", (conv["id"],)
    ).fetchall()
    assert [r["type"] for r in rows] == ["text", "meta", "attachment", "text"]
    assert "claude-fable-5" in rows[1]["text"]
    assert "claude-opus-4-8" in rows[1]["text"]
    assert rows[2]["attachment_id"] is not None

    attachment = conn.execute(
        "SELECT mime, availability FROM attachments WHERE id = ?", (rows[2]["attachment_id"],)
    ).fetchone()
    assert attachment["mime"] == "application/pdf"


def test_claude_desktop_adapter_surfaces_sequential_thinking_tool_input(conn):
    """docs/50_SPEC_ingestion_fidelity_v1.md §4.2b fix for
    docs/audit-shapes/reasoning-sweep.md's claude_desktop row: the
    sequential-thinking MCP tool's `input.thought` is real reasoning prose
    nested inside a legitimate `tool_use` call. Both the `tool_use` block
    (still carrying the full input, including thoughtNumber/totalThoughts
    bookkeeping) and a new `thinking` block (carrying just the prose) must
    be emitted — a split, not a re-type."""
    path = FIXTURES / "claude_desktop" / "sequential_thinking.json"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?", ("web-conv-thinking",)
    ).fetchone()
    assert conv is not None

    rows = conn.execute(
        "SELECT b.type, b.tool_name, b.text FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx, b.idx", (conv["id"],)
    ).fetchall()
    assert [r["type"] for r in rows] == ["text", "tool_use", "thinking"]
    assert rows[1]["tool_name"] == "sequential-thinking:sequentialthinking"
    assert "thoughtNumber" in rows[1]["text"]
    assert "qBittorrent" in rows[2]["text"]


def test_openclaw_adapter_surfaces_nested_thinking_and_leaves_effort_arg_alone(conn):
    """docs/50_SPEC_ingestion_fidelity_v1.md §4.2b fix for
    docs/audit-shapes/reasoning-sweep.md's openclaw rows: a `toolResult`'s
    structured `details.messages[]`/`details.sessions[].messages[]` payload
    (sessions_history/sessions_list) must surface its nested `thinking`
    items as their own `thinking` blocks, alongside the existing
    `tool_result` block. A `toolCall`'s `arguments.thinking` (a thinking-
    effort level like "low", not reasoning content) must NOT be
    reclassified — it stays `tool_use`."""
    path = FIXTURES / "openclaw" / "nested_thinking_shapes.jsonl"
    summary = ingest.ingest_path(conn, path)
    assert summary.totals["inserted"] == 1

    conv = conn.execute(
        "SELECT * FROM conversations WHERE native_id = ?",
        ("cccc3333-4444-5555-6666-777788889999",),
    ).fetchone()
    assert conv is not None

    rows = conn.execute(
        "SELECT b.type, b.text FROM messages m JOIN blocks b ON b.msg_id = m.id "
        "WHERE m.conv_id = ? ORDER BY m.idx, b.idx", (conv["id"],)
    ).fetchall()
    assert [r["type"] for r in rows] == ["text", "tool_use", "tool_result", "thinking", "tool_result", "thinking"]
    assert '"thinking": "low"' in rows[1]["text"] or '"thinking":"low"' in rows[1]["text"].replace(" ", "") or "low" in rows[1]["text"]
    assert "shape A" in rows[3]["text"]
    assert "shape B" in rows[5]["text"]


def test_ingested_archive_dir_skipped_on_parent_walk(tmp_path: Path):
    """Files under raw/_ingested/ are ignored when walking raw/, but an
    explicit ingest of that archive tree still sees them."""
    raw = tmp_path / "raw"
    active = raw / "active"
    archived = raw / "_ingested" / "old_export"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    src = FIXTURES / "claude_code" / "session1.jsonl"
    shutil.copy(src, active / "session1.jsonl")
    shutil.copy(src, archived / "session1.jsonl")

    found = ingest._iter_candidate_files(raw)
    assert found == [active / "session1.jsonl"]

    found_direct = ingest._iter_candidate_files(archived)
    assert found_direct == [archived / "session1.jsonl"]

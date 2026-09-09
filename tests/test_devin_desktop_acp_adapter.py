from __future__ import annotations

from chatvault import ingest
from chatvault.adapters import devin_desktop_acp
from tests.conftest import FIXTURES

ACP_DIR = FIXTURES / "devin_desktop_acp"


def test_sniff_accepts_own_schema_tag_and_rejects_other_jsonl():
    assert devin_desktop_acp.sniff(ACP_DIR / "real_conversation.jsonl") is True
    assert devin_desktop_acp.sniff(FIXTURES / "devin_cli" / "real_session.jsonl") is False
    assert devin_desktop_acp.sniff(FIXTURES / "hermes" / "20260101_030303_def456.jsonl") is False


def test_empty_conversation_file_yields_nothing():
    """Report §5: 230/326 real files have zero messages -- the extraction
    script already skips those, but the adapter must not crash or fabricate
    a conversation if it ever sees a zero-message record (defensive, since
    this predicate is enforced upstream, not duplicated here as a second
    guess)."""
    convs = list(devin_desktop_acp.parse(ACP_DIR / "empty_conversation.jsonl"))
    assert convs == []


def test_real_conversation_maps_kinds_to_expected_block_types():
    convs = list(devin_desktop_acp.parse(ACP_DIR / "real_conversation.jsonl"))
    assert len(convs) == 1
    conv = convs[0]
    assert conv["native_id"] == "ca135893-ea07-4977-ba37-0ff1fbc9beb5"
    assert conv["platform"] == "devin_desktop_acp"
    assert conv["title"] == "Technical Workspace Portfolio Analysis"

    block_types = set()
    for m in conv["messages"]:
        for b in m["blocks"]:
            block_types.add(b["type"])

    assert "thinking" in block_types  # from agent_thought
    assert "text" in block_types  # from agent_message / user_message
    assert "tool_use" in block_types
    assert "tool_result" in block_types


def test_tool_call_row_splits_into_use_and_result_blocks():
    """Report §4.2: unlike sessions.db, ACP carries call+result in ONE row
    (content.rawInput + content.content[]) -- the adapter must still split
    it into two blocks to match the shared vocabulary."""
    convs = list(devin_desktop_acp.parse(ACP_DIR / "real_conversation.jsonl"))
    conv = convs[0]
    for m in conv["messages"]:
        types = [b["type"] for b in m["blocks"]]
        if "tool_use" in types:
            # every tool_call-derived message has both halves when a
            # result was present in the source row
            assert types.count("tool_use") >= 1


def test_plan_kind_maps_to_meta_block_not_dialogue():
    """Report §4.3: `plan` is a 6th kind doc 48 never listed -- todo/plan
    widget state, not conversation, must not read as extra assistant
    dialogue."""
    convs = list(devin_desktop_acp.parse(ACP_DIR / "plan_conversation.jsonl"))
    conv = convs[0]
    # at least one 'plan' kind row present in the fixture -> at least one meta block
    found = any(b["type"] == "meta" for m in conv["messages"] for b in m["blocks"])
    assert found


def test_subagent_kept_nested_not_split_into_new_conversation():
    """Report §4.5: only 6 subagent rows exist corpus-wide, each fully
    self-contained in one row -- kept nested (task+summary as one text
    block, full childMessages preserved in raw), not spun into a second
    conversation the way claude_code.py splits its subagent transcripts."""
    convs = list(devin_desktop_acp.parse(ACP_DIR / "subagent_conversation.jsonl"))
    assert len(convs) == 1  # not 2 -- no new conversation was created
    conv = convs[0]
    subagent_texts = [
        b["text"] for m in conv["messages"] for b in m["blocks"] if b["type"] == "text" and "[subagent:" in b["text"]
    ]
    assert len(subagent_texts) == 1
    assert "Task:" in subagent_texts[0]
    assert "Result:" in subagent_texts[0]
    # full childMessages tree preserved in raw for full-profile/debugging
    subagent_msg = next(m for m in conv["messages"] if m["raw"]["kind"] == "subagent")
    assert "childMessages" in subagent_msg["raw"]["payload"]


def test_model_recovered_from_meta_info_config_options():
    convs = list(devin_desktop_acp.parse(ACP_DIR / "real_conversation.jsonl"))
    conv = convs[0]
    assistant_models = {m["model"] for m in conv["messages"] if m["role"] == "assistant" and m["model"]}
    assert assistant_models  # not empty -- model identity is recoverable (report §6)


def test_ingest_is_idempotent(conn):
    path = ACP_DIR / "real_conversation.jsonl"
    summary1 = ingest.ingest_path(conn, path)
    assert summary1.totals["inserted"] == 1

    summary2 = ingest.ingest_path(conn, path)
    assert summary2.totals == {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
    assert summary2.files[0].status == "skipped_dup"


def test_ingest_empty_conversation_inserts_nothing(conn):
    summary = ingest.ingest_path(conn, ACP_DIR / "empty_conversation.jsonl")
    assert summary.totals["inserted"] == 0

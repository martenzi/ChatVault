from __future__ import annotations

from chatvault import ingest
from chatvault.adapters import devin_cli
from tests.conftest import FIXTURES

CLI_DIR = FIXTURES / "devin_cli"


def test_sniff_accepts_own_schema_tag_and_rejects_other_jsonl():
    assert devin_cli.sniff(CLI_DIR / "real_session.jsonl") is True
    assert devin_cli.sniff(FIXTURES / "hermes" / "20260101_030303_def456.jsonl") is False
    assert devin_cli.sniff(FIXTURES / "openclaw" / "session1.jsonl") is False


def test_hidden_session_yields_no_conversation():
    """Report §2: hidden=1 marks internally-spawned utility sessions (the
    one real instance found was a title/compaction-summary "Summarizer"
    run), not user conversations. The extraction JSONL still carries the
    full record (nothing is deleted at extraction) but parse() must not
    emit it as a conversation."""
    convs = list(devin_cli.parse(CLI_DIR / "hidden_session.jsonl"))
    assert convs == []


def test_real_session_parses_with_main_chain_linearisation():
    """real_session.jsonl (session absorbed-panther) has 28 message_nodes
    but only 9 are on the main chain (main_chain_id=27, walked back via
    parent_node_id) -- report §5. Confirms the linearisation decision:
    messages == main chain only, full node tree still in raw."""
    convs = list(devin_cli.parse(CLI_DIR / "real_session.jsonl"))
    assert len(convs) == 1
    conv = convs[0]
    assert conv["native_id"] == "absorbed-panther"
    assert conv["platform"] == "devin_cli"

    # Fewer emitted messages than raw nodes -- orphan nodes were excluded
    # from `messages` but are still present in `raw`.
    assert len(conv["messages"]) < len(conv["raw"]["message_nodes"])
    assert len(conv["raw"]["message_nodes"]) == 28

    roles = [m["role"] for m in conv["messages"]]
    assert "user" in roles
    assert "assistant" in roles


def test_tool_use_and_tool_result_blocks_linked_by_id():
    """garnet-daffodil fixture: two tool_calls on one assistant node,
    matching tool_result rows on separate role='tool' nodes, linked by
    tool_call_id (report §4.5)."""
    convs = list(devin_cli.parse(CLI_DIR / "tool_call_session.jsonl"))
    conv = convs[0]

    tool_use_names = set()
    tool_result_ids = set()
    thinking_found = False
    for m in conv["messages"]:
        for b in m["blocks"]:
            if b["type"] == "tool_use":
                tool_use_names.add(b["tool_name"])
            elif b["type"] == "tool_result":
                tool_result_ids.add(b["tool_name"])
            elif b["type"] == "thinking":
                thinking_found = True
                assert b["text"]  # reasoning text populated, not empty

    assert "find_file_by_name" in tool_use_names
    assert len(tool_result_ids) == 2
    assert thinking_found


def test_timestamps_are_iso_not_raw_epoch():
    convs = list(devin_cli.parse(CLI_DIR / "real_session.jsonl"))
    conv = convs[0]
    assert conv["started_at"] is not None
    assert "T" in conv["started_at"]  # ISO-8601, not a bare epoch int
    for m in conv["messages"]:
        if m["created_at"] is not None:
            assert "T" in m["created_at"]


def test_project_derived_from_working_directory_basename():
    convs = list(devin_cli.parse(CLI_DIR / "real_session.jsonl"))
    assert convs[0]["project"] == "example-project"  # redacted fixture path basename


def test_ingest_is_idempotent(conn):
    path = CLI_DIR / "real_session.jsonl"
    summary1 = ingest.ingest_path(conn, path)
    assert summary1.totals["inserted"] == 1

    summary2 = ingest.ingest_path(conn, path)
    assert summary2.totals == {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
    assert summary2.files[0].status == "skipped_dup"


def test_ingest_hidden_session_inserts_nothing(conn):
    summary = ingest.ingest_path(conn, CLI_DIR / "hidden_session.jsonl")
    assert summary.totals["inserted"] == 0
    conv_count = conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"]
    assert conv_count == 0

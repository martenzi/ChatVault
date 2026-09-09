from __future__ import annotations

from chatvault import ingest
from chatvault.adapters import antigravity
from tests.conftest import FIXTURES

AG_DIR = FIXTURES / "antigravity"


def test_sniff_accepts_own_schema_tag_and_rejects_other_jsonl():
    assert antigravity.sniff(AG_DIR / "cli_conversation.jsonl") is True
    assert antigravity.sniff(FIXTURES / "cursor" / "real_conversation.jsonl") is False
    assert antigravity.sniff(FIXTURES / "devin_cli" / "real_session.jsonl") is False


def test_empty_conversation_yields_nothing():
    """Report §3: a file with a trajectory_meta row but zero `steps` rows is
    not a real conversation (2 of 23 real CLI files on the investigated
    machine had this shape)."""
    convs = list(antigravity.parse(AG_DIR / "empty_conversation.jsonl"))
    assert convs == []


def test_cli_conversation_maps_step_types_to_expected_blocks():
    convs = list(antigravity.parse(AG_DIR / "cli_conversation.jsonl"))
    assert len(convs) == 1
    conv = convs[0]
    assert conv["native_id"] == "8290abac-0a51-486e-a207-870753b58950"
    assert conv["platform"] == "antigravity"
    assert conv["raw"]["surface"] == "cli"

    block_types = {b["type"] for m in conv["messages"] for b in m["blocks"]}
    assert "text" in block_types  # step_type=14, user message
    assert "tool_use" in block_types


def test_ide_conversation_has_thinking_blocks():
    """Report §4: step_type=15's field 20.3 carries real assistant
    reasoning/prose text when present -- verified on the IDE surface."""
    convs = list(antigravity.parse(AG_DIR / "ide_conversation.jsonl"))
    assert len(convs) == 1
    conv = convs[0]
    assert conv["raw"]["surface"] == "ide"
    thinking_blocks = [b for m in conv["messages"] for b in m["blocks"] if b["type"] == "thinking"]
    assert thinking_blocks
    assert thinking_blocks[0]["text"]


def test_tool_use_name_resolves_without_truncation():
    """Regression: an earlier version of the path reader forced a `.14`
    sub-field dive unconditionally, truncating a plain string field (e.g.
    'run_command' -> 'n_command') when the wrapper didn't apply. The fix
    tries the direct decode first via _maybe_text and only falls back to
    the nested wrapper when the direct decode isn't printable text."""
    convs = list(antigravity.parse(AG_DIR / "cli_conversation.jsonl"))
    conv = convs[0]
    tool_names = {b["tool_name"] for m in conv["messages"] for b in m["blocks"] if b["type"] == "tool_use" and b.get("tool_name")}
    assert "n_command" not in tool_names
    for name in tool_names:
        assert len(name) > 1


def test_no_empty_tool_use_blocks():
    """Regression: step_type=98 (a bookend marker with no resolvable name
    or args) used to produce a blank tool_use block; it should now fall
    through to `unknown` (D7) instead of a confidently-empty tool_use."""
    convs = list(antigravity.parse(AG_DIR / "cli_conversation.jsonl")) + list(
        antigravity.parse(AG_DIR / "ide_conversation.jsonl")
    )
    for conv in convs:
        for m in conv["messages"]:
            for b in m["blocks"]:
                if b["type"] == "tool_use":
                    assert b.get("tool_name") or b["text"]


def test_raw_step_payload_preserved_byte_faithfully():
    """D3/D4: the adapter decodes select fields for `text`, but every
    message's `raw` must carry the complete original step record (base64
    step_payload untouched), not just the extracted subset."""
    convs = list(antigravity.parse(AG_DIR / "cli_conversation.jsonl"))
    conv = convs[0]
    for m in conv["messages"]:
        assert "step_payload" in m["raw"]


def test_ingest_is_idempotent(conn):
    path = AG_DIR / "cli_conversation.jsonl"
    summary1 = ingest.ingest_path(conn, path)
    assert summary1.totals["inserted"] == 1

    summary2 = ingest.ingest_path(conn, path)
    assert summary2.totals == {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
    assert summary2.files[0].status == "skipped_dup"


def test_ingest_empty_conversation_inserts_nothing(conn):
    summary = ingest.ingest_path(conn, AG_DIR / "empty_conversation.jsonl")
    assert summary.totals["inserted"] == 0

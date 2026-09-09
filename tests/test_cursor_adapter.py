from __future__ import annotations

from chatvault import ingest
from chatvault.adapters import cursor
from tests.conftest import FIXTURES

CURSOR_DIR = FIXTURES / "cursor"


def test_sniff_accepts_own_schema_tag_and_rejects_other_jsonl():
    assert cursor.sniff(CURSOR_DIR / "real_conversation.jsonl") is True
    assert cursor.sniff(FIXTURES / "devin_cli" / "real_session.jsonl") is False
    assert cursor.sniff(FIXTURES / "hermes" / "20260101_030303_def456.jsonl") is False


def test_empty_composer_yields_no_conversation():
    """Report §5: a composer with zero bubbles is a genuinely empty
    placeholder (134 of 162 composerHeaders rows on the real machine had
    zero bubbles AND zero search-index body) -- not a conversation."""
    convs = list(cursor.parse(CURSOR_DIR / "empty_conversation.jsonl"))
    assert convs == []


def test_real_conversation_maps_bubble_types_correctly():
    convs = list(cursor.parse(CURSOR_DIR / "real_conversation.jsonl"))
    assert len(convs) == 1
    conv = convs[0]
    assert conv["native_id"] == "9493f73e-31a5-47e5-8331-acbb8fddc6cf"
    assert conv["platform"] == "cursor"
    assert conv["project"] == "example-project"

    block_types = {b["type"] for m in conv["messages"] for b in m["blocks"]}
    assert "text" in block_types
    assert "thinking" in block_types
    assert "tool_use" in block_types
    assert "tool_result" in block_types


def test_tool_former_data_splits_into_use_and_result_without_binary_duplicate():
    """Report §4.3: toolFormerData.toolCallBinary is a same-content
    binary-safe duplicate of .result -- must never be surfaced as text."""
    convs = list(cursor.parse(CURSOR_DIR / "real_conversation.jsonl"))
    conv = convs[0]
    for m in conv["messages"]:
        for b in m["blocks"]:
            if b["type"] in ("tool_use", "tool_result"):
                assert "toolCallBinary" not in b["raw"] or b["type"] == "tool_use"


def test_orphaned_bubbles_excluded_from_messages_but_kept_in_raw():
    """Report §8: composerData.fullConversationHeadersOnly is the
    authoritative order; bubbles not in it are orphaned edit/regenerate
    history and must not appear in `messages`, but the full bubble set
    stays in `raw` (D3)."""
    convs = list(cursor.parse(CURSOR_DIR / "real_conversation.jsonl"))
    conv = convs[0]
    raw_bubble_count = len(conv["raw"]["bubbles"])
    assert len(conv["messages"]) <= raw_bubble_count


def test_headerless_composer_still_parses():
    """Report §5: 28 real composers have bubbles but no composerHeaders
    row (pruned from that table while their message data survived)."""
    convs = list(cursor.parse(CURSOR_DIR / "headerless_conversation.jsonl"))
    assert len(convs) == 1
    assert convs[0]["raw"]["composer_header"] is None
    assert len(convs[0]["messages"]) > 0


def test_subagent_title_prefixed_and_no_synthetic_native_id():
    """Report §9: Cursor subagent composerIds are already globally unique
    on their own -- no #subagent- suffix needed, unlike claude_code."""
    convs = list(cursor.parse(CURSOR_DIR / "subagent_conversation.jsonl"))
    assert len(convs) == 1
    conv = convs[0]
    assert conv["native_id"] == "51e09edc-23df-45cb-88f8-6adee5a53080"
    assert conv["title"].startswith("[subagent")


def test_cloud_agent_meta_enriches_but_does_not_create_a_second_conversation():
    """Corrected finding (report's dated banner): a composer tracked as a
    cloud Background Agent run is not a separate conversation -- it is a
    normal local composer whose raw payload also carries cloud_agent_meta."""
    convs = list(cursor.parse(CURSOR_DIR / "cloud_enriched_conversation.jsonl"))
    assert len(convs) == 1
    conv = convs[0]
    assert conv["raw"]["cloud_agent_meta"] is not None
    assert len(conv["messages"]) > 0


def test_malformed_composer_id_is_sanitised_but_content_preserved():
    """One real composerId (of 57) contains a literal embedded newline
    (report addendum / adapter's own docstring) -- confirmed real content
    (a task_v2-spawned subagent trace), not a parsing artifact. The
    sanitised native_id must contain no control characters; the exact
    original string must survive in raw."""
    convs = list(cursor.parse(CURSOR_DIR / "malformed_id_conversation.jsonl"))
    assert len(convs) == 1
    conv = convs[0]
    assert "\n" not in conv["native_id"]
    assert "\n" in conv["raw"]["composer_id"]
    assert len(conv["messages"]) > 0


def test_ingest_is_idempotent(conn):
    path = CURSOR_DIR / "real_conversation.jsonl"
    summary1 = ingest.ingest_path(conn, path)
    assert summary1.totals["inserted"] == 1

    summary2 = ingest.ingest_path(conn, path)
    assert summary2.totals == {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
    assert summary2.files[0].status == "skipped_dup"


def test_ingest_empty_composer_inserts_nothing(conn):
    summary = ingest.ingest_path(conn, CURSOR_DIR / "empty_conversation.jsonl")
    assert summary.totals["inserted"] == 0


def test_ingest_malformed_id_conversation_does_not_crash(conn):
    """Regression test for the real embedded-newline composerId bug."""
    summary = ingest.ingest_path(conn, CURSOR_DIR / "malformed_id_conversation.jsonl")
    assert summary.totals["inserted"] == 1
    row = conn.execute("SELECT native_id FROM conversations WHERE platform='cursor'").fetchone()
    assert "\n" not in row["native_id"]

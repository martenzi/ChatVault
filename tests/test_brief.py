from __future__ import annotations

from chatvault import brief, export, ingest, stash
from tests.conftest import FIXTURES


def _conv_id(conn, platform):
    return conn.execute("SELECT id FROM conversations WHERE platform = ?", (platform,)).fetchone()[0]


def test_brief_refuses_unstashed_conversation(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    conv_id = _conv_id(conn, "claude_code")
    raised = False
    try:
        brief.render_one(conn, conv_id)
    except brief.BriefError:
        raised = True
    assert raised


def test_brief_is_self_contained(conn):
    """Acceptance criterion (spec §8): output carries why, source, vault
    reference, and full prose — readable with no vault access."""
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    conv_id = _conv_id(conn, "claude_code")
    stash.add(conn, conv_id, "the protobuf breakthrough — walking the mapping tree", kind="method")

    out = brief.render_one(conn, conv_id)

    assert out.startswith("# Context:")
    assert "**Why this was saved:** the protobuf breakthrough — walking the mapping tree" in out
    assert "**Source:** claude_code" in out
    assert "**Vault reference:** claude_code:s1" in out
    assert "How do I write a for loop in python?" in out
    assert "Use `for x in range(10):`" in out
    assert "Context pack generated from ChatVault" in out


def test_brief_reuses_lean_profile_no_second_renderer(conn):
    """Acceptance criterion (spec §8): brief calls the lean export profile
    rather than a new renderer — verified by checking the body matches
    export.render_profile(profile='lean') byte-for-byte, not just similar."""
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    conv_id = _conv_id(conn, "claude_code")
    stash.add(conn, conv_id, "checking renderer reuse")

    out = brief.render_one(conn, conv_id)
    row = conn.execute("SELECT * FROM v_conversations WHERE id = ?", (conv_id,)).fetchone()
    expected_body = export.render_profile(conn, row, profile="lean")

    assert expected_body.strip() in out


def test_brief_combines_multiple_picks_into_one_pack(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    ingest.ingest_path(conn, FIXTURES / "generic" / "toolsession.jsonl")
    id1 = _conv_id(conn, "claude_code")
    id2 = _conv_id(conn, "generic")
    stash.add(conn, id1, "reason one")
    stash.add(conn, id2, "reason two")

    combined = brief.render_many(conn, [id1, id2])
    assert combined.count("# Context:") == 2
    assert "reason one" in combined
    assert "reason two" in combined


def test_filename_for_single_and_combined(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    ingest.ingest_path(conn, FIXTURES / "generic" / "toolsession.jsonl")
    id1 = _conv_id(conn, "claude_code")
    id2 = _conv_id(conn, "generic")

    single = brief.filename_for(conn, [id1])
    assert single.startswith("brief--")
    assert single.endswith(".md")
    assert "+" not in single

    combined = brief.filename_for(conn, [id1, id2])
    assert "+1-more" in combined

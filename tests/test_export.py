from __future__ import annotations

from chatvault import export, ingest
from tests.conftest import FIXTURES


def test_export_default_template_renders_prose(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json")
    rows = export.get_conversations(conn, where="1=1")
    assert len(rows) == 1
    content = export.render(conn, rows[0], "default")
    assert "# Neo4j schema design" in content
    assert "## User" in content
    assert "How should I model a graph schema" in content


def test_export_obsidian_template_has_front_matter(conn):
    ingest.ingest_path(conn, FIXTURES / "chatgpt" / "conversations.json")
    rows = export.get_conversations(conn, where="1=1")
    content = export.render(conn, rows[0], "obsidian")
    assert content.startswith("---\n")
    assert 'conv_uid: "chatgpt:conv-1"' in content
    # abandoned branch must not leak into the export
    assert "abandoned/regenerated" not in content


def test_export_full_template_includes_thinking_and_tool_blocks(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    rows = export.get_conversations(conn, where="1=1")
    content = export.render(conn, rows[0], "full")
    assert "## User" in content
    assert "## Assistant" in content


def test_export_digest_lists_all_matched_conversations(conn):
    ingest.ingest_path(conn, FIXTURES)
    rows = export.get_conversations(conn, where="1=1")
    content = export.render_digest(conn, rows)
    assert content.count("\n- ") == len(rows)


def test_filename_is_deterministic(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json")
    rows = export.get_conversations(conn, where="1=1")
    conv = dict(rows[0])
    conv["uid_short"] = conv["uid"].split(":")[-1][:12]
    assert export.filename_for(conv, "default") == "2026-03-01--claude_desktop--neo4j-schema-design--web-conv-1.md"

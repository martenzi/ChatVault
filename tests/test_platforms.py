from __future__ import annotations

from chatvault import ingest
from chatvault.browse import find
from chatvault.platforms import resolve_platform_alias
from tests.conftest import FIXTURES


def test_resolve_platform_alias_expands_known_aliases():
    assert resolve_platform_alias("gpt") == ("chatgpt",)
    assert resolve_platform_alias("claude") == ("claude_desktop", "claude_code")
    assert resolve_platform_alias("ds") == ("deepseek",)
    assert resolve_platform_alias("gem") == ("gemini",)
    assert resolve_platform_alias("wind") == ("windsurf",)


def test_resolve_platform_alias_passes_through_unknown_names():
    assert resolve_platform_alias("chatgpt") == ("chatgpt",)
    assert resolve_platform_alias("typo_platform") == ("typo_platform",)


def test_find_accepts_a_tuple_platform_as_an_in_clause(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json")
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    ingest.ingest_path(conn, FIXTURES / "chatgpt" / "conversations.json")

    rows, total = find(conn, platform=("claude_desktop", "claude_code"), limit=20)
    platforms = {r["platform"] for r in rows}
    assert platforms == {"claude_desktop", "claude_code"}
    assert total == 2

    rows, total = find(conn, platform=("chatgpt",), limit=20)
    assert total == 1
    assert rows[0]["platform"] == "chatgpt"

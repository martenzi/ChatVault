from __future__ import annotations

from chatvault import ingest
from tests.conftest import FIXTURES


def test_subagent_transcript_does_not_collide_with_parent_session(vault, conn):
    """Real ~/.claude/projects/**/*.jsonl data (verified 2026-08-14, not just
    fixtures) showed subagent transcripts carry the SAME sessionId as their
    parent session's own top-level file. Before the fix, ingesting both
    produced a native_id collision -> the loader flagged the second as an
    ambiguous #dup- duplicate, permanently losing the subagent's own real
    content to a review queue. Both files here share sessionId 's1'."""
    parent = FIXTURES / "claude_code" / "session1.jsonl"
    subagent = FIXTURES / "claude_code" / "parent-session" / "subagents" / "agent-sub1.jsonl"

    ingest.ingest_path(conn, parent)
    summary = ingest.ingest_path(conn, subagent)

    assert summary.totals["flagged"] == 0
    assert summary.totals["inserted"] == 1

    rows = conn.execute(
        "SELECT uid, native_id, title FROM conversations WHERE platform = 'claude_code'"
    ).fetchall()
    assert len(rows) == 2
    assert not any("#dup-" in r["uid"] for r in rows)

    subagent_row = next(r for r in rows if r["native_id"] != "s1")
    assert subagent_row["native_id"] == "s1#subagent-agent-sub1"
    assert subagent_row["title"].startswith("[subagent] ")

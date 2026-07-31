"""These tests exercise the SQLite-side row-shaping SQL used to build the Neo4j
batches, without requiring a live Neo4j server (none is assumed to be running
in CI or during local test runs). A fake session just records what it was
asked to MERGE."""

from __future__ import annotations

from chatvault import graph, ingest
from tests.conftest import FIXTURES


class _FakeSession:
    def __init__(self):
        self.calls: list[tuple[str, list[dict]]] = []

    def run(self, cypher, rows=None, **kwargs):
        self.calls.append((cypher, rows or []))
        return self


def test_load_conversations_sql_is_valid_and_produces_expected_rows(conn):
    ingest.ingest_path(conn, FIXTURES)
    session = _FakeSession()

    n = graph._load_conversations(session, conn)
    visible = conn.execute("SELECT COUNT(*) FROM v_conversations").fetchone()[0]
    assert n == visible
    assert session.calls  # at least one MERGE batch was issued


def test_load_models_and_tools_sql_is_valid(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    session = _FakeSession()

    graph._load_conversations(session, conn)
    n_models = graph._load_models(session, conn)
    assert n_models >= 1  # the assistant message used claude-sonnet-5


def test_since_filter_narrows_conversation_rows(conn):
    ingest.ingest_path(conn, FIXTURES)
    session = _FakeSession()

    all_n = graph._load_conversations(session, conn)
    recent_n = graph._load_conversations(session, conn, since="2027-01-01")
    assert recent_n == 0
    assert recent_n < all_n


def test_canned_queries_are_syntactically_plausible():
    for name, cypher in graph.CANNED_QUERIES.items():
        assert "MATCH" in cypher or "RETURN" in cypher, name

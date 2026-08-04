from __future__ import annotations

from chatvault import ingest
from tests.conftest import FIXTURES


def test_conversation_and_message_counts_unchanged_by_attachments_and_projects_migrations(conn):
    """Spec v1.4 acceptance criterion: no regression — migrations 005/006 only
    add new tables/columns, they must not change existing conversation or
    message ingestion counts for a fixture with no attachments/projects."""
    ingest.ingest_path(conn, FIXTURES / "claude_web" / "conversations.json")

    conv_count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    block_count = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]

    # tests/fixtures/claude_web/conversations.json is a single, fixed
    # 1-conversation/2-message/2-block fixture (see test_export.py) — these
    # values must hold exactly the same after 005/006 as they did before.
    assert conv_count == 1
    assert msg_count == 2
    assert block_count == 2


def test_new_tables_exist_and_are_empty_without_attachments_or_projects(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_web" / "conversations.json")
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM blocks WHERE attachment_id IS NOT NULL"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE project_id IS NOT NULL"
    ).fetchone()[0] == 0


def test_migrations_005_and_006_are_recorded(conn):
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert "005_attachments.sql" in versions
    assert "006_projects.sql" in versions

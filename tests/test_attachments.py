from __future__ import annotations

from chatvault import attachments, ingest, loader
from chatvault.adapters.base import make_attachment, make_block, make_conversation, make_message
from tests.conftest import FIXTURES


def _ingest_att_fixture(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations_with_attachments.json")


def test_attachment_with_extracted_content_is_stored_as_text(conn):
    _ingest_att_fixture(conn)
    rows = attachments.list_attachments(conn)
    text_rows = [r for r in rows if r["availability"] == "text"]
    assert len(text_rows) == 1
    assert text_rows[0]["filename"] == "timeline.txt"
    assert "franchise operator" in text_rows[0]["content"]


def test_file_only_attachment_is_stored_as_missing(conn):
    _ingest_att_fixture(conn)
    rows = attachments.list_attachments(conn, missing=True)
    assert len(rows) == 1
    assert rows[0]["filename"] == "statement.pdf"
    assert rows[0]["content"] is None


def test_block_attachment_id_links_back_to_the_attachment_row(conn):
    _ingest_att_fixture(conn)
    row = conn.execute(
        "SELECT b.attachment_id, a.filename FROM blocks b JOIN attachments a ON a.id = b.attachment_id "
        "WHERE b.type = 'attachment' AND a.availability = 'text'"
    ).fetchone()
    assert row is not None
    assert row["filename"] == "timeline.txt"


def test_attachment_search_finds_distinctive_phrase(conn):
    _ingest_att_fixture(conn)
    results = attachments.search(conn, '"franchise operator"')
    assert len(results) == 1
    assert results[0]["filename"] == "timeline.txt"


def test_redact_empties_content_sets_missing_keeps_row_and_link(conn):
    _ingest_att_fixture(conn)
    row = conn.execute("SELECT id FROM attachments WHERE availability = 'text'").fetchone()
    aid = row["id"]

    n = attachments.redact(conn, attachment_id=aid, confirm=True)
    assert n == 1

    after = attachments.show(conn, aid)
    assert after["content"] is None
    assert after["availability"] == "missing"

    # the row and its link to the message both still exist
    link = conn.execute(
        "SELECT 1 FROM attachment_links WHERE attachment_id = ? AND target_type = 'message'", (aid,)
    ).fetchone()
    assert link is not None
    block = conn.execute("SELECT 1 FROM blocks WHERE attachment_id = ?", (aid,)).fetchone()
    assert block is not None


def test_redact_without_confirm_refuses(conn):
    _ingest_att_fixture(conn)
    row = conn.execute("SELECT id FROM attachments WHERE availability = 'text'").fetchone()
    try:
        attachments.redact(conn, attachment_id=row["id"], confirm=False)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_purge_requires_confirm(vault, conn):
    _ingest_att_fixture(conn)
    row = conn.execute("SELECT id FROM attachments LIMIT 1").fetchone()
    try:
        attachments.purge(conn, vault, attachment_id=row["id"], confirm=False)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_purge_takes_backup_first_logs_and_deletes(vault, conn):
    _ingest_att_fixture(conn)
    row = conn.execute("SELECT id FROM attachments WHERE availability = 'text'").fetchone()
    aid = row["id"]

    backups_before = list((vault.db_dir / "backups").glob("*.zst")) if (vault.db_dir / "backups").exists() else []

    n = attachments.purge(conn, vault, attachment_id=aid, confirm=True)
    assert n == 1

    backups_after = list((vault.db_dir / "backups").glob("*"))
    assert len(backups_after) > len(backups_before)

    log_row = conn.execute(
        "SELECT * FROM exclusion_log WHERE target_type = 'attachment' AND target_id = ? AND action = 'purge'", (aid,)
    ).fetchone()
    assert log_row is not None

    assert attachments.show(conn, aid) is None
    # the block that referenced it must not be left dangling
    dangling = conn.execute("SELECT attachment_id FROM blocks WHERE attachment_id = ?", (aid,)).fetchall()
    assert dangling == []


def test_exclude_hides_from_v_attachments_reversibly(conn):
    _ingest_att_fixture(conn)
    row = conn.execute("SELECT id FROM attachments WHERE availability = 'text'").fetchone()
    aid = row["id"]

    attachments.exclude(conn, attachment_id=aid, reason="testing")
    assert attachments.list_attachments(conn) == [] or all(r["id"] != aid for r in attachments.list_attachments(conn))

    attachments.restore(conn, attachment_id=aid)
    assert any(r["id"] == aid for r in attachments.list_attachments(conn))


def test_reingesting_the_same_file_does_not_duplicate_attachment_rows(conn):
    _ingest_att_fixture(conn)
    # source_files dedup by sha256 means this is a no-op re-ingest
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations_with_attachments.json")
    text_rows = [r for r in attachments.list_attachments(conn) if r["availability"] == "text"]
    assert len(text_rows) == 1


def test_identical_attachment_across_two_conversations_dedups_to_one_row(conn):
    """Same content+filename appearing in two different conversations (e.g.
    the same doc re-uploaded elsewhere) gets one attachments row and two
    links — this is why the acceptance-criteria 'count' for v1.4 is measured
    as attachment *occurrences* (links/blocks), not deduped table rows."""
    shared = make_attachment(filename="shared.txt", content="identical content", availability="text")

    for native_id in ("conv-a", "conv-b"):
        cir = make_conversation(
            platform="synthetic", native_id=native_id, title=f"conv {native_id}", project=None,
            started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:00",
            source_path="synthetic", raw={},
            messages=[
                make_message(
                    0, "user", None, "2026-01-01T00:00:00",
                    [make_block(0, "attachment", "identical content", raw={}, attachment=dict(shared))],
                    raw={},
                )
            ],
        )
        loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()

    text_rows = [r for r in attachments.list_attachments(conn) if r["availability"] == "text"]
    assert len(text_rows) == 1
    links = conn.execute(
        "SELECT COUNT(*) FROM attachment_links WHERE attachment_id = ?", (text_rows[0]["id"],)
    ).fetchone()[0]
    assert links == 2

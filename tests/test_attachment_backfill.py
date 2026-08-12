from __future__ import annotations

import copy
from datetime import datetime, timezone

from chatvault import attachments, hashing, ingest, loader
from tests.conftest import FIXTURES


def _parse_first(mod, path):
    return next(mod.parse(path))


def _seed_as_already_ingested_under_old_adapter(conn, path):
    """Simulate the real production scenario: this file was already ingested
    by ingest_path (a source_files row exists with status='ok') back when the
    adapter didn't extract attachment metadata yet. Returns the conv_id."""
    from chatvault.adapters import claude_desktop

    sha = hashing.file_sha256(path)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, kind, last_seen_at, root_dir, first_seen_at)
        VALUES (?, ?, 'claude_desktop', 'claude_desktop', ?, ?, 'ok', 'frozen', ?, ?, ?)
        """,
        (str(path), sha, path.stat().st_size, now, now, str(path.parent), now),
    )
    source_file_id = cur.lastrowid

    cir = _parse_first(claude_desktop, path)
    old_cir = copy.deepcopy(cir)
    for msg in old_cir["messages"]:
        for block in msg["blocks"]:
            block["attachment"] = None
    result = loader.load_conversation(conn, old_cir, source_file_id=source_file_id)
    conn.commit()
    return result.conv_id


def test_reingest_without_reprocess_does_not_backfill(conn):
    """The default path (no --reprocess): file-level sha256 dedup skips the
    file entirely on a second ingest, so nothing changes. This is the
    baseline the --reprocess flag exists to work around."""
    path = FIXTURES / "claude_desktop" / "conversations_with_attachments.json"
    _seed_as_already_ingested_under_old_adapter(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0

    summary = ingest.ingest_path(conn, path)
    assert summary.files[0].status == "skipped_dup"
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


def test_backfill_attaches_metadata_to_an_already_ingested_conversation_without_duplicating(conn):
    """Simulates the real production scenario: a conversation was ingested
    under the pre-v1.4 adapter (blocks with type='attachment' but no
    `attachment` payload, since that field didn't exist yet), and the adapter
    is fixed afterward. Re-ingesting with --reprocess must attach the new
    metadata in place — same block rows, same conv_id, no duplicate
    conversations/messages/blocks — never by content_hash alone, since the
    hash is unchanged (text/type/tool_name didn't change)."""
    from chatvault.adapters import claude_desktop

    path = FIXTURES / "claude_desktop" / "conversations_with_attachments.json"
    cir = _parse_first(claude_desktop, path)

    # strip the attachment payloads to simulate the OLD adapter's output —
    # same text/type, no metadata — then load it as the "already ingested"
    # baseline the way ingest_path would have, pre-fix.
    old_cir = copy.deepcopy(cir)
    for msg in old_cir["messages"]:
        for block in msg["blocks"]:
            block["attachment"] = None
    result = loader.load_conversation(conn, old_cir, source_file_id=None)
    conn.commit()
    assert result.action == "inserted"

    conv_id = result.conv_id
    conv_count_before = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    msg_count_before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    block_count_before = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0

    # now load the REAL (fixed-adapter) cir for the same conversation —
    # content_hash is identical (text/type/tool_name unchanged), so this must
    # take the noop path and backfill, not insert a second conversation.
    result2 = loader.load_conversation(conn, cir, source_file_id=None)
    assert result2.action == "noop"
    assert result2.conv_id == conv_id
    assert "backfilled" in result2.detail

    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == conv_count_before
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == msg_count_before
    assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == block_count_before

    text_attachments = [r for r in attachments.list_attachments(conn) if r["availability"] == "text"]
    assert len(text_attachments) == 1
    assert text_attachments[0]["filename"] == "timeline.txt"

    block = conn.execute(
        "SELECT attachment_id FROM blocks WHERE conv_id = ? AND type = 'attachment' AND text != ''", (conv_id,)
    ).fetchone()
    assert block["attachment_id"] is not None


def test_backfill_is_idempotent(conn):
    from chatvault.adapters import claude_desktop

    path = FIXTURES / "claude_desktop" / "conversations_with_attachments.json"
    cir = _parse_first(claude_desktop, path)

    old_cir = copy.deepcopy(cir)
    for msg in old_cir["messages"]:
        for block in msg["blocks"]:
            block["attachment"] = None
    loader.load_conversation(conn, old_cir, source_file_id=None)
    conn.commit()

    loader.load_conversation(conn, cir, source_file_id=None)  # first backfill
    n_after_first = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    n_links_after_first = conn.execute("SELECT COUNT(*) FROM attachment_links").fetchone()[0]

    result3 = loader.load_conversation(conn, cir, source_file_id=None)  # run again
    assert "backfilled" not in result3.detail  # nothing left to backfill

    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == n_after_first
    assert conn.execute("SELECT COUNT(*) FROM attachment_links").fetchone()[0] == n_links_after_first


def test_cli_reprocess_flag_triggers_backfill_end_to_end(conn):
    """Same scenario via the public ingest_path/--reprocess surface rather
    than calling loader.load_conversation directly — this is exactly what
    running `chatvault ingest <path> --reprocess` does in production."""
    path = FIXTURES / "claude_desktop" / "conversations_with_attachments.json"
    _seed_as_already_ingested_under_old_adapter(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0

    # a plain re-ingest (no --reprocess) is a no-op at the file level
    summary = ingest.ingest_path(conn, path)
    assert summary.files[0].status == "skipped_dup"
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0

    # --reprocess bypasses the file-level dedup, hits load_conversation's
    # noop-and-backfill path
    summary2 = ingest.ingest_path(conn, path, reprocess=True)
    assert summary2.totals["noop"] == 1
    # the fixture has 2 attachment-shaped blocks (one with text, one file-only)
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1

from __future__ import annotations

from chatvault import loader
from chatvault.adapters.base import make_block, make_conversation, make_message


def _cir(native_id, title, started_at, text):
    return make_conversation(
        platform="synthetic", native_id=native_id, title=title, project=None,
        started_at=started_at, ended_at=started_at, source_path="synthetic", raw={},
        messages=[make_message(0, "user", None, started_at, [make_block(0, "text", text, raw={})], raw={})],
    )


def _load(conn, cir):
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result


def test_third_identical_duplicate_does_not_collide_on_alt_uid(conn):
    """Real ~/Library/Application Support/Claude/local-agent-mode-sessions data
    (found 2026-09-07) showed sandboxed Claude Desktop coding-agent sessions can
    carry byte-identical duplicate transcripts across more than two source
    files sharing the same native_id. Before the fix, the second ambiguous
    duplicate got uid "...#dup-<hash8>"; a THIRD file with the exact same
    native_id + content produced the identical alt_uid string and crashed the
    real ingest with `IntegrityError: UNIQUE constraint failed: conversations.uid`.
    This reproduces that with three loads of the same native_id: one original,
    then the same non-continuation duplicate content twice."""
    original = _load(conn, _cir("x1", "original", "2026-01-01T00:00:00", "hello"))
    assert original.action == "inserted"

    dup1 = _load(conn, _cir("x1", "dup", "2026-01-01T00:00:00", "something unrelated"))
    assert dup1.action == "flagged_duplicate"

    # Same native_id, byte-identical content to dup1 — must not crash, and
    # must land on a distinct uid rather than colliding with dup1's.
    dup2 = _load(conn, _cir("x1", "dup", "2026-01-01T00:00:00", "something unrelated"))
    assert dup2.action == "flagged_duplicate"
    assert dup2.uid != dup1.uid
    assert dup2.uid.startswith(dup1.uid)  # deterministic base, disambiguated suffix

    rows = conn.execute("SELECT uid FROM conversations WHERE platform = 'synthetic'").fetchall()
    assert len(rows) == 3
    assert len({r["uid"] for r in rows}) == 3  # all distinct, no collision


def test_reprocess_updates_content_in_place_and_preserves_identity_and_raw_json(conn):
    original = _cir(None, "original", "2026-01-01T00:00:00", "")
    original["messages"][0]["blocks"][0]["type"] = "unknown"
    loaded = _load(conn, original)
    before = conn.execute(
        """
        SELECT c.id AS conv_id, c.uid, m.id AS msg_id, b.id AS block_id, b.raw_json
        FROM conversations c JOIN messages m ON m.conv_id = c.id JOIN blocks b ON b.msg_id = m.id
        WHERE c.id = ?
        """,
        (loaded.conv_id,),
    ).fetchone()

    improved = _cir(None, "improved", "2026-01-01T00:00:00", "recovered text")
    result = loader.reprocess_conversation(conn, improved, source_file_id=None, position=0)
    conn.commit()

    after = conn.execute(
        """
        SELECT c.id AS conv_id, c.uid, c.title, m.id AS msg_id, b.id AS block_id,
               b.type, b.text, b.raw_json
        FROM conversations c JOIN messages m ON m.conv_id = c.id JOIN blocks b ON b.msg_id = m.id
        WHERE c.id = ?
        """,
        (loaded.conv_id,),
    ).fetchone()
    assert result.action == "reprocessed"
    assert (after["conv_id"], after["uid"], after["msg_id"], after["block_id"]) == (
        before["conv_id"], before["uid"], before["msg_id"], before["block_id"]
    )
    assert after["title"] == "improved"
    assert (after["type"], after["text"]) == ("text", "recovered text")
    assert after["raw_json"] == before["raw_json"]


def test_reprocess_expands_one_opaque_block_into_typed_blocks(conn):
    original = _cir("x1", "original", "2026-01-01T00:00:00", "")
    original["messages"][0]["blocks"][0]["type"] = "unknown"
    loaded = _load(conn, original)
    old_block = conn.execute("SELECT id, raw_json FROM blocks WHERE conv_id = ?", (loaded.conv_id,)).fetchone()

    improved = _cir("x1", "improved", "2026-01-01T00:00:00", "reasoning")
    improved["messages"][0]["blocks"][0]["type"] = "thinking"
    improved["messages"][0]["blocks"].append(make_block(1, "text", "answer", raw={"source": "same record"}))
    result = loader.reprocess_conversation(conn, improved, source_file_id=None, position=0)
    conn.commit()

    rows = conn.execute(
        "SELECT id, idx, type, text, raw_json FROM blocks WHERE conv_id = ? ORDER BY idx",
        (loaded.conv_id,),
    ).fetchall()
    assert result.action == "reprocessed"
    assert [(row["idx"], row["type"], row["text"]) for row in rows] == [
        (0, "thinking", "reasoning"), (1, "text", "answer")
    ]
    assert rows[0]["id"] == old_block["id"]
    assert rows[0]["raw_json"] == old_block["raw_json"]
    assert rows[1]["raw_json"] == '{"source":"same record"}'


def test_reprocess_rejects_structural_changes_without_modifying_rows(conn):
    loaded = _load(conn, _cir("x1", "original", "2026-01-01T00:00:00", "hello"))
    changed = _cir("x1", "changed", "2026-01-01T00:00:00", "hello")
    changed["messages"].append(make_message(1, "assistant", None, None, [], raw={}))

    try:
        loader.reprocess_conversation(conn, changed, source_file_id=None, position=0)
    except ValueError as exc:
        assert "message structure" in str(exc)
    else:
        raise AssertionError("structural reprocess must fail")

    row = conn.execute("SELECT title, message_count FROM conversations WHERE id = ?", (loaded.conv_id,)).fetchone()
    assert (row["title"], row["message_count"]) == ("original", 1)

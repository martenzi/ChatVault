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

    # Same native_id, byte-identical content to dup1 — the original crash was a
    # UNIQUE collision on the alt_uid. Since 2026-10-12 it is a no-op instead:
    # byte-identical content is never stored twice (snapshot_duplication report).
    dup2 = _load(conn, _cir("x1", "dup", "2026-01-01T00:00:00", "something unrelated"))
    assert dup2.action == "noop"
    assert dup2.uid == dup1.uid
    dup3 = _load(conn, _cir("x1", "dup", "2026-01-01T00:00:00", "something unrelated"))
    assert dup3.action == "noop"

    rows = conn.execute("SELECT uid FROM conversations WHERE platform = 'synthetic'").fetchall()
    assert len(rows) == 2


def _grow(native_id, texts):
    msgs = [
        make_message(i, "user", None, "2026-01-01T00:00:00", [make_block(0, "text", t, raw={})], raw={})
        for i, t in enumerate(texts)
    ]
    return make_conversation(
        platform="synthetic", native_id=native_id, title="t", project=None,
        started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:00",
        source_path="synthetic", raw={}, messages=msgs,
    )


def _live(conn):
    return conn.execute(
        "SELECT message_count FROM conversations WHERE platform='synthetic' AND superseded_by IS NULL"
    ).fetchall()


def test_older_snapshot_arriving_after_longer_is_hidden_not_duplicated(conn):
    """Real case (native_id 7df6576a…): 89/211/218/379/470/586/869-message
    snapshots of one growing transcript, ingested out of order. Shorter ones
    arriving after a longer one used to become live '#dup-' rows."""
    long = _load(conn, _grow("g1", ["a", "b", "c", "d"]))
    assert long.action == "inserted"
    short = _load(conn, _grow("g1", ["a", "b"]))
    assert short.action == "snapshot"
    assert "#snapshot-" in short.uid
    assert [r["message_count"] for r in _live(conn)] == [4]
    row = conn.execute("SELECT superseded_by FROM conversations WHERE id = ?", (short.conv_id,)).fetchone()
    assert row["superseded_by"] == long.conv_id
    # Re-ingest of the same snapshot is a no-op, not a second copy.
    assert _load(conn, _grow("g1", ["a", "b"])).action == "noop"
    assert conn.execute("SELECT COUNT(*) FROM conversations WHERE platform='synthetic'").fetchone()[0] == 2


def test_growing_transcript_supersedes_all_earlier_snapshots_in_any_order(conn):
    _load(conn, _grow("g2", ["a", "b"]))
    _load(conn, _grow("g2", ["a", "b", "c", "d"]))
    _load(conn, _grow("g2", ["a"]))  # older, arrives late
    _load(conn, _grow("g2", ["a", "b", "c", "d", "e"]))
    assert [r["message_count"] for r in _live(conn)] == [5]
    assert conn.execute("SELECT COUNT(*) FROM conversations WHERE platform='synthetic'").fetchone()[0] == 4


def test_divergent_fork_is_flagged_with_review_tag(conn):
    _load(conn, _grow("g3", ["a", "b"]))
    forked = _load(conn, _grow("g3", ["a", "X", "Y"]))
    assert forked.action == "flagged_duplicate"
    tag = conn.execute(
        "SELECT t.name FROM tag_links l JOIN tags t ON t.id = l.tag_id WHERE l.target_id = ?", (forked.conv_id,)
    ).fetchone()
    assert tag["name"] == "review/duplicate-ambiguous"


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


def test_backfill_snapshots_supersedes_prefixes_and_identicals_and_tags_forks(conn):
    from chatvault import dedupe

    def raw_row(native_id, uid, texts):
        c = _grow(native_id, texts)
        h = loader._compute_hashes(c)
        return loader._insert_conversation_row(
            conn, c, uid, h["content_hash"], h["head_hash"], None, "2026-01-01T00:00:00", h
        )

    # Pre-fix state: every ingest order left live rows behind.
    raw_row("b1", "synthetic:b1", ["a", "b", "c"])
    short = raw_row("b1", "synthetic:b1#dup-aaaa", ["a"])
    ident = raw_row("b1", "synthetic:b1#dup-bbbb", ["a", "b", "c"])
    raw_row("b2", "synthetic:b2", ["a", "b"])
    raw_row("b2", "synthetic:b2#dup-cccc", ["a", "Z"])
    conn.commit()

    dry = dedupe.backfill_snapshots(conn, apply=False)
    assert (dry["identical"], dry["prefix"], dry["ambiguous_families"]) == (1, 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM conversations WHERE superseded_by IS NOT NULL").fetchone()[0] == 0

    done = dedupe.backfill_snapshots(conn, apply=True)
    assert done == dry
    sup = {r["id"] for r in conn.execute("SELECT id FROM conversations WHERE superseded_by IS NOT NULL")}
    assert sup == {short, ident}
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 2  # both live b2 rows
    # Idempotent.
    again = dedupe.backfill_snapshots(conn, apply=True)
    assert again["identical"] == 0 and again["prefix"] == 0

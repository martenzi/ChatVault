from __future__ import annotations

from chatvault import curation, curation_io, ingest
from chatvault.db import connect, migrate
from tests.conftest import FIXTURES


def _seed_curation(conn, config):
    """Ingest fixtures and add one of each entity type, including a
    block_class-style bulk exclusion."""
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json", config=config)
    ingest.ingest_path(conn, FIXTURES / "chatgpt" / "conversations.json", config=config)

    curation.tag_add(conn, "important", "1=1", "conversation")
    curation.note_add(conn, "conversation", 1, "this one matters")
    curation.view_save(conn, "my-view", "SELECT id FROM v_conversations WHERE platform='claude_desktop'")

    block_hash = conn.execute("SELECT content_hash FROM blocks LIMIT 1").fetchone()[0]
    curation.exclude(conn, "block", f"content_hash='{block_hash}'", reason="test bulk exclusion")


def test_export_wipe_reingest_import_restores_everything(vault, conn):
    _seed_curation(conn, vault)
    data = curation_io.export_curation(conn)
    conn.close()

    vault.db_path.unlink()
    migrate(vault)
    new_conn = connect(vault)
    try:
        ingest.ingest_path(new_conn, FIXTURES / "claude_desktop" / "conversations.json", config=vault)
        ingest.ingest_path(new_conn, FIXTURES / "chatgpt" / "conversations.json", config=vault)

        summary = curation_io.import_curation(new_conn, data)
        assert summary.unresolved == []

        assert curation.tag_list(new_conn)[0]["name"] == "important"
        assert curation.view_list(new_conn)[0]["name"] == "my-view"
        notes = new_conn.execute("SELECT note FROM annotations").fetchall()
        assert notes[0]["note"] == "this one matters"
        excluded_blocks = new_conn.execute("SELECT COUNT(*) FROM blocks WHERE excluded = 1").fetchone()[0]
        assert excluded_blocks >= 1
    finally:
        new_conn.close()


def test_import_is_idempotent(vault, conn):
    _seed_curation(conn, vault)
    data = curation_io.export_curation(conn)

    curation_io.import_curation(conn, data)
    summary2 = curation_io.import_curation(conn, data)

    for entity, c in summary2.counts.items():
        assert c["added"] == 0, f"{entity} should report 0 added on repeat import"


def test_import_preserves_post_export_annotation(vault, conn):
    _seed_curation(conn, vault)
    data = curation_io.export_curation(conn)

    curation.note_add(conn, "conversation", 1, "a brand new note added after export")
    before_count = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]

    curation_io.import_curation(conn, data)

    after_count = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
    notes = {r[0] for r in conn.execute("SELECT note FROM annotations").fetchall()}
    assert "a brand new note added after export" in notes
    assert after_count == before_count  # the old export had nothing new to add


def test_import_content_drift_and_missing_target_are_reported_not_fatal(vault, conn):
    _seed_curation(conn, vault)
    data = curation_io.export_curation(conn)

    # hand-corrupt: reference a non-existent msg_idx and a wrong content_hash
    data["annotations"].append(
        {"target": {"kind": "message", "conv_uid": "claude_desktop:web-conv-1", "msg_idx": 999, "content_hash": "x"}, "note": "ghost", "created_at": "2026-01-01T00:00:00Z"}
    )
    real_msg_target = dict(data["annotations"][0]["target"])
    if real_msg_target["kind"] != "message":
        # fall back to constructing one directly against a real message
        row = conn.execute("SELECT c.uid, m.idx, m.content_hash FROM messages m JOIN conversations c ON c.id=m.conv_id LIMIT 1").fetchone()
        real_msg_target = {"kind": "message", "conv_uid": row["uid"], "msg_idx": row["idx"], "content_hash": "deliberately-wrong-hash"}
    else:
        real_msg_target["content_hash"] = "deliberately-wrong-hash"
    data["annotations"].append({"target": real_msg_target, "note": "drifted", "created_at": "2026-01-01T00:00:00Z"})

    summary = curation_io.import_curation(conn, data)
    reasons = {u["reason"] for u in summary.unresolved}
    assert "missing_target" in reasons
    assert "content_drift" in reasons

    out_path = curation_io.write_unresolved(vault.exports_dir / "curation.json", summary.unresolved)
    assert out_path.exists()


def test_dry_run_changes_nothing(vault, conn):
    _seed_curation(conn, vault)
    data = curation_io.export_curation(conn)
    conn.close()

    vault.db_path.unlink()
    migrate(vault)
    new_conn = connect(vault)
    try:
        ingest.ingest_path(new_conn, FIXTURES / "claude_desktop" / "conversations.json", config=vault)
        ingest.ingest_path(new_conn, FIXTURES / "chatgpt" / "conversations.json", config=vault)

        before = new_conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        curation_io.import_curation(new_conn, data, dry_run=True)
        after = new_conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        # `before` need not be 0: ingest's own post-load tagging passes (e.g.
        # chatvault.tags.flag_shape_unclear(), spec 50_SPEC_ingestion_fidelity_v1
        # §4.4/§7.6) may legitimately create tags for the ingested fixtures.
        # What this test actually asserts is that the *dry run* changes
        # nothing relative to whatever ingest already produced.
        assert before == after
    finally:
        new_conn.close()


def test_block_class_exclusion_reapplies_to_newly_ingested_blocks(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json", config=vault)
    block_hash = conn.execute("SELECT content_hash FROM blocks LIMIT 1").fetchone()[0]
    curation.exclude(conn, "block", f"content_hash='{block_hash}'", reason="boilerplate")
    data = curation_io.export_curation(conn)

    block_class_entries = [e for e in data["exclusions"] if e["target"]["kind"] == "block_class"]
    assert len(block_class_entries) == 1
    assert block_class_entries[0]["target"]["content_hash"] == block_hash

    # ingest a second, unrelated fixture — none of its blocks share that hash,
    # so this just proves the mechanism doesn't error when nothing new matches
    ingest.ingest_path(conn, FIXTURES / "chatgpt" / "conversations.json", config=vault)
    summary = curation_io.import_curation(conn, data)
    assert summary.counts["exclusions"]["unchanged"] >= 1


def test_on_conflict_overwrite_vs_skip(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json", config=vault)
    curation.tag_add(conn, "important", "1=1", "conversation")
    data = curation_io.export_curation(conn)
    data["tags"][0]["colour"] = "#ff0000"

    curation_io.import_curation(conn, data, on_conflict="skip")
    assert conn.execute("SELECT colour FROM tags WHERE name = 'important'").fetchone()["colour"] is None

    curation_io.import_curation(conn, data, on_conflict="overwrite")
    assert conn.execute("SELECT colour FROM tags WHERE name = 'important'").fetchone()["colour"] == "#ff0000"


def test_replace_annotations_requires_confirm_and_logs_deletions(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json", config=vault)
    curation.note_add(conn, "conversation", 1, "old note")
    data = curation_io.export_curation(conn)
    uid = conn.execute("SELECT uid FROM conversations LIMIT 1").fetchone()["uid"]

    raised = False
    try:
        curation_io.replace_annotations(conn, vault, data, uid, confirm=False)
    except ValueError:
        raised = True
    assert raised

    summary = curation_io.replace_annotations(conn, vault, data, uid, confirm=True)
    assert summary.deleted == 1
    assert summary.added == 1

    deletions = conn.execute("SELECT COUNT(*) FROM exclusion_log WHERE target_type = 'annotation'").fetchone()[0]
    assert deletions == 1

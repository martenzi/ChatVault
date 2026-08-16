from __future__ import annotations

from chatvault import brain, brain_db, export, ingest, stash
from tests.conftest import FIXTURES


def _conv(conn, platform="claude_code"):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    row = conn.execute("SELECT * FROM v_conversations WHERE platform = ?", (platform,)).fetchone()
    return row


def _vault_ro(vault):
    return brain_db.connect_vault_readonly(vault.db_path)


def test_connect_creates_schema_and_is_idempotent(vault):
    b1 = brain_db.connect(vault)
    tables = {r[0] for r in b1.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"items", "item_links", "tags", "tag_links"}.issubset(tables)
    b1.close()

    # reconnecting must not error (CREATE ... IF NOT EXISTS everywhere)
    b2 = brain_db.connect(vault)
    b2.execute("SELECT COUNT(*) FROM items")
    b2.close()


def test_brain_is_self_sufficient_without_the_vault(vault):
    """Acceptance criterion (spec §8): brain commands that don't explicitly
    promote work with the vault absent or unreachable. Simulated here by
    deleting db/chatvault.sqlite outright before touching the brain — add/
    list/search never open it."""
    vault.db_path.unlink(missing_ok=True)
    assert not vault.db_path.exists()

    brain_conn = brain_db.connect(vault)
    brain.add(brain_conn, "a manual note", why="no vault needed for this")
    entries = brain.list_all(brain_conn)
    assert len(entries) == 1
    results = brain.search(brain_conn, "manual")
    assert len(results) == 1
    assert not vault.db_path.exists()  # still never (re)created
    brain_conn.close()


def test_promote_requires_why(vault, conn):
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    raised = False
    try:
        brain.promote(brain_conn, vault_ro, conn, row["uid"], "")
    except brain.BrainError:
        raised = True
    assert raised
    vault_ro.close()


def test_promote_requires_valid_kind(vault, conn):
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    raised = False
    try:
        brain.promote(brain_conn, vault_ro, conn, row["uid"], "a reason", kind="not-a-kind")
    except brain.BrainError:
        raised = True
    assert raised
    vault_ro.close()


def test_promote_unknown_uid_raises(vault, conn):
    _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    raised = False
    try:
        brain.promote(brain_conn, vault_ro, conn, "claude_code:does-not-exist", "a reason")
    except brain.BrainError:
        raised = True
    assert raised
    vault_ro.close()


def test_promote_snapshots_via_lean_profile(vault, conn):
    row = _conv(conn)
    # Rendered BEFORE promote() runs, deliberately: promote() also tags the
    # vault conversation stash/promoted, which render_profile's own template
    # reflects live in its "tags:" line — rendering after would compare
    # against an already-mutated tag set, not the snapshot promote() froze.
    expected_body = export.render_profile(conn, row, profile="lean")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(brain_conn, vault_ro, conn, row["uid"], "the reason", kind="method")
    vault_ro.close()

    item = brain.get_item_row(brain_conn, result["item_id"])
    assert item["body"] == expected_body
    assert item["why"] == "the reason"
    assert item["kind"] == "method"
    assert item["source_uid"] == row["uid"]
    assert item["source_kind"] == "chatvault"


def test_promote_is_idempotent(vault, conn):
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)

    r1 = brain.promote(brain_conn, vault_ro, conn, row["uid"], "first call", kind="method")
    assert r1["action"] == "promoted"
    r2 = brain.promote(brain_conn, vault_ro, conn, row["uid"], "second call, different why", kind="method")
    assert r2["action"] == "already_promoted"
    assert r2["item_id"] == r1["item_id"]

    n = brain_conn.execute("SELECT COUNT(*) FROM items WHERE source_uid = ?", (row["uid"],)).fetchone()[0]
    assert n == 1
    vault_ro.close()


def test_promote_marks_vault_conversation_stash_promoted_without_changing_counts(vault, conn):
    """Acceptance criterion (spec §8): `chatvault stats` (conversation
    counts) identical before and after a brain operation."""
    row = _conv(conn)
    before_count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    brain.promote(brain_conn, vault_ro, conn, row["uid"], "why this matters", kind="method")
    vault_ro.close()

    after_count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    assert before_count == after_count

    tagged = conn.execute(
        """
        SELECT 1 FROM tag_links tl JOIN tags t ON t.id = tl.tag_id
        WHERE tl.target_type = 'conversation' AND tl.target_id = ? AND t.name = 'stash/promoted'
        """,
        (row["id"],),
    ).fetchone()
    assert tagged is not None


def test_promote_without_rw_connection_never_writes_to_vault(vault, conn):
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    brain.promote(brain_conn, vault_ro, None, row["uid"], "read-only promote", kind="method")
    vault_ro.close()

    tagged = conn.execute(
        "SELECT COUNT(*) FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = 'stash/promoted'"
    ).fetchone()[0]
    assert tagged == 0


def test_snapshot_is_frozen_after_source_edit(vault, conn):
    """Acceptance criterion (spec §8): editing the source conversation in
    the vault after promoting does not change the brain item."""
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(brain_conn, vault_ro, conn, row["uid"], "frozen check", kind="method")
    vault_ro.close()

    original_body = brain.get_item_row(brain_conn, result["item_id"])["body"]

    conn.execute("UPDATE conversations SET title = 'a totally different title now' WHERE id = ?", (row["id"],))
    conn.commit()

    unchanged = brain.get_item_row(brain_conn, result["item_id"])
    assert unchanged["body"] == original_body
    assert unchanged["title"] != "a totally different title now"


def test_add_requires_why_and_title(vault):
    brain_conn = brain_db.connect(vault)
    raised = False
    try:
        brain.add(brain_conn, "a title", "")
    except brain.BrainError:
        raised = True
    assert raised

    raised = False
    try:
        brain.add(brain_conn, "", "a why")
    except brain.BrainError:
        raised = True
    assert raised


def test_add_creates_manual_item(vault):
    brain_conn = brain_db.connect(vault)
    result = brain.add(brain_conn, "a fact I know", "because I verified it", kind="fact", body="the body")
    item = brain.get_item_row(brain_conn, result["item_id"])
    assert item["source_kind"] == "manual"
    assert item["source_uid"] is None
    assert item["body"] == "the body"


def test_list_all_ordering_and_filters(vault):
    brain_conn = brain_db.connect(vault)
    brain.add(brain_conn, "first", "why 1", kind="fact")
    brain.add(brain_conn, "second", "why 2", kind="method")
    entries = brain.list_all(brain_conn)
    assert [e.title for e in entries] == ["second", "first"]  # newest first
    assert [e.n for e in entries] == [1, 2]

    only_fact = brain.list_all(brain_conn, kind="fact")
    assert len(only_fact) == 1
    assert only_fact[0].title == "first"


def test_get_entry_out_of_range_raises(vault):
    brain_conn = brain_db.connect(vault)
    brain.add(brain_conn, "only one", "why")
    raised = False
    try:
        brain.get_entry(brain_conn, 2)
    except brain.BrainError:
        raised = True
    assert raised


def test_search_finds_by_content(vault):
    brain_conn = brain_db.connect(vault)
    brain.add(brain_conn, "protobuf breakthrough", "walking the mapping tree", body="root to current_node")
    brain.add(brain_conn, "unrelated", "something else entirely", body="nothing to do with it")
    results = brain.search(brain_conn, "mapping tree")
    assert len(results) == 1
    assert results[0]["title"] == "protobuf breakthrough"


def test_promote_from_stash_uses_stash_why_and_maps_kinds(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    ingest.ingest_path(conn, FIXTURES / "generic" / "toolsession.jsonl")
    ids = [r[0] for r in conn.execute("SELECT id FROM conversations ORDER BY id").fetchall()]
    stash.add(conn, ids[0], "stash why one", kind="reference")  # maps to 'fact'
    stash.add(conn, ids[1], "stash why two", kind="thread")  # maps to 'conversation'

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    results = brain.promote_from_stash(brain_conn, vault_ro, conn)
    vault_ro.close()

    assert len(results) == 2
    kinds = {r["kind"] for r in [brain.get_item_row(brain_conn, r["item_id"]) for r in results]}
    assert kinds == {"fact", "conversation"}
    whys = {brain.get_item_row(brain_conn, r["item_id"])["why"] for r in results}
    assert whys == {"stash why one", "stash why two"}


def test_promote_from_stash_skips_already_promoted(vault, conn):
    row = _conv(conn)
    stash.add(conn, row["id"], "stash reason", kind="method")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    first = brain.promote_from_stash(brain_conn, vault_ro, conn)
    assert len(first) == 1
    second = brain.promote_from_stash(brain_conn, vault_ro, conn)
    vault_ro.close()
    assert second == []

    n = brain_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert n == 1

from __future__ import annotations

import hashlib
import json

import pytest

from chatvault import brain, brain_db, export, ingest, stash, tags
from chatvault.brain import embeddings as brain_embeddings
from tests.conftest import FIXTURES

# Same fake-embedder philosophy as tests/test_embeddings.py: no Ollama
# dependency in the test suite. Topic keywords map to distinct axes of a
# deterministic vector, so cosine distance produces a predictable ranking
# without a real model. Real-model verification lives in STATUS.md's
# manual/production pass, not here.
_TOPICS = ["npm", "docker", "medical", "finance"]


def _fake_embed(text: str, model: str = "nomic-embed-text") -> list[float]:
    text_l = text.lower()
    vec = [0.0] * 768
    for i, topic in enumerate(_TOPICS):
        vec[i] = 1.0 if topic in text_l else 0.0
    h = hashlib.sha256(text.encode("utf-8")).digest()
    for i in range(len(_TOPICS), 768):
        vec[i] = (h[i % len(h)] / 255.0) * 0.01
    return vec


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    monkeypatch.setattr(brain_embeddings, "embed_text", _fake_embed)


def _conv(conn, platform="claude_code"):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    row = conn.execute("SELECT * FROM v_conversations WHERE platform = ?", (platform,)).fetchone()
    return row


def _vault_ro(vault):
    return brain_db.connect_vault_readonly(vault.db_path)


def test_connect_creates_schema_and_is_idempotent(vault):
    b1 = brain_db.connect(vault)
    tables = {r[0] for r in b1.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"items", "item_links", "tags", "tag_links", "kinds", "aliases", "collections", "collection_items", "rel_types", "history", "reviews", "attachments"}.issubset(tables)
    b1.close()

    # reconnecting must not error (idempotent base schema + already-applied migrations)
    b2 = brain_db.connect(vault)
    b2.execute("SELECT COUNT(*) FROM items")
    b2.close()


def test_brain_is_self_sufficient_without_the_vault(vault):
    """Acceptance criterion (spec §7): brain commands that don't explicitly
    promote work with the vault absent or unreachable."""
    vault.db_path.unlink(missing_ok=True)
    assert not vault.db_path.exists()

    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "a manual note", why="no vault needed for this")
    entries = brain.list_entries(brain_conn)
    assert len(entries) == 1
    results = brain.find_entries(brain_conn, "manual")
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
    expected_body = export.render_profile(conn, row, profile="lean")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(brain_conn, vault_ro, conn, row["uid"], "the reason", kind="method")
    vault_ro.close()

    item = brain.get_item_by_id(brain_conn, result["item_id"])
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
    """Acceptance criterion (spec §7): `chatvault stats` (conversation
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
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(brain_conn, vault_ro, conn, row["uid"], "frozen check", kind="method")
    vault_ro.close()

    original_body = brain.get_item_by_id(brain_conn, result["item_id"])["body"]

    conn.execute("UPDATE conversations SET title = 'a totally different title now' WHERE id = ?", (row["id"],))
    conn.commit()

    unchanged = brain.get_item_by_id(brain_conn, result["item_id"])
    assert unchanged["body"] == original_body
    assert unchanged["title"] != "a totally different title now"


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
    kinds = {r["kind"] for r in [brain.get_item_by_id(brain_conn, r["item_id"]) for r in results]}
    assert kinds == {"fact", "conversation"}
    whys = {brain.get_item_by_id(brain_conn, r["item_id"])["why"] for r in results}
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


# ────────────── tag carryover + --profile (docs/51_SPEC §4) ──────────────


def _brain_tags_for_item(brain_conn, item_id):
    rows = brain_conn.execute(
        "SELECT t.name, tl.source FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = ?",
        (item_id,),
    ).fetchall()
    return {r["name"]: r["source"] for r in rows}


def test_promote_carries_regular_tag_unchanged_and_namespaces_curation_tag(vault, conn):
    """§4.2/§4.3 acceptance: a regular tag carries unchanged, a
    curation-namespace tag (review/*) is carried in prefixed `vault/`, and
    nothing is silently dropped."""
    row = _conv(conn)
    tags.tag_add(conn, "tech/sqlite", f"id = {row['id']}")
    tags.tag_add(conn, "review/model-unknown", f"id = {row['id']}")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(brain_conn, vault_ro, conn, row["uid"], "carry tags", kind="method")
    vault_ro.close()

    carried = _brain_tags_for_item(brain_conn, result["item_id"])
    assert carried == {"tech/sqlite": "chatvault", "vault/review/model-unknown": "chatvault"}
    assert set(result["tags_carried"]) == {"tech/sqlite", "vault/review/model-unknown"}
    assert result["tags_dropped"] == []


def test_promote_no_tags_flag_skips_carryover(vault, conn):
    row = _conv(conn)
    tags.tag_add(conn, "tech/sqlite", f"id = {row['id']}")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(brain_conn, vault_ro, conn, row["uid"], "no tags please", kind="method", no_tags=True)
    vault_ro.close()

    assert _brain_tags_for_item(brain_conn, result["item_id"]) == {}
    assert result["tags_carried"] == []


def test_promote_extra_tag_option_adds_manual_tag_alongside_carried(vault, conn):
    row = _conv(conn)
    tags.tag_add(conn, "tech/sqlite", f"id = {row['id']}")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(
        brain_conn, vault_ro, conn, row["uid"], "extra tag", kind="method", extra_tags=["curated"],
    )
    vault_ro.close()

    carried = _brain_tags_for_item(brain_conn, result["item_id"])
    assert carried == {"tech/sqlite": "chatvault", "curated": "manual"}


def test_untag_by_source_reverses_carryover_only(vault, conn):
    """§4.2 point 3: `untag --source chatvault` removes exactly the tags a
    carryover wrote, and nothing else."""
    row = _conv(conn)
    tags.tag_add(conn, "tech/sqlite", f"id = {row['id']}")
    tags.tag_add(conn, "review/model-unknown", f"id = {row['id']}")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(brain_conn, vault_ro, conn, row["uid"], "carry then untag", kind="method")
    vault_ro.close()
    item_id = result["item_id"]

    brain.tag_entry(brain_conn, str(item_id), ["my-own-tag"])
    assert _brain_tags_for_item(brain_conn, item_id) == {
        "tech/sqlite": "chatvault", "vault/review/model-unknown": "chatvault", "my-own-tag": "manual",
    }

    brain.untag_entry(brain_conn, str(item_id), source="chatvault")

    remaining = _brain_tags_for_item(brain_conn, item_id)
    assert remaining == {"my-own-tag": "manual"}


def test_promote_from_stash_also_carries_tags(vault, conn):
    """`stash add` itself writes stash/<kind> and stash/promoted-style tags
    on the vault side (curation namespace, §4.3) — those get carried in
    `vault/`-prefixed right alongside a regular tag, unchanged."""
    row = _conv(conn)
    tags.tag_add(conn, "tech/sqlite", f"id = {row['id']}")
    stash.add(conn, row["id"], "stash reason", kind="method")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    results = brain.promote_from_stash(brain_conn, vault_ro, conn)
    vault_ro.close()

    assert len(results) == 1
    carried = _brain_tags_for_item(brain_conn, results[0]["item_id"])
    assert carried["tech/sqlite"] == "chatvault"
    assert "tech/sqlite" in results[0]["tags_carried"]
    # every other carried tag came from stash's own bookkeeping and must
    # therefore be namespaced under vault/ — never a raw stash/* name.
    for name in carried:
        if name != "tech/sqlite":
            assert name.startswith("vault/"), name


def _make_thinking_and_tool_conv(conn, native_id: str) -> str:
    """A conversation with a thinking block and a tool call — unlike this
    file's plain-text fixtures, `read`/`lean`/`full` profiles actually
    render it differently, so a --profile test has something real to
    measure (mirrors tests/test_export_profiles.py's own synthetic-
    conversation pattern rather than assuming a jsonl fixture happens to
    contain one)."""
    from chatvault import loader
    from chatvault.adapters.base import make_block, make_conversation, make_message

    blocks = [
        make_block(0, "thinking", "a long chain of internal reasoning about the approach", raw={}),
        make_block(1, "tool_use", "x" * 80, raw={}, tool_name="search_docs"),
        make_block(2, "text", "the final answer", raw={}),
    ]
    msgs = [
        make_message(0, "user", None, "2026-01-01T00:00:00", [make_block(0, "text", "the question", raw={})], raw={}),
        make_message(1, "assistant", None, "2026-01-01T00:00:01", blocks, raw={}),
    ]
    cir = make_conversation(
        platform="synthetic", native_id=native_id, title="profile fixture", project=None,
        started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:01",
        source_path="synthetic", raw={}, messages=msgs,
    )
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result.uid


def test_promote_profile_records_which_profile_was_used(vault, conn):
    """§4.4 acceptance: the profile actually used is recorded in source_meta."""
    uid_a = _make_thinking_and_tool_conv(conn, "profile-a")
    uid_b = _make_thinking_and_tool_conv(conn, "profile-b")

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result_read = brain.promote(brain_conn, vault_ro, conn, uid_a, "read profile", kind="method", profile="read")
    result_full = brain.promote(brain_conn, vault_ro, conn, uid_b, "full profile", kind="method", profile="full")
    vault_ro.close()

    meta_read = json.loads(brain.get_item_by_id(brain_conn, result_read["item_id"])["source_meta"])
    meta_full = json.loads(brain.get_item_by_id(brain_conn, result_full["item_id"])["source_meta"])
    assert meta_read["copy_profile"] == "read"
    assert meta_full["copy_profile"] == "full"


def test_promote_profile_full_produces_larger_body_than_read(vault, conn):
    """§4.4/5.6 acceptance: --profile full is strictly larger than
    --profile read for the same conversation (full keeps thinking/tool
    content that read collapses/drops)."""
    uid = _make_thinking_and_tool_conv(conn, "profile-compare")
    row = conn.execute("SELECT * FROM v_conversations WHERE uid = ?", (uid,)).fetchone()
    expected_read = export.render_profile(conn, row, profile="read")
    expected_full = export.render_profile(conn, row, profile="full")
    assert len(expected_full) > len(expected_read)

    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.promote(brain_conn, vault_ro, conn, uid, "profile check", kind="method", profile="full")
    vault_ro.close()

    body = brain.get_item_by_id(brain_conn, result["item_id"])["body"]
    assert body == expected_full
    assert len(body) > len(expected_read)


# ─────────────────────────── entries (v2) ───────────────────────────


def test_add_requires_why_and_title(vault):
    brain_conn = brain_db.connect(vault)
    raised = False
    try:
        brain.add_entry(brain_conn, "a title", "")
    except brain.BrainError:
        raised = True
    assert raised

    raised = False
    try:
        brain.add_entry(brain_conn, "", "a why")
    except brain.BrainError:
        raised = True
    assert raised


def test_add_creates_manual_item(vault):
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "a fact I know", "because I verified it", kind="fact", body="the body")
    item = brain.get_item_by_id(brain_conn, result["item_id"])
    assert item["source_kind"] == "manual"
    assert item["source_uid"] is None
    assert item["body"] == "the body"


def test_add_rejects_unknown_kind(vault):
    brain_conn = brain_db.connect(vault)
    raised = False
    try:
        brain.add_entry(brain_conn, "x", "y", kind="not-a-real-kind")
    except brain.BrainError:
        raised = True
    assert raised


def test_kinds_are_editable_with_zero_code_change(vault):
    """§7 acceptance: `brain add kind` then using it in `add entry` works."""
    brain_conn = brain_db.connect(vault)
    brain.add_kind(brain_conn, "recipe", description="a cooking recipe")
    result = brain.add_entry(brain_conn, "carbonara", "why not", kind="recipe")
    item = brain.get_item_by_id(brain_conn, result["item_id"])
    assert item["kind"] == "recipe"


def test_list_entries_ordering_and_filters(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "first", "why 1", kind="fact")
    brain.add_entry(brain_conn, "second", "why 2", kind="method")
    entries = brain.list_entries(brain_conn)
    assert [e.title for e in entries] == ["second", "first"]  # newest first
    assert [e.n for e in entries] == [1, 2]

    only_fact = brain.list_entries(brain_conn, kind="fact")
    assert len(only_fact) == 1
    assert only_fact[0].title == "first"


def test_ref_resolution_by_ref_alias_and_row_number(vault):
    """§7 acceptance: every command taking <ref> also accepts a row number
    and an alias."""
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "npm fix", "why", ref="npm_fix")
    brain.add_alias(brain_conn, "npm_fix", "the npm thing")

    by_ref = brain.get_item_row(brain_conn, "npm_fix")
    by_alias = brain.get_item_row(brain_conn, "the npm thing")
    by_row = brain.get_item_row(brain_conn, "1")
    assert by_ref["id"] == by_alias["id"] == by_row["id"] == result["item_id"]


def test_find_by_alias_never_in_title_or_body(vault):
    """§7 acceptance: alias search."""
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "protobuf breakthrough", "walking the mapping tree", body="root to current_node")
    brain.add_alias(brain_conn, str(result["item_id"]), "the npm thing")
    hits = brain.find_entries(brain_conn, "npm")
    assert len(hits) == 1
    assert hits[0]["id"] == result["item_id"]


def test_find_finds_by_content(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "protobuf breakthrough", "walking the mapping tree", body="root to current_node")
    brain.add_entry(brain_conn, "unrelated", "something else entirely", body="nothing to do with it")
    results = brain.find_entries(brain_conn, "mapping tree")
    assert len(results) == 1
    assert results[0]["title"] == "protobuf breakthrough"


def test_pick_returns_exactly_one_by_length(vault):
    """§7 acceptance: `brain pick <ref> --length short` returns exactly one entry."""
    brain_conn = brain_db.connect(vault)
    parent = brain.add_entry(brain_conn, "CV bullet", "the family", kind="claim", ref="cv_bullet")
    brain.add_entry(brain_conn, "short version", "ATS friendly", kind="variant", parent="cv_bullet", rank=1, set_pairs=["length=short"])
    brain.add_entry(brain_conn, "long version", "full detail", kind="variant", parent="cv_bullet", rank=2, set_pairs=["length=long"])

    picked = brain.pick(brain_conn, "cv_bullet", length="short")
    assert picked["title"] == "short version"


def test_edit_entry_writes_history_and_edit_count(vault):
    """§7 acceptance: three edits produce three history rows and edit_count=3."""
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "original title", "why")
    ref = str(result["item_id"])

    brain.edit_entry(brain_conn, ref, title="new title")
    brain.edit_entry(brain_conn, ref, why="new why")
    brain.edit_entry(brain_conn, ref, rank=1)

    history = brain_conn.execute("SELECT * FROM history WHERE item_id = ?", (result["item_id"],)).fetchall()
    assert len(history) == 3
    item = brain.get_item_by_id(brain_conn, result["item_id"])
    assert item["edit_count"] == 3
    assert item["title"] == "new title"


def test_archive_and_restore_never_hard_delete(vault):
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "archive me", "why", ref="archive_me")
    ref = "archive_me"
    brain.archive_entry(brain_conn, ref)
    assert brain.get_item_by_id(brain_conn, result["item_id"])["status"] == "archived"
    brain.restore_entry(brain_conn, ref)
    assert brain.get_item_by_id(brain_conn, result["item_id"])["status"] == "active"


def test_remove_entry_is_the_only_hard_delete(vault):
    """§7 acceptance: only `remove entry --confirm` hard-deletes."""
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "delete me", "why")
    ref = str(result["item_id"])

    raised = False
    try:
        brain.remove_entry(brain_conn, ref, confirm=False)
    except brain.BrainError:
        raised = True
    assert raised
    assert brain_conn.execute("SELECT COUNT(*) FROM items WHERE id=?", (result["item_id"],)).fetchone()[0] == 1

    brain.remove_entry(brain_conn, ref, confirm=True)
    assert brain_conn.execute("SELECT COUNT(*) FROM items WHERE id=?", (result["item_id"],)).fetchone()[0] == 0


# ─────────────────────────── collections ───────────────────────────


def test_remove_entry_refuses_a_parent_with_children(vault):
    """Deleting a parent must not silently cascade-orphan its children —
    the FK on items.parent_id has no ON DELETE action on purpose."""
    brain_conn = brain_db.connect(vault)
    parent = brain.add_entry(brain_conn, "parent", "why", ref="the_parent")
    brain.add_entry(brain_conn, "child", "why", parent="the_parent")

    raised = False
    try:
        brain.remove_entry(brain_conn, "the_parent", confirm=True)
    except brain.BrainError:
        raised = True
    assert raised
    assert brain_conn.execute("SELECT COUNT(*) FROM items WHERE id=?", (parent["item_id"],)).fetchone()[0] == 1


def test_reordering_collection_changes_only_its_own_output(vault):
    """§7 acceptance: reordering a collection changes `show collection`
    output, and nothing else."""
    brain_conn = brain_db.connect(vault)
    a = brain.add_entry(brain_conn, "a", "why", ref="item_a")
    b = brain.add_entry(brain_conn, "b", "why", ref="item_b")
    brain.add_collection(brain_conn, "toolbox", why="stuff I use")
    brain.add_to_collection(brain_conn, "toolbox", ["item_a", "item_b"])

    _, items_before = brain.show_collection(brain_conn, "toolbox")
    assert [i["id"] for i in items_before] == [a["item_id"], b["item_id"]]

    brain.reorder_collection(brain_conn, "toolbox", ["item_b", "item_a"])
    _, items_after = brain.show_collection(brain_conn, "toolbox")
    assert [i["id"] for i in items_after] == [b["item_id"], a["item_id"]]

    # nothing about the items themselves changed
    assert brain.get_item_by_id(brain_conn, a["item_id"])["title"] == "a"
    assert brain.get_item_by_id(brain_conn, b["item_id"])["title"] == "b"


# ─────────────────────────── export / sensitivity ───────────────────────────


def test_export_excludes_non_normal_sensitivity_by_default(vault):
    """§7 acceptance."""
    import json

    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "public fact", "why", sensitivity="normal")
    brain.add_entry(brain_conn, "private note", "why", sensitivity="personal")

    default_export = json.loads(brain.export_items(brain_conn, fmt="json"))
    assert {r["title"] for r in default_export} == {"public fact"}

    full_export = json.loads(brain.export_items(brain_conn, fmt="json", include_sensitive=True))
    assert {r["title"] for r in full_export} == {"public fact", "private note"}


# ─────────────────────────── verify / drift ───────────────────────────


def test_verify_detects_body_drift_outside_the_db(vault, tmp_path):
    """§7 acceptance: change a file body outside the DB -> `brain verify`
    reports origin='detected'."""
    brain_conn = brain_db.connect(vault)
    f = tmp_path / "note.md"
    f.write_text("original body")
    result = brain.add_entry(brain_conn, "file-backed note", "why", source_path=str(f))

    f.write_text("changed outside the db")
    report = brain.verify(brain_conn)
    assert any(d["item_id"] == result["item_id"] for d in report["drifted"])

    item = brain.get_item_by_id(brain_conn, result["item_id"])
    assert item["body"] == "changed outside the db"

    detected = brain_conn.execute(
        "SELECT * FROM history WHERE item_id = ? AND origin = 'detected'", (result["item_id"],)
    ).fetchall()
    assert len(detected) == 1


# ─────────────────────────── semantic search ───────────────────────────


def test_index_entries_embeds_active_items(vault):
    brain_conn = brain_db.connect(vault)
    a = brain.add_entry(brain_conn, "npm troubleshooting", "why", body="fixing a broken npm install")
    b = brain.add_entry(brain_conn, "docker compose notes", "why", body="docker networking gotchas")

    result = brain.index_entries(brain_conn)
    assert result.embedded == 2
    assert result.skipped == 0

    for r in (a, b):
        meta = brain_conn.execute("SELECT * FROM item_embedding_meta WHERE item_id=?", (r["item_id"],)).fetchone()
        assert meta is not None
        vec_row = brain_conn.execute("SELECT * FROM item_embeddings WHERE item_id=?", (r["item_id"],)).fetchone()
        assert vec_row is not None


def test_index_entries_skips_unchanged_text_unless_forced(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "npm troubleshooting", "why", body="fixing a broken npm install")

    first = brain.index_entries(brain_conn)
    assert first.embedded == 1

    second = brain.index_entries(brain_conn)
    assert second.embedded == 0
    assert second.skipped == 1

    forced = brain.index_entries(brain_conn, force=True)
    assert forced.embedded == 1


def test_index_entries_reembeds_when_text_changes(vault):
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "npm troubleshooting", "why", body="fixing a broken npm install")
    brain.index_entries(brain_conn)

    brain.edit_entry(brain_conn, str(result["item_id"]), body="a totally different topic now")
    again = brain.index_entries(brain_conn)
    assert again.embedded == 1


def test_index_entries_stops_on_unavailable(vault, monkeypatch):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "npm troubleshooting", "why", body="fixing a broken npm install")

    def raise_unavailable(text, model="nomic-embed-text"):
        raise brain.EmbeddingUnavailable("no ollama here")

    monkeypatch.setattr(brain_embeddings, "embed_text", raise_unavailable)
    result = brain.index_entries(brain_conn)
    assert result.unavailable is True
    assert result.embedded == 0


def test_semantic_find_ranks_by_meaning_not_exact_words(vault):
    """The core semantic-search promise: a query sharing zero words with
    the target still ranks it highest, because the topic vector matches."""
    brain_conn = brain_db.connect(vault)
    npm_item = brain.add_entry(brain_conn, "the npm thing", "why", body="fixing a broken npm install on macOS")
    docker_item = brain.add_entry(brain_conn, "container notes", "why", body="docker networking gotchas")
    brain.index_entries(brain_conn)

    hits = brain.semantic_find(brain_conn, "npm package manager troubles")
    assert hits[0]["id"] == npm_item["item_id"]
    assert hits[0]["id"] != docker_item["item_id"]


def test_remove_entry_cleans_up_its_embedding(vault):
    """item_embeddings is a vec0 table with no real FK/CASCADE — a hard
    delete has to clean it up itself, or a removed item's vector keeps
    influencing semantic_find forever."""
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "npm troubleshooting", "why", body="fixing npm", ref="npm_del_test")
    brain.index_entries(brain_conn)
    assert brain_conn.execute("SELECT COUNT(*) FROM item_embeddings WHERE item_id=?", (result["item_id"],)).fetchone()[0] == 1

    brain.remove_entry(brain_conn, "npm_del_test", confirm=True)
    assert brain_conn.execute("SELECT COUNT(*) FROM item_embeddings WHERE item_id=?", (result["item_id"],)).fetchone()[0] == 0


def test_verify_self_heals_dangling_embeddings(vault):
    """Belt-and-suspenders: even if an embedding got orphaned some other
    way, `verify` finds and repairs it."""
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "npm troubleshooting", "why", body="fixing npm")
    brain.index_entries(brain_conn)
    # simulate an orphan the way a delete that skipped cleanup would leave one
    brain_conn.execute("DELETE FROM items WHERE id=?", (result["item_id"],))
    brain_conn.commit()
    assert brain_conn.execute("SELECT COUNT(*) FROM item_embeddings").fetchone()[0] == 1

    report = brain.verify(brain_conn)
    assert len(report["dangling_embeddings"]) == 1
    assert brain_conn.execute("SELECT COUNT(*) FROM item_embeddings").fetchone()[0] == 0


def test_embed_status_counts_are_consistent(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "a", "why", body="npm stuff")
    brain.add_entry(brain_conn, "b", "why", body="docker stuff")
    s0 = brain.embed_status(brain_conn)
    assert s0["total"] == 2
    assert s0["embedded"] == 0
    assert s0["pending"] == 2

    brain.index_entries(brain_conn)
    s1 = brain.embed_status(brain_conn)
    assert s1["embedded"] == 2
    assert s1["pending"] == 0


# ─────────────────────────── distill ───────────────────────────


def _fake_generate_extraction(prompt: str, model: str = "fake") -> str:
    return "- Fact: the fake extraction ran.\n- Method: this is a fake, deterministic response."


def _fake_generate_nothing_durable(prompt: str, model: str = "fake") -> str:
    from chatvault.brain.distill import NOTHING_DURABLE_MARKER

    return NOTHING_DURABLE_MARKER


def test_distill_requires_why(vault, conn):
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    raised = False
    try:
        brain.distill(brain_conn, vault_ro, row["uid"], "", generate=_fake_generate_extraction)
    except brain.BrainError:
        raised = True
    assert raised
    vault_ro.close()


def test_distill_creates_item_with_source_uid_stored_internally(vault, conn):
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    result = brain.distill(brain_conn, vault_ro, row["uid"], "worth keeping", generate=_fake_generate_extraction)
    vault_ro.close()

    assert result["action"] == "distilled"
    item = brain.get_item_by_id(brain_conn, result["item_id"])
    assert item["source_uid"] == row["uid"]
    assert item["source_kind"] == "chatvault_distilled"
    assert "fake extraction" in item["body"]
    assert item["why"] == "worth keeping"
    # never addressed by the raw uid — no ref was set from it, and the uid
    # never appears in anything a user would type to find this item again
    assert item["ref"] != row["uid"]


def test_distill_creates_no_item_when_nothing_durable(vault, conn):
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    before = brain_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    result = brain.distill(brain_conn, vault_ro, row["uid"], "why", generate=_fake_generate_nothing_durable)
    vault_ro.close()

    assert result["action"] == "nothing_durable"
    after = brain_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert after == before


def test_distill_is_not_deduplicated_like_promote(vault, conn):
    """Distilling twice creates two items — unlike promote, which is
    idempotent. This is deliberate: distilling is a creative act the user
    may repeat."""
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    brain.distill(brain_conn, vault_ro, row["uid"], "first pass", generate=_fake_generate_extraction)
    brain.distill(brain_conn, vault_ro, row["uid"], "second pass", generate=_fake_generate_extraction)
    vault_ro.close()

    n = brain_conn.execute("SELECT COUNT(*) FROM items WHERE source_uid = ?", (row["uid"],)).fetchone()[0]
    assert n == 2


# ─────────────────────────── jot / inbox ───────────────────────────


def test_jot_requires_no_why(vault):
    brain_conn = brain_db.connect(vault)
    result = brain.jot(brain_conn, "a quick unstructured thought")
    item = brain.get_item_by_id(brain_conn, result["item_id"])
    assert item["why"] == ""
    assert item["status"] == "inbox"


def test_jot_requires_some_text(vault):
    brain_conn = brain_db.connect(vault)
    raised = False
    try:
        brain.jot(brain_conn, "   ")
    except brain.BrainError:
        raised = True
    assert raised


def test_jotted_items_are_invisible_to_list_and_find(vault):
    brain_conn = brain_db.connect(vault)
    brain.jot(brain_conn, "npm troubleshooting notes, unstructured")
    assert brain.list_entries(brain_conn) == []
    assert brain.find_entries(brain_conn, "npm") == []
    assert len(brain.list_inbox(brain_conn)) == 1


def test_accept_requires_why_and_promotes_to_active(vault):
    brain_conn = brain_db.connect(vault)
    brain.jot(brain_conn, "npm troubleshooting notes, unstructured")

    raised = False
    try:
        brain.accept(brain_conn, "1", "")
    except brain.BrainError:
        raised = True
    assert raised

    result = brain.accept(brain_conn, "1", "actually useful", kind="fact", ref="npm_from_jot")
    item = brain.get_item_by_id(brain_conn, result["item_id"])
    assert item["status"] == "active"
    assert item["why"] == "actually useful"
    assert item["kind"] == "fact"
    assert item["ref"] == "npm_from_jot"
    assert len(brain.list_inbox(brain_conn)) == 0
    assert len(brain.list_entries(brain_conn)) == 1


def test_reject_discards_inbox_item_only(vault):
    brain_conn = brain_db.connect(vault)
    brain.jot(brain_conn, "a thought not worth keeping")
    before = brain_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    brain.reject(brain_conn, "1")
    after = brain_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert after == before - 1


def test_reject_does_not_touch_a_real_entry(vault):
    """reject only ever deletes status='inbox' rows — it must never become
    a second hard-delete path for curated entries."""
    brain_conn = brain_db.connect(vault)
    result = brain.add_entry(brain_conn, "a real entry", "why")

    raised = False
    try:
        brain.reject(brain_conn, "1")
    except brain.BrainError:
        raised = True
    # either it refuses outright, or it's a no-op that leaves the entry intact
    if not raised:
        assert brain_conn.execute("SELECT COUNT(*) FROM items WHERE id=?", (result["item_id"],)).fetchone()[0] == 1


# ─────────────────────── docs/51_SPEC parity batch A ───────────────────────


def test_doctor_reports_all_checks_and_survives_ollama_down(vault, monkeypatch):
    """docs/51_SPEC_brain_parity_and_naming.md §2.1 `doctor` row. Must not
    depend on a real Ollama server — force the reachability probe to fail
    deterministically and assert `doctor` still completes and reports it as
    a warning, not a fatal failure."""
    import urllib.error

    from chatvault.brain import doctor as brain_doctor

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(brain_doctor.urllib.request, "urlopen", fake_urlopen)

    brain_conn = brain_db.connect(vault)
    results = brain_doctor.run_all(brain_conn)
    names = {r.name for r in results}
    assert {
        "brain.sqlite integrity", "brain migration state", "orphaned FTS rows",
        "item_links edge count", "ollama reachability",
    } <= names

    ollama = next(r for r in results if r.name == "ollama reachability")
    assert ollama.ok is False
    assert ollama.fatal is False  # warn only — doctor must not treat Ollama-down as fatal

    integrity = next(r for r in results if r.name == "brain.sqlite integrity")
    assert integrity.ok is True

    edges = next(r for r in results if r.name == "item_links edge count")
    assert "0 edge(s)" in edges.detail


def test_add_note_appends_freeform_commentary_distinct_from_why(vault):
    brain_conn = brain_db.connect(vault)
    added = brain.add_entry(brain_conn, "an entry", "the mandatory why")
    result = brain.add_note(brain_conn, str(added["item_id"]), "a later, free-form remark")
    assert result["item_id"] == added["item_id"]

    notes = brain.list_notes(brain_conn, str(added["item_id"]))
    assert len(notes) == 1
    assert notes[0]["text"] == "a later, free-form remark"

    # why is untouched — notes are additive, not a replacement for it
    item = brain.get_item_by_id(brain_conn, added["item_id"])
    assert item["why"] == "the mandatory why"


def test_add_note_requires_nonempty_text(vault):
    brain_conn = brain_db.connect(vault)
    added = brain.add_entry(brain_conn, "an entry", "why")
    with pytest.raises(brain.BrainError):
        brain.add_note(brain_conn, str(added["item_id"]), "   ")


def test_exclude_and_restore_round_trip_leaves_row_count_unchanged(vault):
    """§7 acceptance 5.7: an excluded entry disappears from `list entries`/
    `find`/`export` and reappears via `restore`; row count unchanged throughout."""
    brain_conn = brain_db.connect(vault)
    # explicit `ref=` so exclude/restore can be addressed unambiguously — a
    # bare digit given to resolve_item means "row N of the current list",
    # not "id N" (same convention every other <ref>-taking command uses),
    # so a non-numeric ref sidesteps that ambiguity entirely for this test.
    a = brain.add_entry(brain_conn, "npm caching notes", "why a", ref="entry-a")
    b = brain.add_entry(brain_conn, "unrelated entry", "why b", ref="entry-b")
    total = brain_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert total == 2
    assert len(brain.list_entries(brain_conn)) == 2

    brain.exclude_entry(brain_conn, "entry-a")

    assert brain_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2  # row count unchanged
    remaining = brain.list_entries(brain_conn)
    assert len(remaining) == 1
    assert remaining[0].item_id == b["item_id"]
    assert brain.find_entries(brain_conn, "npm") == []
    exported = json.loads(brain.export_items(brain_conn, fmt="json"))
    assert all(row["id"] != a["item_id"] for row in exported)

    brain.restore_entry(brain_conn, "entry-a")

    assert brain_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    restored = brain.list_entries(brain_conn)
    assert len(restored) == 2
    assert any(e.item_id == a["item_id"] for e in restored)
    found_again = brain.find_entries(brain_conn, "npm")
    assert any(r["id"] == a["item_id"] for r in found_again)


def test_exclude_is_not_archive(vault):
    """§8 Q6: exclude and archive are deliberately separate assertions —
    excluding an entry must not set status='archived'."""
    brain_conn = brain_db.connect(vault)
    added = brain.add_entry(brain_conn, "an entry", "why")
    brain.exclude_entry(brain_conn, str(added["item_id"]))
    item = brain.get_item_by_id(brain_conn, added["item_id"])
    assert item["status"] == "active"
    assert item["excluded"] == 1


def test_stats_by_kind_and_status(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "a fact", "why", kind="fact")
    brain.add_entry(brain_conn, "a note", "why", kind="note")
    other = brain.add_entry(brain_conn, "another note", "why", kind="note")

    by_kind = brain.stats_by(brain_conn, "kind")
    assert by_kind.get("fact") == 1
    assert by_kind.get("note") == 2

    brain.archive_entry(brain_conn, str(other["item_id"]))
    by_status = brain.stats_by(brain_conn, "status")
    assert by_status.get("active") == 2
    assert by_status.get("archived") == 1


def test_stats_by_rejects_unknown_axis(vault):
    brain_conn = brain_db.connect(vault)
    with pytest.raises(ValueError):
        brain.stats_by(brain_conn, "not-a-real-axis")


def test_ledger_reports_origin_detail_populated_by_copy_conversation(vault, conn):
    """§2.1 `ledger` row: a provenance report over `items.origin_detail`,
    stamped by `copy conversation` (brain.entries.promote)."""
    row = _conv(conn)
    brain_conn = brain_db.connect(vault)
    vault_ro = _vault_ro(vault)
    try:
        brain.promote(brain_conn, vault_ro, None, row["uid"], "worth keeping")
    finally:
        vault_ro.close()

    report = brain.ledger_mod.report(brain_conn)
    origins = {r["origin"]: r["count"] for r in report}
    assert origins.get("copy:chatvault") == 1


def test_ledger_groups_items_with_no_origin_detail_as_unknown(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "a manual entry", "why")
    report = brain.ledger_mod.report(brain_conn)
    origins = {r["origin"]: r["count"] for r in report}
    assert origins.get("(unknown)") == 1


# ═══════════════════════════ parity batch B (docs/51_SPEC §2.1/§6 phase 4) ═══════════════════════════


def test_dedupe_reports_candidates_and_modifies_nothing_without_confirm(vault):
    """§7 acceptance 5.9 / §8 D7: `brain dedupe` alone must never write."""
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "npm caching notes", "why", body="the exact same body text")
    brain.add_entry(brain_conn, "npm caching notes", "why", body="the exact same body text")

    before = [dict(r) for r in brain_conn.execute("SELECT * FROM items ORDER BY id").fetchall()]
    candidates = brain.dedupe_mod.scan_candidates(brain_conn)
    after = [dict(r) for r in brain_conn.execute("SELECT * FROM items ORDER BY id").fetchall()]

    assert before == after
    assert len(candidates) >= 1
    assert any(c["reason"] == "exact_body_hash" for c in candidates)


def test_dedupe_confirm_merges_named_pair_only(vault):
    brain_conn = brain_db.connect(vault)
    a = brain.add_entry(brain_conn, "npm caching notes", "why a", body="same body", ref="dupe-a")
    b = brain.add_entry(brain_conn, "npm caching notes", "why b", body="same body", ref="dupe-b")
    brain.tag_entry(brain_conn, "dupe-b", ["tech/npm"])

    result = brain.dedupe_mod.merge_pair(brain_conn, "dupe-a", "dupe-b", confirm=True)

    assert result["kept"] == a["item_id"]
    assert result["archived"] == b["item_id"]
    kept = brain.get_item_by_id(brain_conn, a["item_id"])
    dropped = brain.get_item_by_id(brain_conn, b["item_id"])
    assert kept["status"] == "active"
    assert dropped["status"] == "archived"  # not hard-deleted — reversible
    tags = {r["name"] for r in brain_conn.execute(
        "SELECT t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = ?", (a["item_id"],)
    ).fetchall()}
    assert "tech/npm" in tags


def test_dedupe_merge_refuses_without_confirm(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "a", "why", ref="x")
    brain.add_entry(brain_conn, "b", "why", ref="y")
    with pytest.raises(brain.BrainError):
        brain.dedupe_mod.merge_pair(brain_conn, "x", "y", confirm=False)


def test_view_save_run_list_round_trip(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "a fact", "why", kind="fact")
    brain.add_entry(brain_conn, "a note", "why", kind="note")

    brain.views_mod.save_view(brain_conn, "facts-only", "SELECT id, title FROM items WHERE kind = 'fact'", description="just facts")
    names = {r["name"] for r in brain.views_mod.list_views(brain_conn)}
    assert "facts-only" in names

    rows = brain.views_mod.run_view(brain_conn, "facts-only")
    assert len(rows) == 1
    assert rows[0]["title"] == "a fact"

    brain.views_mod.remove_view(brain_conn, "facts-only")
    assert brain.views_mod.list_views(brain_conn) == []


def test_view_save_rejects_duplicate_name(vault):
    brain_conn = brain_db.connect(vault)
    brain.views_mod.save_view(brain_conn, "v1", "SELECT 1")
    with pytest.raises(brain.BrainError):
        brain.views_mod.save_view(brain_conn, "v1", "SELECT 2")


def test_curation_export_import_round_trips_tags_and_links(vault):
    brain_conn = brain_db.connect(vault)
    a = brain.add_entry(brain_conn, "entry a", "why", ref="cur-a")
    b = brain.add_entry(brain_conn, "entry b", "why", ref="cur-b")
    brain.tag_entry(brain_conn, "cur-a", ["tech/sqlite"])
    brain.link_entry(brain_conn, "cur-a", "cur-b", "see_also")
    brain.add_note(brain_conn, "cur-a", "a running remark")
    brain.exclude_entry(brain_conn, "cur-a")

    data = brain.curation_io_mod.export_curation(brain_conn)
    assert data["counts"]["tag_links"] == 1
    assert data["counts"]["item_links"] == 1
    assert data["counts"]["notes"] == 1
    assert data["counts"]["exclusions"] == 1

    # simulate a fresh DB (same items, curation wiped) and re-import
    brain_conn.execute("DELETE FROM tag_links")
    brain_conn.execute("DELETE FROM item_links")
    brain_conn.execute("DELETE FROM notes")
    brain_conn.execute("UPDATE items SET excluded = 0")
    brain_conn.commit()

    summary = brain.curation_io_mod.import_curation(brain_conn, data)
    assert summary.counts["tag_links"]["added"] == 1
    assert summary.counts["item_links"]["added"] == 1
    assert summary.counts["notes"]["added"] == 1
    assert summary.counts["exclusions"]["added"] == 1
    assert not summary.unresolved

    tags = {r["name"] for r in brain_conn.execute(
        "SELECT t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = ?", (a["item_id"],)
    ).fetchall()}
    assert "tech/sqlite" in tags
    links = brain_conn.execute("SELECT * FROM item_links WHERE from_id = ? AND to_id = ?", (a["item_id"], b["item_id"])).fetchall()
    assert len(links) == 1
    assert brain.get_item_by_id(brain_conn, a["item_id"])["excluded"] == 1


def test_curation_diff_detects_a_real_change(vault):
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "entry a", "why", ref="diff-a")
    brain.tag_entry(brain_conn, "diff-a", ["tech/sqlite"])
    data = brain.curation_io_mod.export_curation(brain_conn)

    # no change yet -> diff reports nothing new
    clean_diff = brain.curation_io_mod.diff_curation(brain_conn, data)
    assert clean_diff.counts["tag_links"]["added"] == 0

    # remove the tag link -> diff must now report it as something to add back
    brain_conn.execute("DELETE FROM tag_links")
    brain_conn.commit()
    dirty_diff = brain.curation_io_mod.diff_curation(brain_conn, data)
    assert dirty_diff.counts["tag_links"]["added"] == 1
    # diff never writes
    assert brain_conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 0


def test_attachments_add_list_show(vault, tmp_path):
    brain_conn = brain_db.connect(vault)
    added = brain.add_entry(brain_conn, "an entry", "why", ref="att-a")
    f = tmp_path / "notes.txt"
    f.write_text("hello world")

    result = brain.attachments_mod.add_attachment(brain_conn, "att-a", f)
    assert result["filename"] == "notes.txt"

    rows = brain.attachments_mod.list_attachments(brain_conn, "att-a")
    assert len(rows) == 1
    assert rows[0]["bytes"] == len("hello world")

    shown = brain.attachments_mod.show_attachment(brain_conn, rows[0]["id"])
    assert shown["content"] == "hello world"
    assert shown["item_id"] == added["item_id"]


def test_attachments_show_missing_raises(vault):
    brain_conn = brain_db.connect(vault)
    with pytest.raises(brain.BrainError):
        brain.attachments_mod.show_attachment(brain_conn, 999)


def test_redact_wipes_body_keeps_row_requires_confirm(vault):
    brain_conn = brain_db.connect(vault)
    added = brain.add_entry(brain_conn, "an entry", "why", body="sensitive text")
    with pytest.raises(brain.BrainError):
        brain.redact_entry(brain_conn, str(added["item_id"]), confirm=False)

    result = brain.redact_entry(brain_conn, str(added["item_id"]), confirm=True)
    assert result["action"] == "redacted"
    item = brain.get_item_by_id(brain_conn, added["item_id"])
    assert item["body"] is None
    assert item["title"] == "an entry"  # row kept


def test_purge_is_the_same_hard_delete_as_remove_entry(vault):
    brain_conn = brain_db.connect(vault)
    added = brain.add_entry(brain_conn, "an entry", "why")
    with pytest.raises(brain.BrainError):
        brain.purge_entry(brain_conn, str(added["item_id"]), confirm=False)

    brain.purge_entry(brain_conn, str(added["item_id"]), confirm=True)
    assert brain_conn.execute("SELECT COUNT(*) FROM items WHERE id = ?", (added["item_id"],)).fetchone()[0] == 0


def test_tag_rule_auto_applies_on_add_entry_and_is_reversible(vault):
    brain_conn = brain_db.connect(vault)
    brain.tag_rules_mod.add_rule(brain_conn, "docker", "tech/docker", field="title")

    added = brain.add_entry(brain_conn, "docker compose notes", "why")
    tags = {r["name"] for r in brain_conn.execute(
        "SELECT t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = ?", (added["item_id"],)
    ).fetchall()}
    assert "tech/docker" in tags
    source = brain_conn.execute(
        "SELECT tl.source FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = ? AND t.name = 'tech/docker'",
        (added["item_id"],),
    ).fetchone()["source"]
    assert source == "rule"

    # reversible via untag --source rule
    brain.untag_entry(brain_conn, str(added["item_id"]), source="rule")
    tags_after = {r["name"] for r in brain_conn.execute(
        "SELECT t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = ?", (added["item_id"],)
    ).fetchall()}
    assert "tech/docker" not in tags_after


def test_tag_rule_auto_applies_on_accept(vault):
    brain_conn = brain_db.connect(vault)
    brain.tag_rules_mod.add_rule(brain_conn, "docker", "tech/docker", field="title")
    brain.jot(brain_conn, "docker compose notes")
    result = brain.accept(brain_conn, "1", "why it matters")
    tags = {r["name"] for r in brain_conn.execute(
        "SELECT t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = ?", (result["item_id"],)
    ).fetchall()}
    assert "tech/docker" in tags


def test_tag_rule_apply_all_backfills_existing_entries(vault):
    brain_conn = brain_db.connect(vault)
    added = brain.add_entry(brain_conn, "docker compose notes", "why")
    brain.tag_rules_mod.add_rule(brain_conn, "docker", "tech/docker", field="title")

    n = brain.tag_rules_mod.apply_rules_all(brain_conn)
    assert n == 1
    tags = {r["name"] for r in brain_conn.execute(
        "SELECT t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.item_id = ?", (added["item_id"],)
    ).fetchall()}
    assert "tech/docker" in tags


def test_tag_rule_list_and_remove(vault):
    brain_conn = brain_db.connect(vault)
    added = brain.tag_rules_mod.add_rule(brain_conn, "docker", "tech/docker")
    assert len(brain.tag_rules_mod.list_rules(brain_conn)) == 1
    assert brain.tag_rules_mod.remove_rule(brain_conn, added["rule_id"]) is True
    assert brain.tag_rules_mod.list_rules(brain_conn) == []
    assert brain.tag_rules_mod.remove_rule(brain_conn, 999) is False


class _FakeNeo4jTx:
    def run(self, *args, **kwargs):
        return None


class _FakeNeo4jSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, *args, **kwargs):
        return None

    def execute_write(self, fn):
        return fn(_FakeNeo4jTx())


class _FakeNeo4jDriver:
    """Never opens a real socket — docs/51_SPEC §7 non-goal: `brain graph`
    must be built and testable without ever running against a real Neo4j
    instance."""

    def session(self):
        return _FakeNeo4jSession()

    def close(self):
        pass


def test_graph_rebuild_against_scratch_brain_with_synthetic_links(vault):
    brain_conn = brain_db.connect(vault)
    a = brain.add_entry(brain_conn, "entry a", "why", ref="graph-a")
    b = brain.add_entry(brain_conn, "entry b", "why", ref="graph-b")
    brain.tag_entry(brain_conn, "graph-a", ["tech/sqlite"])
    brain.link_entry(brain_conn, "graph-a", "graph-b", "see_also")

    fake_driver = _FakeNeo4jDriver()
    counts = brain.graph_mod.rebuild(brain_conn, fake_driver)

    assert counts["items"] == 2
    assert counts["item_links"] == 1
    assert counts["tag_edges"] == 1


def test_graph_get_driver_is_never_called_by_rebuild(vault, monkeypatch):
    """Never touches chatvault.brain.graph.get_driver (the only path that
    would open a real Neo4j connection) — rebuild()/run_query() take a
    driver as a parameter instead, so a caller can't accidentally reach a
    live instance through this module's own test coverage."""
    from chatvault.brain import graph as brain_graph

    def _boom(*a, **k):
        raise AssertionError("get_driver must not be called in tests")

    monkeypatch.setattr(brain_graph, "get_driver", _boom)
    brain_conn = brain_db.connect(vault)
    brain.add_entry(brain_conn, "entry a", "why")
    brain.graph_mod.rebuild(brain_conn, _FakeNeo4jDriver())

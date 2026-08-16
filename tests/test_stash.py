from __future__ import annotations

from chatvault import ingest, stash
from tests.conftest import FIXTURES


def _stash_conv_id(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    return conn.execute("SELECT id FROM conversations WHERE platform = 'claude_code'").fetchone()[0]


def _conv_count(conn):
    return conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]


def test_add_refuses_empty_why(conn):
    conv_id = _stash_conv_id(conn)
    raised = False
    try:
        stash.add(conn, conv_id, "")
    except stash.StashError:
        raised = True
    assert raised

    raised = False
    try:
        stash.add(conn, conv_id, "   ")
    except stash.StashError:
        raised = True
    assert raised

    raised = False
    try:
        stash.add(conn, conv_id, None)
    except stash.StashError:
        raised = True
    assert raised


def test_add_refuses_invalid_kind(conn):
    conv_id = _stash_conv_id(conn)
    raised = False
    try:
        stash.add(conn, conv_id, "a real reason", kind="not-a-real-kind")
    except stash.StashError:
        raised = True
    assert raised


def test_add_tags_and_records_why(conn):
    conv_id = _stash_conv_id(conn)
    stash.add(conn, conv_id, "the protobuf breakthrough", kind="method")

    tag_names = {
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN tag_links tl ON tl.tag_id = t.id WHERE tl.target_type='conversation' AND tl.target_id = ?",
            (conv_id,),
        ).fetchall()
    }
    assert "stash/" in tag_names
    assert "stash/method" in tag_names
    assert stash.get_why(conn, conv_id) == "the protobuf breakthrough"
    assert stash.get_kind(conn, conv_id) == "method"


def test_add_without_kind_has_no_kind_tag(conn):
    conv_id = _stash_conv_id(conn)
    stash.add(conn, conv_id, "just a keep, no sub-kind")
    assert stash.get_kind(conn, conv_id) is None


def test_add_is_idempotent_on_same_why(conn):
    conv_id = _stash_conv_id(conn)
    stash.add(conn, conv_id, "same reason", kind="reference")
    n_before = conn.execute(
        "SELECT COUNT(*) FROM annotations WHERE target_type='conversation' AND target_id=?", (conv_id,)
    ).fetchone()[0]
    stash.add(conn, conv_id, "same reason", kind="reference")
    n_after = conn.execute(
        "SELECT COUNT(*) FROM annotations WHERE target_type='conversation' AND target_id=?", (conv_id,)
    ).fetchone()[0]
    assert n_before == n_after == 1


def test_add_with_new_why_is_additive_not_overwrite(conn):
    conv_id = _stash_conv_id(conn)
    stash.add(conn, conv_id, "first reason")
    stash.add(conn, conv_id, "better, later reason")
    n = conn.execute(
        "SELECT COUNT(*) FROM annotations WHERE target_type='conversation' AND target_id=?", (conv_id,)
    ).fetchone()[0]
    assert n == 2
    # get_why returns the most recent
    assert stash.get_why(conn, conv_id) == "better, later reason"


def test_list_all_ordering_kind_and_since_filters(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    ingest.ingest_path(conn, FIXTURES / "generic" / "toolsession.jsonl")
    ids = [r[0] for r in conn.execute("SELECT id FROM conversations ORDER BY id").fetchall()]
    stash.add(conn, ids[0], "first stashed", kind="method")
    stash.add(conn, ids[1], "second stashed", kind="decision")

    entries = stash.list_all(conn)
    assert len(entries) == 2
    # newest first
    assert entries[0].conv_id == ids[1]
    assert entries[1].conv_id == ids[0]
    assert [e.n for e in entries] == [1, 2]

    filtered = stash.list_all(conn, kind="method")
    assert len(filtered) == 1
    assert filtered[0].conv_id == ids[0]

    future = stash.list_all(conn, since="2099-01-01")
    assert future == []


def test_get_entry_out_of_range_raises(conn):
    conv_id = _stash_conv_id(conn)
    stash.add(conn, conv_id, "only one")
    raised = False
    try:
        stash.get_entry(conn, 2)
    except stash.StashError:
        raised = True
    assert raised


def test_drop_is_non_destructive(conn):
    """Acceptance criterion (spec §8): stash drop removes tag + note, the
    conversation is untouched, and `chatvault stats` conversation counts
    stay identical."""
    conv_id = _stash_conv_id(conn)
    before_row = dict(conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone())
    before_count = _conv_count(conn)

    stash.add(conn, conv_id, "worth keeping", kind="thread")
    result = stash.drop(conn, 1)

    assert result["conv_id"] == conv_id
    assert result["removed_tags"] == 2  # stash/ + stash/thread
    assert result["removed_notes"] == 1

    after_row = dict(conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone())
    after_count = _conv_count(conn)

    assert before_row == after_row
    assert before_count == after_count

    # stash state is gone
    assert stash.get_why(conn, conv_id) is None
    assert stash.get_kind(conn, conv_id) is None
    assert stash.list_all(conn) == []


def test_drop_does_not_touch_unrelated_annotations(conn):
    """A conversation might already carry an ordinary annotation (via the
    pre-existing `chatvault note` command) — drop must only remove the
    stash-owned why, never a user's other notes on the same conversation."""
    conv_id = _stash_conv_id(conn)
    conn.execute(
        "INSERT INTO annotations (target_type, target_id, note, created_at) VALUES ('conversation', ?, 'unrelated curation note', ?)",
        (conv_id, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()

    stash.add(conn, conv_id, "the stash reason")
    stash.drop(conn, 1)

    remaining = conn.execute(
        "SELECT note FROM annotations WHERE target_type='conversation' AND target_id=?", (conv_id,)
    ).fetchall()
    assert [r[0] for r in remaining] == ["unrelated curation note"]


def test_resolve_pick_single_and_multi(conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    ingest.ingest_path(conn, FIXTURES / "generic" / "toolsession.jsonl")
    ids = [r[0] for r in conn.execute("SELECT id FROM conversations ORDER BY id").fetchall()]
    stash.add(conn, ids[0], "first")
    stash.add(conn, ids[1], "second")

    # newest-first: entry 1 = ids[1], entry 2 = ids[0]
    assert stash.resolve_pick(conn, "1") == [ids[1]]
    assert stash.resolve_pick(conn, "1,2") == [ids[1], ids[0]]


def test_resolve_pick_survives_a_write_between_stash_and_pick(conn):
    """Regression: find's shared --pick cache invalidates on any DB write
    (content-based via PRAGMA data_version), and `stash add` is itself a
    write — so resolving `brief --pick` against that cache would break on
    the very next command after the add that made an item worth briefing.
    stash.resolve_pick must not depend on any such cache."""
    conv_id = _stash_conv_id(conn)
    stash.add(conn, conv_id, "first save")
    # a second, unrelated write after the stash add
    conn.execute("UPDATE conversations SET starred = 1 WHERE id = ?", (conv_id,))
    conn.commit()
    assert stash.resolve_pick(conn, "1") == [conv_id]


def test_resolve_pick_invalid_row_raises(conn):
    conv_id = _stash_conv_id(conn)
    stash.add(conn, conv_id, "only one")
    raised = False
    try:
        stash.resolve_pick(conn, "99")
    except stash.StashError:
        raised = True
    assert raised


def test_add_unknown_conv_id_raises(conn):
    raised = False
    try:
        stash.add(conn, 999999, "doesn't exist")
    except stash.StashError:
        raised = True
    assert raised

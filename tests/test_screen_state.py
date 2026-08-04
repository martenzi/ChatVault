from __future__ import annotations

from chatvault import loader, tags
from chatvault.adapters.base import make_block, make_conversation, make_message
from chatvault.screen_state import ScreenState


def _load(conn, native_id, title, started_at, tagged_with=None):
    cir = make_conversation(
        platform="synthetic", native_id=native_id, title=title, project=None,
        started_at=started_at, ended_at=started_at, source_path="synthetic", raw={},
        messages=[make_message(0, "user", None, started_at, [make_block(0, "text", "hello", raw={})], raw={})],
    )
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    if tagged_with:
        for name in tagged_with:
            tags.tag_add(conn, name, f"id = {result.conv_id}", source="manual")
    return result


# ─────────────── search / filters ───────────────


def test_search_defaults_to_most_recent(conn):
    _load(conn, "a1", "older", "2026-01-01T00:00:00")
    _load(conn, "a2", "newer", "2026-01-02T00:00:00")
    state = ScreenState(conn)
    rows = state.search()
    assert rows[0]["title"] == "newer"
    assert state.total_matches == 2


def test_search_untagged_only(conn):
    r1 = _load(conn, "b1", "no tags", "2026-01-01T00:00:00")
    _load(conn, "b2", "has a tag", "2026-01-02T00:00:00", tagged_with=["x/y"])

    state = ScreenState(conn)
    rows = state.search(untagged_only=True)
    assert len(rows) == 1
    assert rows[0]["id"] == r1.conv_id


def test_search_filters_persist_across_calls(conn):
    _load(conn, "c1", "conv one", "2026-01-01T00:00:00")
    state = ScreenState(conn)
    state.search(platform="synthetic")
    assert state.platform == "synthetic"
    # a later call that only changes the query keeps the platform filter
    state.search(query="conv")
    assert state.platform == "synthetic"
    assert state.query == "conv"


def test_search_does_not_disturb_selection(conn):
    r1 = _load(conn, "d1", "conv one", "2026-01-01T00:00:00")
    _load(conn, "d2", "conv two", "2026-01-02T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.search(query="conv")  # re-searching (spec: multi-select survives re-searching)
    assert state.selected == {r1.conv_id}


# ─────────────── selection ───────────────


def test_toggle_selection(conn):
    r1 = _load(conn, "e1", "conv one", "2026-01-01T00:00:00")
    state = ScreenState(conn)
    state.toggle(r1.conv_id)
    assert state.selected == {r1.conv_id}
    state.toggle(r1.conv_id)
    assert state.selected == set()


def test_select_all_and_none(conn):
    r1 = _load(conn, "f1", "conv one", "2026-01-01T00:00:00")
    r2 = _load(conn, "f2", "conv two", "2026-01-02T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.select_all()
    assert state.selected == {r1.conv_id, r2.conv_id}
    state.select_none()
    assert state.selected == set()


# ─────────────── bulk apply / untag ───────────────


def test_apply_tag_to_selection(conn):
    r1 = _load(conn, "g1", "conv one", "2026-01-01T00:00:00")
    r2 = _load(conn, "g2", "conv two", "2026-01-02T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.toggle(r2.conv_id)

    n = state.apply_tag("tech/macos/npm")
    assert n == 2
    link = conn.execute(
        "SELECT source FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = ? AND tl.target_id = ?",
        ("tech/macos/npm", r1.conv_id),
    ).fetchone()
    assert link["source"] == "manual"


def test_apply_tag_only_counts_newly_tagged(conn):
    r1 = _load(conn, "h1", "conv one", "2026-01-01T00:00:00", tagged_with=["tech/macos/npm"])
    r2 = _load(conn, "h2", "conv two", "2026-01-02T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.toggle(r2.conv_id)

    n = state.apply_tag("tech/macos/npm")
    assert n == 1  # r1 already had it


def test_apply_tag_with_nothing_selected_is_a_noop(conn):
    state = ScreenState(conn)
    assert state.apply_tag("x/y") == 0


def test_untag_selected(conn):
    r1 = _load(conn, "i1", "conv one", "2026-01-01T00:00:00", tagged_with=["x/y"])
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)

    n = state.untag_selected("x/y")
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 0


def test_untag_selected_only_counts_rows_that_actually_had_it(conn):
    r1 = _load(conn, "j1", "has tag", "2026-01-01T00:00:00", tagged_with=["x/y"])
    r2 = _load(conn, "j2", "no tag", "2026-01-02T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.toggle(r2.conv_id)

    n = state.untag_selected("x/y")
    assert n == 1


def test_untag_unknown_tag_is_a_noop(conn):
    r1 = _load(conn, "k1", "conv", "2026-01-01T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    assert state.untag_selected("does/not/exist") == 0


# ─────────────── recently used ───────────────


def test_recently_used_tracks_and_dedupes(conn):
    r1 = _load(conn, "l1", "conv", "2026-01-01T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.apply_tag("a")
    state.apply_tag("b")
    state.apply_tag("a")  # re-touch — should move to front, not duplicate
    assert state.recently_used == ["a", "b"]


def test_recently_used_caps_at_limit(conn):
    r1 = _load(conn, "m1", "conv", "2026-01-01T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    for i in range(12):
        state.apply_tag(f"tag{i}")
    assert len(state.recently_used) == 8  # RECENT_LIMIT
    assert state.recently_used[0] == "tag11"


# ─────────────── undo ───────────────


def test_undo_reverses_last_apply(conn):
    r1 = _load(conn, "n1", "conv one", "2026-01-01T00:00:00")
    r2 = _load(conn, "n2", "conv two", "2026-01-02T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.toggle(r2.conv_id)
    state.apply_tag("x/y")
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 2

    assert state.can_undo()
    n = state.undo()
    assert n == 2
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 0
    assert not state.can_undo()


def test_undo_reverses_last_untag(conn):
    r1 = _load(conn, "o1", "conv", "2026-01-01T00:00:00", tagged_with=["x/y"])
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.untag_selected("x/y")
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 0

    state.undo()
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 1


def test_undo_never_removes_a_tag_that_predates_the_action(conn):
    """If a selected conversation already had the tag before apply_tag ran,
    undo must not remove it — apply_tag only tracks what it actually changed."""
    r1 = _load(conn, "p1", "already tagged", "2026-01-01T00:00:00", tagged_with=["x/y"])
    r2 = _load(conn, "p2", "newly tagged", "2026-01-02T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.toggle(r2.conv_id)
    state.apply_tag("x/y")

    state.undo()
    remaining = conn.execute(
        "SELECT target_id FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = 'x/y'"
    ).fetchall()
    assert [r["target_id"] for r in remaining] == [r1.conv_id]


def test_undo_only_reverses_a_single_action_deep(conn):
    r1 = _load(conn, "q1", "conv", "2026-01-01T00:00:00")
    state = ScreenState(conn)
    state.search()
    state.toggle(r1.conv_id)
    state.apply_tag("a")
    state.apply_tag("b")  # only this is undoable now

    state.undo()
    names = {
        r["name"] for r in conn.execute(
            "SELECT t.name FROM tags t JOIN tag_links tl ON tl.tag_id = t.id WHERE tl.target_id = ?",
            (r1.conv_id,),
        )
    }
    assert names == {"a"}
    assert not state.can_undo()


def test_undo_with_nothing_to_undo(conn):
    state = ScreenState(conn)
    assert state.undo() == 0


# ─────────────── tag tree ───────────────


def test_tag_tree_builds_nested_structure(conn):
    tags.tag_add(conn, "tech/macos/npm", "1=0")  # no matches, just create the tag row
    state = ScreenState(conn)
    tree = state.tag_tree()
    assert "tech" in tree
    assert "macos" in tree["tech"].children
    assert "npm" in tree["tech"].children["macos"].children
    assert tree["tech"].children["macos"].children["npm"].path == "tech/macos/npm"

from __future__ import annotations

import pytest

from chatvault import brain, brain_db
from chatvault.brain.core import BrainError
from chatvault.brain_screen_state import BrainScreenState


@pytest.fixture()
def bconn(vault):
    conn = brain_db.connect(vault)
    yield conn
    conn.close()


def _add(bconn, title, why="because", **kw):
    return brain.add_entry(bconn, title, why=why, **kw)["item_id"]


# Multi-item scenarios below use an explicit `ref=` when creating the entry
# they need to address by name, rather than str(item_id) — chatvault.brain.
# core.resolve_item treats a bare digit string as a *row number* in the
# active list (most-recent-first) before ever falling back to it as a
# literal id, so str(item_id) is only unambiguous when it's the sole item in
# the vault. Using a ref sidesteps that ambiguity entirely, the same way
# tests/test_brain.py does when it needs to name more than one item.


# ─────────────── filtering: kind ───────────────


def test_filter_by_kind(bconn):
    _add(bconn, "a note", kind="note")
    _add(bconn, "a fact", kind="fact")
    state = BrainScreenState(bconn)
    state.search(kind="fact")
    assert [e.title for e in state.results] == ["a fact"]
    assert state.total_matches == 1


# ─────────────── filtering: tag ───────────────


def test_filter_by_tag(bconn):
    _add(bconn, "tagged one", ref="tagged-one")
    _add(bconn, "untagged")
    brain.tag_entry(bconn, "tagged-one", ["tech/macos"])
    state = BrainScreenState(bconn)
    state.search(tag="tech/macos")
    assert [e.title for e in state.results] == ["tagged one"]


def test_filter_by_tag_wildcard(bconn):
    _add(bconn, "child tag", ref="child-tag")
    brain.tag_entry(bconn, "child-tag", ["tech/macos/npm"])
    state = BrainScreenState(bconn)
    state.search(tag="tech/macos/**")
    assert [e.title for e in state.results] == ["child tag"]


# ─────────────── filtering: collection ───────────────


def test_filter_by_collection(bconn):
    id1 = _add(bconn, "in collection", ref="in-collection")
    _add(bconn, "not in collection")
    brain.add_collection(bconn, "reading-list", why="things to read")
    brain.add_to_collection(bconn, "reading-list", ["in-collection"])
    state = BrainScreenState(bconn)
    state.search(collection="reading-list")
    assert [e.title for e in state.results] == ["in collection"]
    assert state.results[0].item_id == id1


def test_filter_by_collection_with_active_search_query(bconn):
    """Collection is a local post-filter over FTS results too (find_entries
    itself doesn't know about collections) — must still narrow correctly."""
    id1 = _add(bconn, "searchable member", ref="searchable-member")
    id2 = _add(bconn, "searchable non-member")
    brain.add_collection(bconn, "reading-list", why="things to read")
    brain.add_to_collection(bconn, "reading-list", ["searchable-member"])
    state = BrainScreenState(bconn)
    state.search(query="searchable", collection="reading-list")
    assert [e.item_id for e in state.results] == [id1]
    assert id2 not in [e.item_id for e in state.results]


# ─────────────── filtering: status ───────────────


def test_filter_by_status_defaults_to_active(bconn):
    id1 = _add(bconn, "active one")
    _add(bconn, "archived one", ref="archived-one")
    brain.archive_entry(bconn, "archived-one")
    state = BrainScreenState(bconn)
    state.search()
    assert [e.item_id for e in state.results] == [id1]


def test_filter_by_status_archived(bconn):
    _add(bconn, "active one")
    id2 = _add(bconn, "archived one", ref="archived-one")
    brain.archive_entry(bconn, "archived-one")
    state = BrainScreenState(bconn)
    state.search(status="archived")
    assert [e.item_id for e in state.results] == [id2]


def test_filter_by_status_any(bconn):
    id1 = _add(bconn, "active one")
    id2 = _add(bconn, "archived one", ref="archived-one")
    brain.archive_entry(bconn, "archived-one")
    state = BrainScreenState(bconn)
    state.search(status="any")
    assert {e.item_id for e in state.results} == {id1, id2}


# ─────────────── filtering: confidence / sensitivity ───────────────


def test_filter_by_confidence(bconn):
    _add(bconn, "high", confidence="high")
    _add(bconn, "low", confidence="low")
    state = BrainScreenState(bconn)
    state.search(confidence="high")
    assert [e.title for e in state.results] == ["high"]


def test_filter_by_sensitivity(bconn):
    _add(bconn, "public", sensitivity="normal")
    _add(bconn, "private", sensitivity="secret")
    state = BrainScreenState(bconn)
    state.search(sensitivity="secret")
    assert [e.title for e in state.results] == ["private"]


# ─────────────── search (FTS) ───────────────


def test_search_query_matches_title(bconn):
    _add(bconn, "docker compose tips")
    _add(bconn, "unrelated thing")
    state = BrainScreenState(bconn)
    state.search(query="docker")
    assert [e.title for e in state.results] == ["docker compose tips"]


def test_search_filters_persist_across_calls(bconn):
    _add(bconn, "npm trick", kind="fact")
    state = BrainScreenState(bconn)
    state.search(kind="fact")
    assert state.kind == "fact"
    state.search(query="npm")
    assert state.kind == "fact"
    assert state.query == "npm"


def test_clear_filters_resets_everything(bconn):
    _add(bconn, "a", kind="fact")
    _add(bconn, "b", kind="note")
    state = BrainScreenState(bconn)
    state.search(kind="fact", query="a")
    assert len(state.results) == 1
    state.clear_filters()
    assert state.kind is None
    assert state.query is None
    assert len(state.results) == 2


def test_filter_by_tag_helper_sets_and_searches(bconn):
    id1 = _add(bconn, "tagged", ref="tagged")
    _add(bconn, "not tagged")
    brain.tag_entry(bconn, "tagged", ["x/y"])
    state = BrainScreenState(bconn)
    state.filter_by_tag("x/y")
    assert state.tag == "x/y"
    assert [e.item_id for e in state.results] == [id1]

    state.filter_by_tag(None)
    assert state.tag is None
    assert len(state.results) == 2


# ─────────────── detail resolution ───────────────


def test_resolve_detail_includes_tags_links_history_collections(bconn):
    id1 = _add(bconn, "root entry", why="root reason", ref="root")
    id2 = _add(bconn, "linked entry", why="linked reason", ref="linked")
    brain.tag_entry(bconn, "root", ["tech/macos"])
    brain.add_rel(bconn, "relates_to")
    brain.link_entry(bconn, "root", "linked", "relates_to", note="see also")
    brain.add_collection(bconn, "reading-list", why="stuff")
    brain.add_to_collection(bconn, "reading-list", ["root"])
    brain.edit_entry(bconn, "root", title="root entry (edited)")

    state = BrainScreenState(bconn)
    detail = state.view("root")

    assert detail.item_id == id1
    assert detail.title == "root entry (edited)"
    assert detail.tags == ["tech/macos"]
    assert detail.collections == ["reading-list"]
    assert len(detail.links_out) == 1
    assert detail.links_out[0].item_id == id2
    assert detail.links_out[0].rel == "relates_to"
    assert detail.links_in == []
    assert len(detail.history) == 1
    assert detail.history[0].field == "title"


def test_resolve_detail_includes_incoming_links(bconn):
    id1 = _add(bconn, "target", ref="target")
    id2 = _add(bconn, "source", ref="source")
    brain.add_rel(bconn, "relates_to")
    brain.link_entry(bconn, "source", "target", "relates_to")

    state = BrainScreenState(bconn)
    detail = state.view("target")
    assert detail.links_out == []
    assert len(detail.links_in) == 1
    assert detail.links_in[0].item_id == id2
    assert detail.item_id == id1


def test_resolve_detail_by_row_number_matches_ref_lookup(bconn):
    """`view()` accepts the same ref/alias/row-number conventions as every
    other <ref>-taking Brain command (chatvault.brain.core.resolve_item)
    when given a string — a plain int, by contrast, always means the
    literal primary key (see test_view_with_int_id_is_a_direct_lookup)."""
    id1 = _add(bconn, "only entry")
    state = BrainScreenState(bconn)
    detail = state.view("1")  # row 1 of the (single-item) active list
    assert detail.item_id == id1


def test_view_with_int_id_is_a_direct_primary_key_lookup(bconn):
    """A plain int must never be reinterpreted as a row number — link
    navigation (follow_link) always hands view()/resolve_detail a real
    item_links.to_id/from_id, which must resolve to exactly that row even
    when it isn't row 1 of the active list."""
    _add(bconn, "first (row 1, most recent)")
    id2 = _add(bconn, "second")
    # id2 was created before "first", so id2 is NOT row 1 — a naive
    # string-coercing lookup would resolve to the wrong item here.
    state = BrainScreenState(bconn)
    detail = state.view(id2)
    assert detail.item_id == id2


def test_resolve_detail_unknown_ref_raises(bconn):
    state = BrainScreenState(bconn)
    with pytest.raises(BrainError):
        state.view("does-not-exist")


def test_view_does_not_touch_use_count(bconn):
    """Unlike `show entry`, browsing in the screen must not bump
    use_count/last_used_at — that's a deliberate CLI-only side effect."""
    id1 = _add(bconn, "entry")
    state = BrainScreenState(bconn)
    state.view(id1)
    row = bconn.execute("SELECT use_count, last_used_at FROM items WHERE id = ?", (id1,)).fetchone()
    assert row["use_count"] == 0
    assert row["last_used_at"] is None


# ─────────────── navigation-state transitions ───────────────


def test_view_sets_current_id_without_touching_nav_stack(bconn):
    id1 = _add(bconn, "one")
    state = BrainScreenState(bconn)
    state.view(id1)
    assert state.current_id == id1
    assert state.nav_stack == []
    assert not state.can_go_back()


def test_follow_link_pushes_current_onto_stack(bconn):
    id1 = _add(bconn, "one", ref="one")
    id2 = _add(bconn, "two", ref="two")
    brain.add_rel(bconn, "relates_to")
    brain.link_entry(bconn, "one", "two", "relates_to")

    state = BrainScreenState(bconn)
    state.view(id1)
    detail = state.follow_link(id2)

    assert detail.item_id == id2
    assert state.current_id == id2
    assert state.nav_stack == [id1]
    assert state.can_go_back()


def test_follow_link_chain_and_go_back_unwinds_in_order(bconn):
    id1 = _add(bconn, "one", ref="one")
    id2 = _add(bconn, "two", ref="two")
    id3 = _add(bconn, "three", ref="three")
    brain.add_rel(bconn, "relates_to")
    brain.link_entry(bconn, "one", "two", "relates_to")
    brain.link_entry(bconn, "two", "three", "relates_to")

    state = BrainScreenState(bconn)
    state.view(id1)
    state.follow_link(id2)
    state.follow_link(id3)
    assert state.nav_stack == [id1, id2]

    back1 = state.go_back()
    assert back1.item_id == id2
    assert state.current_id == id2
    assert state.nav_stack == [id1]

    back2 = state.go_back()
    assert back2.item_id == id1
    assert state.current_id == id1
    assert state.nav_stack == []
    assert not state.can_go_back()


def test_go_back_with_nothing_to_go_back_to_returns_none(bconn):
    id1 = _add(bconn, "one")
    state = BrainScreenState(bconn)
    state.view(id1)
    assert state.go_back() is None
    assert state.current_id == id1  # unchanged


def test_close_detail_clears_current_and_stack(bconn):
    id1 = _add(bconn, "one", ref="one")
    id2 = _add(bconn, "two", ref="two")
    brain.add_rel(bconn, "relates_to")
    brain.link_entry(bconn, "one", "two", "relates_to")

    state = BrainScreenState(bconn)
    state.view(id1)
    state.follow_link(id2)
    state.close_detail()

    assert state.current_id is None
    assert state.nav_stack == []


def test_follow_link_unknown_target_raises_and_leaves_stack_untouched(bconn):
    id1 = _add(bconn, "one")
    state = BrainScreenState(bconn)
    state.view(id1)
    with pytest.raises(BrainError):
        state.follow_link(999999)
    assert state.current_id == id1
    assert state.nav_stack == []


# ─────────────── tag tree ───────────────


def test_tag_tree_builds_nested_structure(bconn):
    _add(bconn, "entry", ref="entry")
    brain.tag_entry(bconn, "entry", ["tech/macos/npm"])
    state = BrainScreenState(bconn)
    tree = state.tag_tree()
    assert "tech" in tree
    assert "macos" in tree["tech"].children
    assert "npm" in tree["tech"].children["macos"].children
    assert tree["tech"].children["macos"].children["npm"].path == "tech/macos/npm"
    assert tree["tech"].children["macos"].children["npm"].own == 1

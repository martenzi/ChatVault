"""Lightweight pilot smoke tests for the actual Textual app (screen.py) —
verifying the real widget/event wiring end to end, on top of the exhaustive
logic coverage in test_screen_state.py. No pixel-level assertions, per the
project's own acceptance bar for this feature: just that a select ->
navigate-tree -> apply -> undo loop genuinely works through the widgets a
user would actually interact with.

Run synchronously via asyncio.run() rather than pytest-asyncio (not a
project dependency) — matches how this was manually verified during
development."""

from __future__ import annotations

import asyncio

from chatvault import loader, tags
from chatvault.adapters.base import make_block, make_conversation, make_message
from chatvault.screen import TaggingScreenApp
from textual.widgets import SelectionList, Tree


def _load(conn, native_id, title, started_at):
    cir = make_conversation(
        platform="synthetic", native_id=native_id, title=title, project=None,
        started_at=started_at, ended_at=started_at, source_path="synthetic", raw={},
        messages=[make_message(0, "user", None, started_at, [make_block(0, "text", "hello", raw={})], raw={})],
    )
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result


def test_app_mounts_and_populates_results_and_tree(conn):
    _load(conn, "a1", "conv one", "2026-01-01T00:00:00")
    _load(conn, "a2", "conv two", "2026-01-02T00:00:00")
    tags.get_or_create_tag(conn, "tech/macos/npm")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test() as pilot:
            await pilot.pause()
            sel = app.query_one("#results", SelectionList)
            assert sel.option_count == 2
            tree = app.query_one("#tags", Tree)
            assert len(tree.root.children) == 1  # 'tech'

    asyncio.run(body())


def test_select_tag_apply_via_tree_enter_persists_to_db(conn):
    r1 = _load(conn, "b1", "conv one", "2026-01-01T00:00:00")
    tags.get_or_create_tag(conn, "tech/macos/npm")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sel = app.query_one("#results", SelectionList)
            sel.focus()
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            assert app.state.selected == {r1.conv_id}

            tree = app.query_one("#tags", Tree)
            tree.focus()
            await pilot.pause()
            for _ in range(3):  # tags -> tech -> macos -> npm
                await pilot.press("down")
                await pilot.pause()
            assert app._tree_cursor_tag == "tech/macos/npm"

            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(body())

    row = conn.execute(
        "SELECT source FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = 'tech/macos/npm' AND tl.target_id = ?",
        (r1.conv_id,),
    ).fetchone()
    assert row is not None
    assert row["source"] == "manual"


def test_untag_via_tree_cursor_and_u_key(conn):
    r1 = _load(conn, "c1", "conv one", "2026-01-01T00:00:00")
    tags.tag_add(conn, "tech/macos/npm", f"id = {r1.conv_id}")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sel = app.query_one("#results", SelectionList)
            sel.focus()
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()

            tree = app.query_one("#tags", Tree)
            tree.focus()
            await pilot.pause()
            for _ in range(3):
                await pilot.press("down")
                await pilot.pause()
            await pilot.press("u")
            await pilot.pause()

    asyncio.run(body())

    n = conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0]
    assert n == 0


def test_undo_key_reverses_last_apply(conn):
    r1 = _load(conn, "d1", "conv one", "2026-01-01T00:00:00")
    tags.get_or_create_tag(conn, "tech/macos/npm")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sel = app.query_one("#results", SelectionList)
            sel.focus()
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()

            tree = app.query_one("#tags", Tree)
            tree.focus()
            for _ in range(3):
                await pilot.press("down")
                await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 1

            await pilot.press("z")
            await pilot.pause()

    asyncio.run(body())
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 0


def test_select_all_and_none_keys(conn):
    _load(conn, "e1", "conv one", "2026-01-01T00:00:00")
    _load(conn, "e2", "conv two", "2026-01-02T00:00:00")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#results", SelectionList).focus()
            await pilot.press("a")
            await pilot.pause()
            assert len(app.state.selected) == 2

            await pilot.press("n")
            await pilot.pause()
            assert len(app.state.selected) == 0

    asyncio.run(body())


def test_read_pane_opens_and_escape_returns_with_selection_intact(conn):
    r1 = _load(conn, "f1", "conv one", "2026-01-01T00:00:00")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sel = app.query_one("#results", SelectionList)
            sel.focus()
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            assert app.state.selected == {r1.conv_id}

            await pilot.press("r")
            await pilot.pause()
            assert len(app.screen_stack) == 2  # base Screen + ReadPane

            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert app.state.selected == {r1.conv_id}  # selection survived

    asyncio.run(body())


def test_semantic_toggle_is_enabled_and_wired(conn, monkeypatch):
    """The checkbox used to be a disabled 'coming soon' placeholder; now that
    semantic search is built, it must be a real, enabled control that
    actually switches the query mode — not silently still doing keyword
    search under a semantic-looking label. Mocks the embedding call itself
    (no real Ollama dependency in a unit test) but drives the toggle through
    the real widget event, same as everything else in this file."""
    from textual.widgets import Checkbox, Input

    _load(conn, "s1", "conv one", "2026-01-01T00:00:00")
    calls = []

    def fake_semantic_search(conn_, query, limit=20, model=None):
        calls.append(query)
        return list(conn_.execute("SELECT * FROM v_conversations LIMIT 1"))

    monkeypatch.setattr("chatvault.screen_state.embeddings.semantic_search", fake_semantic_search)

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test() as pilot:
            await pilot.pause()
            sem = app.query_one("#semantic", Checkbox)
            assert sem.disabled is False

            sem.toggle()
            await pilot.pause()
            assert app.state.semantic is True

            search = app.query_one("#search", Input)
            search.focus()
            await pilot.pause()
            search.value = "some query"
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(body())
    assert calls == ["some query"]


def test_untagged_only_filter_updates_results(conn):
    r1 = _load(conn, "g1", "no tags", "2026-01-01T00:00:00")
    r2 = _load(conn, "g2", "has tag", "2026-01-02T00:00:00")
    tags.tag_add(conn, "x/y", f"id = {r2.conv_id}")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import Checkbox

            box = app.query_one("#untagged_only", Checkbox)
            box.toggle()
            await pilot.pause()
            assert len(app.state.results) == 1
            assert app.state.results[0]["id"] == r1.conv_id

    asyncio.run(body())


def test_titles_with_literal_brackets_do_not_crash_the_results_list(conn):
    """Regression test: found against real production data, not a fixture.
    A real conversation titled '@[/Users/.../sample-Standalone/...]' crashed
    the app immediately on mount — Rich markup parsing treats '[...]' as a
    style tag, and unmatched/unknown brackets raise MarkupError. Result
    labels must never be markup-parsed."""
    _load(conn, "h1", "@[/Users/username/vault_main/sample-Standalone/notes.md] some text", "2026-01-01T00:00:00")
    _load(conn, "h2", "[not a style][/not a closing tag either]", "2026-01-02T00:00:00")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test() as pilot:
            await pilot.pause()
            sel = app.query_one("#results", SelectionList)
            assert sel.option_count == 2

    asyncio.run(body())


def test_read_pane_with_bracketed_title_does_not_crash(conn):
    r1 = _load(conn, "i1", "@[weird/bracket/title.md] rest of the title", "2026-01-01T00:00:00")

    async def body():
        app = TaggingScreenApp(conn)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._open_read_pane(r1.conv_id)
            await pilot.pause()
            assert len(app.screen_stack) == 2

    asyncio.run(body())

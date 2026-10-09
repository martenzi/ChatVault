"""Lightweight pilot smoke tests for the actual Textual app (brain_screen.py)
— verifying the real widget/event wiring end to end, on top of the
exhaustive logic coverage in test_brain_screen_state.py. Mirrors
tests/test_screen_app.py's own approach (run_test() + asyncio.run(), no
pytest-asyncio dependency, no pixel-level assertions) for consistency
between the two tools' test suites."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import OptionList, Tree

from chatvault import brain, brain_db
from chatvault.brain_screen import BrainScreenApp, ReadPane


@pytest.fixture()
def bconn(vault):
    conn = brain_db.connect(vault)
    yield conn
    conn.close()


def _add(bconn, title, why="because", **kw):
    return brain.add_entry(bconn, title, why=why, **kw)["item_id"]


def test_app_mounts_and_populates_results_and_tag_tree(bconn):
    _add(bconn, "one", ref="one")
    _add(bconn, "two")
    brain.tag_entry(bconn, "one", ["tech/macos"])

    async def body():
        app = BrainScreenApp(bconn)
        async with app.run_test() as pilot:
            await pilot.pause()
            results = app.query_one("#results", OptionList)
            assert results.option_count == 2
            tree = app.query_one("#tags", Tree)
            assert len(tree.root.children) == 1  # 'tech'

    asyncio.run(body())


def test_read_pane_opens_shows_detail_and_escape_returns(bconn):
    id1 = _add(bconn, "conv one", ref="conv-one")

    async def body():
        app = BrainScreenApp(bconn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            results = app.query_one("#results", OptionList)
            results.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == 2  # base screen + ReadPane
            pane = app.screen_stack[-1]
            assert isinstance(pane, ReadPane)
            assert pane.detail.item_id == id1

            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(body())


def test_following_a_link_updates_the_read_pane_and_escape_steps_back(bconn):
    id1 = _add(bconn, "root", ref="root")
    id2 = _add(bconn, "linked", ref="linked")
    brain.add_rel(bconn, "relates_to")
    brain.link_entry(bconn, "root", "linked", "relates_to")

    async def body():
        app = BrainScreenApp(bconn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            results = app.query_one("#results", OptionList)
            results.focus()
            # results are newest-first: "linked" then "root" — arrow down to "root"
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            pane = app.screen_stack[-1]
            assert pane.detail.item_id == id1

            links = pane.query_one("#read_links", OptionList)
            links.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert pane.detail.item_id == id2  # followed the link
            assert app.state.nav_stack == [id1]

            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 2  # stepped back, pane still open
            assert pane.detail.item_id == id1

            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1  # now fully closed

    asyncio.run(body())


def test_tag_tree_click_filters_results(bconn):
    _add(bconn, "tagged", ref="tagged")
    _add(bconn, "untagged")
    brain.tag_entry(bconn, "tagged", ["x"])

    async def body():
        app = BrainScreenApp(bconn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            tree = app.query_one("#tags", Tree)
            tree.focus()
            await pilot.press("down")  # onto 'x'
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.tag == "x"
            results = app.query_one("#results", OptionList)
            assert results.option_count == 1

    asyncio.run(body())


def test_kind_filter_input_updates_results(bconn):
    _add(bconn, "a note", kind="note")
    _add(bconn, "a fact", kind="fact")

    async def body():
        from textual.widgets import Input

        app = BrainScreenApp(bconn)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            kind_input = app.query_one("#kind", Input)
            kind_input.focus()
            kind_input.value = "fact"
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.state.results) == 1
            assert app.state.results[0].kind == "fact"

    asyncio.run(body())


def test_titles_with_literal_brackets_do_not_crash_the_results_list(bconn):
    """Regression guard mirroring chatvault's own equivalent test: entry
    titles can contain literal '[' ']' (paths, citations), which Rich
    markup parsing would otherwise choke on if a label were built as a raw
    f-string instead of a plain (non-markup) Text object."""
    _add(bconn, "@[/Users/username/notes.md] some text")
    _add(bconn, "[not a style][/not a closing tag either]")

    async def body():
        app = BrainScreenApp(bconn)
        async with app.run_test() as pilot:
            await pilot.pause()
            results = app.query_one("#results", OptionList)
            assert results.option_count == 2

    asyncio.run(body())

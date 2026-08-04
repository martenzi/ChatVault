from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from rich.text import Text
from textual.widgets import Checkbox, Footer, Header, Input, SelectionList, Static, Tree
from textual.widgets.selection_list import Selection

from chatvault import export as export_mod
from chatvault.screen_state import ScreenState

# Undo is bound to a plain 'z', not ctrl+z/cmd+z: both of those are
# intercepted as SIGTSTP (suspend) by most terminal emulators before a raw-
# mode TUI app ever sees the keystroke, so binding them would silently do
# nothing (or background the whole app) instead of undoing. 'z' is free,
# mnemonic-adjacent, and reaches the app reliably. Documented here per the
# spec's own "use whatever's idiomatic ... note what you chose."
UNDO_KEY = "z"


def _result_label(row: sqlite3.Row) -> Text:
    """A plain (non-markup) Text object, not an f-string handed straight to
    the widget — real conversation titles routinely contain literal '[' ']'
    (paths, citations, bracketed notes), which Textual/Rich would otherwise
    parse as markup tags and crash on (found against real production data,
    not a fixture: a title starting '[/Users/.../SomeProject-S...' broke the
    unescaped version of this immediately)."""
    date = (row["started_at"] or "")[:10] or "?"
    title = (row["title"] or "Untitled")[:56]
    return Text(f"{date}  {row['platform']:<12}  {title}  ({row['message_count']} msgs)")


class ReadPane(ModalScreen):
    """Enter opens this; Escape returns to the results/tags screen with
    selection state untouched (it lives in the app/ScreenState, not here)."""

    BINDINGS = [Binding("escape", "dismiss", "back")]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            # markup=False on both — a real conversation title or exported
            # body routinely contains literal '[' ']' (paths, citations),
            # which Rich markup parsing would otherwise choke on
            yield Static(Text(self._title, style="bold"), markup=False)
            yield Static("")
            yield Static(self._content, markup=False)

    def action_dismiss(self, result=None) -> None:
        self.dismiss(result)


class TaggingScreenApp(App):
    """The tagging screen — Tagging Workflow doc §3. Filters + a checkbox
    result list + a clickable hierarchical tag tree; clicking (or Enter-ing)
    a tag applies it to every currently-selected conversation at once."""

    CSS = """
    #filters { height: auto; border: round $accent; padding: 0 1; margin-bottom: 1; }
    #filters Input { margin-right: 1; }
    #main { height: 1fr; }
    #results { width: 1fr; border: round $primary; }
    #tags { width: 1fr; border: round $primary; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #recent { height: 1; padding: 0 1; }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "search"),
        Binding("t", "tag_selected", "tag (Enter on tree)"),
        Binding("u", "untag_selected", "untag (tree cursor)"),
        Binding(UNDO_KEY, "undo", "undo"),
        Binding("a", "select_all", "select all"),
        Binding("n", "select_none", "none"),
        Binding("s", "save_view", "save view"),
        Binding("r", "read_selected", "read"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self.state = ScreenState(conn)
        self._tree_cursor_tag: str | None = None

    # ─────────────── layout ───────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="filters"):
            yield Input(placeholder="search (plain-text FTS, or check ⚡ semantic) — press Enter", id="search")
            with Horizontal():
                yield Input(placeholder="platform (blank = any)", id="platform")
                yield Checkbox("untagged only", id="untagged_only")
                yield Checkbox("⚡ semantic", id="semantic")
        with Horizontal(id="main"):
            yield SelectionList(id="results")
            yield Tree("tags", id="tags")
        yield Static("", id="status", markup=False)
        yield Static("", id="recent", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.state.search()
        self.refresh_results()
        self.refresh_tag_tree()
        # Results, not the search Input, get default focus — single-letter
        # bindings (a/n/t/u/z/s) would otherwise be swallowed as literal text
        # by a focused Input. '/' is the explicit, documented way to focus
        # search; this makes that meaningful rather than redundant.
        self.query_one("#results", SelectionList).focus()

    # ─────────────── rendering from state ───────────────

    def refresh_results(self) -> None:
        sel = self.query_one("#results", SelectionList)
        sel.clear_options()
        for row in self.state.results:
            sel.add_option(Selection(_result_label(row), row["id"], row["id"] in self.state.selected))
        self.update_status()

    def refresh_tag_tree(self) -> None:
        tree = self.query_one("#tags", Tree)
        tree.clear()
        tree.root.data = None

        def _add(parent, nodes: dict) -> None:
            for name, node in sorted(nodes.items()):
                label = f"{name} ({node.uses})" if node.uses else name
                child = parent.add(label, data=node.path)
                _add(child, node.children)
                child.expand()  # expanded by default (space still collapses) — down-arrow can then reach every node without an expand step first

        _add(tree.root, self.state.tag_tree())
        tree.root.expand()

    def update_status(self) -> None:
        status = self.query_one("#status", Static)
        mode = "⚡ semantic — ranked by meaning" if (self.state.semantic and self.state.query) else "showing"
        status.update(
            f"{len(self.state.selected)} selected  ·  {mode} {len(self.state.results)} of {self.state.total_matches} matches"
        )
        recent = self.query_one("#recent", Static)
        recent.update(
            "recently used:  " + "  ".join(f"[{t}]" for t in self.state.recently_used)
            if self.state.recently_used else ""
        )

    # ─────────────── events ───────────────

    @on(SelectionList.SelectedChanged, "#results")
    def _on_selection_changed(self, event: SelectionList.SelectedChanged) -> None:
        self.state.selected = set(event.selection_list.selected)
        self.update_status()

    @on(Input.Submitted, "#search")
    def _on_search_submitted(self, event: Input.Submitted) -> None:
        self.state.search(query=event.value or None)
        self.refresh_results()
        if self.state.semantic_error:
            self.notify(self.state.semantic_error, severity="error", timeout=8)

    @on(Input.Submitted, "#platform")
    def _on_platform_submitted(self, event: Input.Submitted) -> None:
        self.state.search(platform=event.value or None)
        self.refresh_results()

    @on(Checkbox.Changed, "#untagged_only")
    def _on_untagged_toggled(self, event: Checkbox.Changed) -> None:
        self.state.search(untagged_only=event.value)
        self.refresh_results()

    @on(Checkbox.Changed, "#semantic")
    def _on_semantic_toggled(self, event: Checkbox.Changed) -> None:
        self.state.search(semantic=event.value)
        self.refresh_results()
        if self.state.semantic_error:
            self.notify(self.state.semantic_error, severity="error", timeout=8)


    @on(Tree.NodeHighlighted, "#tags")
    def _on_tag_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._tree_cursor_tag = event.node.data

    @on(Tree.NodeSelected, "#tags")
    def _on_tag_selected(self, event: Tree.NodeSelected) -> None:
        """Click, or Enter with the tree focused — applies the tag to every
        currently-selected conversation. This is the primary interaction the
        spec describes ('clicking a tag in the right-hand tree applies to
        everything selected at once')."""
        path = event.node.data
        if not path:
            return
        n = self.state.apply_tag(path)
        self.refresh_tag_tree()
        self.refresh_results()
        self.notify(f"tagged {n} conversation(s) with '{path}'" if n else f"already tagged with '{path}'")

    # ─────────────── keybindings ───────────────

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_select_all(self) -> None:
        self.state.select_all()
        self.query_one("#results", SelectionList).select_all()
        self.update_status()

    def action_select_none(self) -> None:
        self.state.select_none()
        self.query_one("#results", SelectionList).deselect_all()
        self.update_status()

    def action_tag_selected(self) -> None:
        """'t' moves focus to the tag tree — arrow to a tag, press Enter (or
        click) to apply it. Applying and un-applying both go through the same
        tree so there's one place to learn, not two: highlight a node and
        press Enter to add it, or Enter again style isn't needed — untag is
        available as a distinct action on the highlighted node (see
        action_untag_selected) rather than overloading Enter with a toggle,
        which would make 'did that just apply or remove?' ambiguous."""
        self.query_one("#tags", Tree).focus()
        self.notify("navigate to a tag, press Enter to apply to the current selection")

    def action_untag_selected(self) -> None:
        if not self._tree_cursor_tag:
            self.notify("highlight a tag in the tree first")
            return
        n = self.state.untag_selected(self._tree_cursor_tag)
        self.refresh_tag_tree()
        self.refresh_results()
        self.notify(f"removed '{self._tree_cursor_tag}' from {n} conversation(s)")

    def action_undo(self) -> None:
        n = self.state.undo()
        self.refresh_results()
        self.refresh_tag_tree()
        self.notify(f"undone — {n} link(s) reverted" if n else "nothing to undo")

    def action_read_selected(self) -> None:
        """Bound to 'r', not literally 'enter' as the doc's own mockup lists
        it — a genuine Textual constraint, not a stylistic choice: Textual's
        SelectionList maps both space AND enter to the same internal
        action_select (toggle), stopping the underlying OptionSelected
        message before it ever bubbles to an app-level handler, and neither
        key carries any information distinguishing which one was pressed.
        There's no way to make 'enter' mean 'open' while 'space' still means
        'toggle' on this widget without event-level surgery that would risk
        breaking the toggle behavior itself. 'r' (read) reaches the app
        cleanly and doesn't collide with anything else bound here.

        Reads whichever row the cursor is currently on (not an arbitrary
        member of the selection) — you're usually peeking at the row you're
        looking at, which may not even be selected yet."""
        sel = self.query_one("#results", SelectionList)
        if sel.highlighted is None:
            self.notify("highlight a conversation first")
            return
        conv_id = sel.get_option_at_index(sel.highlighted).value
        self._open_read_pane(conv_id)

    def _open_read_pane(self, conv_id: int) -> None:
        row = self.state.conn.execute("SELECT * FROM v_conversations WHERE id = ?", (conv_id,)).fetchone()
        if row is None:
            return
        content = export_mod.render_profile(self.state.conn, row, profile="read")
        self.push_screen(ReadPane(row["title"] or "Untitled", content))

    def action_save_view(self) -> None:
        from chatvault import curation

        name = f"screen-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        clauses = []
        if self.state.platform:
            clauses.append(f"platform = '{self.state.platform}'")
        if self.state.untagged_only:
            clauses.append(
                "id NOT IN (SELECT target_id FROM tag_links WHERE target_type = 'conversation')"
            )
        sql = "SELECT id FROM v_conversations" + (f" WHERE {' AND '.join(clauses)}" if clauses else "")
        curation.view_save(self.state.conn, name, sql, description="saved from the tagging screen")
        self.notify(f"saved view '{name}'")


def run(conn: sqlite3.Connection) -> None:
    TaggingScreenApp(conn).run()

from __future__ import annotations

import sqlite3

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from rich.text import Text
from textual.widgets import Checkbox, Footer, Header, Input, OptionList, Static, Tree
from textual.widgets.option_list import Option

from chatvault.brain import entries as entries_mod
from chatvault.brain.core import BrainError
from chatvault.brain_screen_state import BrainScreenState, EntryDetail

# docs/51_SPEC_brain_parity_and_naming.md §2.1 `screen` row + §6 phase 5.
# This is chatvault/screen.py's counterpart for the Brain: a thin Textual
# presentation layer over BrainScreenState (brain_screen_state.py), which
# owns every bit of actual logic (filtering, search, detail resolution,
# link navigation) and is unit-tested independently of Textual. Nothing in
# this file talks to sqlite directly except read-only formatting helpers —
# every state change goes through BrainScreenState.
#
# §8 Q8 ("does brain screen need to reach the vault? No.") — this module,
# like brain_screen_state.py, is only ever handed a brain.sqlite connection
# (see run() at the bottom) and has no code path that opens, or could open,
# a chatvault.sqlite connection. Copying from the vault (`copy conversation`
# / `copy stashed`) stays a CLI-only act, exactly as the spec answer says —
# there is no such action anywhere in this screen.


def _result_label(entry: entries_mod.BrainEntry) -> Text:
    """A plain (non-markup) Text object, not an f-string handed straight to
    the widget — same reasoning as chatvault.screen._result_label: entry
    titles can contain literal '[' ']' that Rich would otherwise parse as
    markup and crash on."""
    date = (entry.created_at or "")[:10] or "?"
    rank = f"[{entry.rank}]" if entry.rank is not None else ""
    return Text(f"{date}  {entry.kind:<10} {rank:<4} {entry.title}")


def _format_detail(detail: EntryDetail) -> Text:
    lines = [
        f"ref: {detail.ref or '-'}   kind: {detail.kind}   status: {detail.status}",
        f"why: {detail.why}",
        f"confidence: {detail.confidence or '-'}   sensitivity: {detail.sensitivity}",
        f"rank: {detail.rank if detail.rank is not None else '-'}",
        f"saved: {detail.created_at}   vetted: {detail.vetted_at or '-'}   "
        f"used: {detail.use_count}x   edits: {detail.edit_count}",
        "-" * 40,
        f"# {detail.title}",
        "",
        detail.body or "(no body)",
    ]

    lines.append("\ntags: " + (", ".join(detail.tags) if detail.tags else "(none)"))
    lines.append("collections: " + (", ".join(detail.collections) if detail.collections else "(none)"))

    lines.append("\nlinks out:")
    if detail.links_out:
        for i, link in enumerate(detail.links_out, start=1):
            lines.append(f"  {i}. --{link.rel}--> {link.title} (#{link.item_id})")
    else:
        lines.append("  (none)")

    lines.append("\nlinks in:")
    if detail.links_in:
        offset = len(detail.links_out)
        for i, link in enumerate(detail.links_in, start=offset + 1):
            lines.append(f"  {i}. <--{link.rel}-- {link.title} (#{link.item_id})")
    else:
        lines.append("  (none)")

    lines.append("\nhistory:")
    if detail.history:
        for h in detail.history:
            lines.append(f"  {h.changed_at} [{h.origin}] {h.field}: {h.old_value!r} -> {h.new_value!r}")
    else:
        lines.append("  (none)")

    return Text("\n".join(lines))


class ReadPane(ModalScreen):
    """Enter (or 'r') opens this from the results list; Escape steps back
    one link hop if the user has followed any links, otherwise closes the
    pane and returns to the list — mirrors chatvault.screen.ReadPane's
    Escape-to-dismiss, extended with BrainScreenState's link back-stack,
    which conversations don't have an equivalent of."""

    BINDINGS = [
        Binding("escape", "back_or_dismiss", "back"),
        Binding("l", "focus_links", "links"),
    ]

    def __init__(self, state: BrainScreenState, detail: EntryDetail) -> None:
        super().__init__()
        self.state = state
        self.detail = detail

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(Text(self.detail.title, style="bold"), markup=False, id="read_title")
            yield Static("", markup=False, id="read_body")
        yield OptionList(id="read_links")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_pane()

    def _refresh_pane(self) -> None:
        self.query_one("#read_title", Static).update(Text(self.detail.title, style="bold"))
        self.query_one("#read_body", Static).update(_format_detail(self.detail))

        links = self.query_one("#read_links", OptionList)
        links.clear_options()
        for link in self.detail.links_out:
            links.add_option(Option(f"--{link.rel}--> {link.title}", id=str(link.item_id)))
        for link in self.detail.links_in:
            links.add_option(Option(f"<--{link.rel}-- {link.title}", id=str(link.item_id)))
        if links.option_count:
            links.highlighted = 0

    @on(OptionList.OptionSelected, "#read_links")
    def _on_link_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter (or click) on a link in the links list follows it — pushes
        the current entry onto BrainScreenState's back-stack and re-renders
        this same pane with the linked entry's detail, rather than pushing
        a second ModalScreen."""
        item_id = int(event.option.id)
        try:
            self.detail = self.state.follow_link(item_id)
        except BrainError as exc:
            self.notify(str(exc), severity="error")
            return
        self._refresh_pane()

    def action_focus_links(self) -> None:
        self.query_one("#read_links", OptionList).focus()

    def action_back_or_dismiss(self) -> None:
        if self.state.can_go_back():
            self.detail = self.state.go_back()
            self._refresh_pane()
        else:
            self.state.close_detail()
            self.dismiss()


class BrainScreenApp(App):
    """The Brain screen (§2.1 `screen` row) — browse/filter/search entries,
    click a tag in the tree to filter by it, read one entry's full detail
    (tags, links, history, collections) and follow links between entries.
    Read-only: no tagging/archiving/editing happens here (those stay CLI
    acts, same spirit as §8 Q8 keeping vault-copy CLI-only)."""

    CSS = """
    #filters { height: auto; border: round $accent; padding: 0 1; margin-bottom: 1; }
    #filters Input { margin-right: 1; }
    #main { height: 1fr; }
    #results { width: 1fr; border: round $primary; }
    #tags { width: 1fr; border: round $primary; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    ReadPane VerticalScroll { height: 1fr; padding: 0 1; }
    ReadPane #read_links { height: 8; border: round $primary; }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "search"),
        # OptionList (unlike ChatVault's SelectionList) has no space/enter
        # collision to work around — Enter on a highlighted result already
        # fires OptionSelected (see _on_result_selected) and opens the read
        # pane. 'r' is kept as an explicit alias anyway, for parity with
        # chatvault.screen's own 'r' binding and as a way to open the pane
        # without moving focus onto the results list first.
        Binding("r", "read_highlighted", "read"),
        Binding("c", "clear_filters", "clear filters"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self.state = BrainScreenState(conn)

    # ─────────────── layout ───────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="filters"):
            yield Input(placeholder="search (title/why/body/tags FTS) — press Enter", id="search")
            with Horizontal():
                yield Input(placeholder="kind (blank = any)", id="kind")
                yield Input(placeholder="collection (blank = any)", id="collection")
                yield Input(placeholder="confidence (blank = any)", id="confidence")
                yield Input(placeholder="sensitivity (blank = any)", id="sensitivity")
                yield Checkbox("show archived too", id="show_archived")
        with Horizontal(id="main"):
            yield OptionList(id="results")
            yield Tree("tags", id="tags")
        yield Static("", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.state.search()
        self.refresh_results()
        self.refresh_tag_tree()
        self.query_one("#results", OptionList).focus()

    # ─────────────── rendering from state ───────────────

    def refresh_results(self) -> None:
        results = self.query_one("#results", OptionList)
        results.clear_options()
        for entry in self.state.results:
            results.add_option(Option(_result_label(entry), id=str(entry.item_id)))
        # OptionList doesn't auto-highlight its first option the way
        # SelectionList/Tree do — without this, Enter/'r' on a freshly
        # (re)populated list has nothing highlighted to act on.
        if self.state.results:
            results.highlighted = 0
        self.update_status()

    def refresh_tag_tree(self) -> None:
        from chatvault.brain_screen_state import cumulative_count

        tree = self.query_one("#tags", Tree)
        tree.clear()
        tree.root.data = None

        def _add(parent, nodes: dict) -> None:
            for name, node in sorted(nodes.items()):
                count = cumulative_count(node)
                label = f"{name} ({count})" if count else name
                child = parent.add(label, data=node.path)
                _add(child, node.children)
                child.expand()

        _add(tree.root, self.state.tag_tree())
        tree.root.expand()

    def update_status(self) -> None:
        status = self.query_one("#status", Static)
        active_filters = ", ".join(
            f"{k}={v}"
            for k, v in (
                ("tag", self.state.tag), ("kind", self.state.kind), ("collection", self.state.collection),
                ("confidence", self.state.confidence), ("sensitivity", self.state.sensitivity),
                ("status", self.state.status),
            )
            if v
        )
        suffix = f"  ·  filters: {active_filters}" if active_filters else ""
        status.update(f"showing {len(self.state.results)} of {self.state.total_matches} matches{suffix}")

    # ─────────────── events ───────────────

    @on(Input.Submitted, "#search")
    def _on_search_submitted(self, event: Input.Submitted) -> None:
        self.state.search(query=event.value or "")
        self.refresh_results()

    @on(Input.Submitted, "#kind")
    def _on_kind_submitted(self, event: Input.Submitted) -> None:
        self.state.search(kind=event.value or "")
        self.refresh_results()

    @on(Input.Submitted, "#collection")
    def _on_collection_submitted(self, event: Input.Submitted) -> None:
        self.state.search(collection=event.value or "")
        self.refresh_results()

    @on(Input.Submitted, "#confidence")
    def _on_confidence_submitted(self, event: Input.Submitted) -> None:
        self.state.search(confidence=event.value or "")
        self.refresh_results()

    @on(Input.Submitted, "#sensitivity")
    def _on_sensitivity_submitted(self, event: Input.Submitted) -> None:
        self.state.search(sensitivity=event.value or "")
        self.refresh_results()

    @on(Checkbox.Changed, "#show_archived")
    def _on_show_archived_toggled(self, event: Checkbox.Changed) -> None:
        self.state.search(status="any" if event.value else "active")
        self.refresh_results()

    @on(Tree.NodeSelected, "#tags")
    def _on_tag_selected(self, event: Tree.NodeSelected) -> None:
        """Click (or Enter with the tree focused) sets the clicked tag as
        the current filter and re-runs the search — the browsing
        counterpart to chatvault.screen's tree, which instead *applies* the
        tag to the current multi-select. There's no selection to apply to
        in a read-only screen, so the same tree interaction means 'filter
        by this' here."""
        path = event.node.data
        self.state.filter_by_tag(path)
        self.refresh_results()
        self.notify(f"filtered by tag '{path}'" if path else "tag filter cleared")

    @on(OptionList.OptionSelected, "#results")
    def _on_result_selected(self, event: OptionList.OptionSelected) -> None:
        self._open_read_pane(int(event.option.id))

    # ─────────────── keybindings ───────────────

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear_filters(self) -> None:
        self.state.clear_filters()
        self.refresh_results()
        self.notify("filters cleared")

    def action_read_highlighted(self) -> None:
        results = self.query_one("#results", OptionList)
        if results.highlighted is None:
            self.notify("highlight an entry first")
            return
        item_id = int(results.get_option_at_index(results.highlighted).id)
        self._open_read_pane(item_id)

    def _open_read_pane(self, item_id: int) -> None:
        try:
            detail = self.state.view(item_id)
        except BrainError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(ReadPane(self.state, detail))


def run(conn: sqlite3.Connection) -> None:
    BrainScreenApp(conn).run()

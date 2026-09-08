from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from chatvault.brain import entries as entries_mod
from chatvault.brain import tags as tags_mod
from chatvault.brain.core import get_item_by_id, resolve_item

# docs/51_SPEC_brain_parity_and_naming.md §2.1 `screen` row + §6 phase 5:
# "Port screen_state.py's pattern: business logic unit-tested without
# Textual." This module is chatvault/screen_state.py's counterpart for the
# Brain — pure Python over a brain.sqlite connection, no Textual import
# anywhere in this file. brain_screen.py is the thin presentation layer on
# top of it.
#
# Scope is deliberately narrower than ChatVault's own screen: §8 Q8 answers
# "does brain screen need to reach the vault? No" — this never opens a
# ChatVault connection, and it never calls copy conversation/summarise
# conversation (those stay CLI-only acts). What it *does* do is the
# browsing/reading half of the Brain's command surface: list/filter, search,
# read one entry's full detail (tags, links, history, collections), and
# follow a link to another entry and back — the two axes (item_links,
# collection_items) that Brain entries have and ChatVault conversations
# don't, which is exactly the added surface a Brain screen needs over the
# vault's tagging screen.

RESULT_LIMIT = 200


# ─────────────── tag tree, for the tag-filter panel ───────────────
#
# Mirrors chatvault.tags.TagBrowseNode/tag_tree/cumulative_count (the
# structure screen.py already renders into a Tree widget) but built from the
# Brain's own flat tag list (chatvault.brain.tags.list_tags), since the
# Brain has no v_conversations-shaped view to join against.


@dataclass
class TagNode:
    name: str  # this segment only, e.g. 'macos'
    path: str  # full path, e.g. 'tech/macos'
    own: int = 0  # items tagged with this exact tag
    children: dict = field(default_factory=dict)  # name -> TagNode


def cumulative_count(node: TagNode) -> int:
    """Sum of this tag's own item count plus everything nested beneath it —
    additive, not deduplicated across siblings (an item tagged under two
    different children counts under each), matching chatvault.tags's own
    cumulative_count semantics."""
    return node.own + sum(cumulative_count(child) for child in node.children.values())


def tag_tree(conn: sqlite3.Connection) -> dict[str, TagNode]:
    """Every Brain tag, nested into a tree by its '/'-delimited segments.
    Intermediate segments with no tag row of their own still appear, as pure
    navigation nodes with own=0."""
    roots: dict[str, TagNode] = {}
    for row in tags_mod.list_tags(conn):
        segments = row["name"].split("/")
        level = roots
        node = None
        path = ""
        for i, seg in enumerate(segments):
            path = seg if i == 0 else f"{path}/{seg}"
            node = level.setdefault(seg, TagNode(name=seg, path=path))
            level = node.children
        node.own = row["item_count"]
    return roots


# ─────────────── entry detail: content + tags + links + history + collections ───────────────


@dataclass
class LinkRef:
    item_id: int
    title: str
    rel: str
    note: str | None


@dataclass
class HistoryRow:
    changed_at: str
    field: str
    old_value: str | None
    new_value: str | None
    origin: str
    note: str | None


@dataclass
class EntryDetail:
    item_id: int
    ref: str | None
    kind: str
    title: str
    why: str
    body: str | None
    status: str
    confidence: str | None
    sensitivity: str
    rank: int | None
    created_at: str
    updated_at: str | None
    use_count: int
    edit_count: int
    vetted_at: str | None
    tags: list[str]
    collections: list[str]
    links_out: list[LinkRef]
    links_in: list[LinkRef]
    history: list[HistoryRow]


def resolve_detail(conn: sqlite3.Connection, ref) -> EntryDetail:
    """Everything the read pane needs for one entry. Pure lookup, no
    mutation — unlike `show entry` (brain_cli.py's show_entry_cmd), this
    never calls touch_used: looking at something while browsing the screen
    isn't the same signal as deliberately `show`-ing it, so use_count/
    last_used_at are left alone here.

    `ref` is resolved the same way as everywhere else in the Brain (ref,
    alias, or row-number-of-the-active-list — chatvault.brain.core.resolve_item)
    *except* when it's a plain int: an int always means the item's actual
    primary key, taken as-is via get_item_by_id. This matters specifically
    for link navigation (follow_link below), which always has a real
    item_links.to_id/from_id in hand and must not have that reinterpreted as
    a row-number by resolve_item's digit-string fallback."""
    item = get_item_by_id(conn, ref) if isinstance(ref, int) else resolve_item(conn, ref)
    item_id = item["id"]

    tag_rows = conn.execute(
        "SELECT t.name FROM tags t JOIN tag_links tl ON tl.tag_id = t.id WHERE tl.item_id = ? ORDER BY t.name",
        (item_id,),
    ).fetchall()

    coll_rows = conn.execute(
        """
        SELECT c.name FROM collections c JOIN collection_items ci ON ci.collection_id = c.id
        WHERE ci.item_id = ? ORDER BY c.name
        """,
        (item_id,),
    ).fetchall()

    link_rows = conn.execute(
        """
        SELECT il.from_id, fi.title AS from_title, il.to_id, ti.title AS to_title, il.rel, il.note
        FROM item_links il
        JOIN items fi ON fi.id = il.from_id
        JOIN items ti ON ti.id = il.to_id
        WHERE il.from_id = ? OR il.to_id = ?
        ORDER BY il.created_at
        """,
        (item_id, item_id),
    ).fetchall()
    links_out = [LinkRef(r["to_id"], r["to_title"], r["rel"], r["note"]) for r in link_rows if r["from_id"] == item_id]
    links_in = [LinkRef(r["from_id"], r["from_title"], r["rel"], r["note"]) for r in link_rows if r["to_id"] == item_id]

    hist_rows = conn.execute(
        "SELECT changed_at, field, old_value, new_value, origin, note FROM history WHERE item_id = ? ORDER BY changed_at",
        (item_id,),
    ).fetchall()

    return EntryDetail(
        item_id=item_id, ref=item["ref"], kind=item["kind"], title=item["title"], why=item["why"],
        body=item["body"], status=item["status"], confidence=item["confidence"], sensitivity=item["sensitivity"],
        rank=item["rank"], created_at=item["created_at"], updated_at=item["updated_at"],
        use_count=item["use_count"], edit_count=item["edit_count"], vetted_at=item["vetted_at"],
        tags=[r["name"] for r in tag_rows],
        collections=[r["name"] for r in coll_rows],
        links_out=links_out, links_in=links_in,
        history=[
            HistoryRow(r["changed_at"], r["field"], r["old_value"], r["new_value"], r["origin"], r["note"])
            for r in hist_rows
        ],
    )


def _status_ok(entry: entries_mod.BrainEntry, status: str | None) -> bool:
    if status is None or status == "active":
        return entry.status == "active"
    if status == "archived":
        return entry.status == "archived"
    return True  # "any"


class BrainScreenState:
    """Pure-Python state and business logic for the Brain screen (§2.1
    `screen` row, §6 phase 5) — deliberately independent of Textual, so
    filtering, search, detail resolution, and link navigation are all
    unit-testable without a running TUI. brain_screen.py wires this to
    widgets; it owns no rendering.

    Read-only by design: nothing in this class writes to brain.sqlite (no
    tagging/archiving/editing from the screen — those stay CLI acts), and it
    is handed a brain.sqlite connection only. It never opens, and has no way
    to open, a ChatVault connection (§8 Q8)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.results: list[entries_mod.BrainEntry] = []
        self.total_matches = 0

        self.query: str | None = None
        self.kind: str | None = None
        self.tag: str | None = None
        self.collection: str | None = None
        self.confidence: str | None = None
        self.sensitivity: str | None = None
        self.status: str | None = None  # None/'active' (default), 'archived', 'any'

        # ─── link-navigation state (current detail + a back-stack of item ids) ───
        self.current_id: int | None = None
        self.nav_stack: list[int] = []

    # ─────────────── search / filters ───────────────

    def search(
        self,
        query: str | None = None,
        kind: str | None = None,
        tag: str | None = None,
        collection: str | None = None,
        confidence: str | None = None,
        sensitivity: str | None = None,
        status: str | None = None,
        limit: int = RESULT_LIMIT,
    ) -> list[entries_mod.BrainEntry]:
        """Re-runs the query with any given filters updated — omitted
        keyword args keep their current value, same stateful-filter-bar
        contract as chatvault.screen_state.ScreenState.search. Pass the
        literal empty string to clear a filter (mirrors ScreenState: `"" or
        None` collapses to None); status additionally accepts 'any' to show
        archived and active together."""
        if query is not None:
            self.query = query or None
        if kind is not None:
            self.kind = kind or None
        if tag is not None:
            self.tag = tag or None
        if collection is not None:
            self.collection = collection or None
        if confidence is not None:
            self.confidence = confidence or None
        if sensitivity is not None:
            self.sensitivity = sensitivity or None
        if status is not None:
            self.status = status or None

        if self.query:
            rows = entries_mod.find_entries(self.conn, self.query, kind=self.kind, tag=self.tag, limit=limit)
            entries = [
                entries_mod.BrainEntry(
                    n=i, item_id=r["id"], ref=r["ref"], kind=r["kind"], title=r["title"], why=r["why"],
                    parent_id=r["parent_id"], rank=r["rank"], confidence=r["confidence"], sensitivity=r["sensitivity"],
                    status=r["status"], source_uid=r["source_uid"], created_at=r["created_at"],
                )
                for i, r in enumerate(rows, start=1)
            ]
            entries = self._apply_local_filters(entries)
        else:
            entries = entries_mod.list_entries(
                self.conn, kind=self.kind, tag=self.tag, collection=self.collection,
                confidence=self.confidence, sensitivity=self.sensitivity,
                include_archived=(self.status in ("archived", "any")),
            )
            entries = [e for e in entries if _status_ok(e, self.status)]

        self.results = entries
        self.total_matches = len(entries)
        return entries

    def _apply_local_filters(self, entries: list[entries_mod.BrainEntry]) -> list[entries_mod.BrainEntry]:
        """find_entries (FTS) doesn't take collection/confidence/sensitivity/
        status filters itself — applied here as a local post-filter, same
        pattern as ScreenState._apply_local_filters over ChatVault's
        semantic-search results."""
        out = entries
        if self.confidence is not None:
            out = [e for e in out if e.confidence == self.confidence]
        if self.sensitivity is not None:
            out = [e for e in out if e.sensitivity == self.sensitivity]
        out = [e for e in out if _status_ok(e, self.status)]
        if self.collection is not None:
            from chatvault.brain.core import resolve_collection

            coll = resolve_collection(self.conn, self.collection)
            coll_ids = {
                r["item_id"]
                for r in self.conn.execute("SELECT item_id FROM collection_items WHERE collection_id = ?", (coll["id"],))
            }
            out = [e for e in out if e.item_id in coll_ids]
        return out

    def clear_filters(self) -> list[entries_mod.BrainEntry]:
        """Resets every filter (query included) back to its default and
        re-searches — the screen's 'start over' action."""
        self.query = self.kind = self.tag = self.collection = None
        self.confidence = self.sensitivity = self.status = None
        return self.search()

    def filter_by_tag(self, path: str | None) -> list[entries_mod.BrainEntry]:
        """Clicking (or Enter-ing) a tag in the tree sets it as the current
        tag filter and re-runs the search — the read/browse counterpart to
        ChatVault's tree, which instead *applies* the clicked tag to the
        current multi-select. There is no selection to apply to here, so
        the same tree interaction means 'filter by this' instead of
        'tag these with this'."""
        return self.search(tag=path or "")

    # ─────────────── tag tree, for the tags panel ───────────────

    def tag_tree(self) -> dict[str, TagNode]:
        return tag_tree(self.conn)

    # ─────────────── detail + link navigation ───────────────

    def view(self, ref) -> EntryDetail:
        """Opens ref's detail as a fresh top-level view (not a link hop —
        does not touch nav_stack). Used when the read pane is opened from
        the results list."""
        detail = resolve_detail(self.conn, ref)
        self.current_id = detail.item_id
        return detail

    def follow_link(self, item_id: int) -> EntryDetail:
        """Navigates from the currently-viewed entry to a linked one,
        pushing the current entry onto the back-stack first. Raises
        BrainError (via resolve_detail/resolve_item) if item_id doesn't
        exist — the back-stack is left untouched in that case."""
        detail = resolve_detail(self.conn, item_id)
        if self.current_id is not None:
            self.nav_stack.append(self.current_id)
        self.current_id = detail.item_id
        return detail

    def can_go_back(self) -> bool:
        return bool(self.nav_stack)

    def go_back(self) -> EntryDetail | None:
        """Steps back one link hop. Returns None (and leaves current_id
        alone) if there's nothing to go back to — the caller (the read
        pane's Escape handler) treats that as 'close the pane instead'."""
        if not self.nav_stack:
            return None
        item_id = self.nav_stack.pop()
        detail = resolve_detail(self.conn, item_id)
        self.current_id = detail.item_id
        return detail

    def close_detail(self) -> None:
        """Leaves the read pane entirely, back to the results list —
        clears both the current entry and the whole back-stack, so
        re-opening any entry afterwards starts a fresh navigation chain."""
        self.current_id = None
        self.nav_stack = []

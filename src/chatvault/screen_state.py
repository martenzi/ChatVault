from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from chatvault import browse, embeddings, tags as tags_mod

RECENT_LIMIT = 8


@dataclass
class BulkAction:
    """Exactly what a bulk apply/untag changed — never a guess, always the
    real set of conv_ids that were actually modified (already-tagged rows
    stay untouched by apply, already-absent rows stay untouched by untag),
    so undo() can reverse precisely that and nothing else."""
    kind: str  # 'apply' | 'untag'
    tag_name: str
    conv_ids: list[int]


class ScreenState:
    """Pure-Python state and business logic for the tagging screen (Tagging
    Workflow doc §3) — deliberately independent of Textual, so selection,
    filtering, bulk tag apply, and undo are all unit-testable without a
    running TUI. screen.py wires this to widgets; it owns no rendering."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.results: list[sqlite3.Row] = []
        self.total_matches = 0
        self.selected: set[int] = set()
        self.recently_used: list[str] = []
        self._last_action: BulkAction | None = None

        self.query: str | None = None
        self.platform: str | None = None
        self.untagged_only: bool = False
        self.since: str | None = None
        self.before: str | None = None
        self.semantic: bool = False
        self.semantic_error: str | None = None

    # ─────────────── search / filters ───────────────

    def search(
        self,
        query: str | None = None,
        platform: str | None = None,
        untagged_only: bool | None = None,
        since: str | None = None,
        before: str | None = None,
        semantic: bool | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        """Re-runs the query with any given filters updated (omitted args
        keep their current value — the filter bar is stateful, not a
        one-shot). Multi-select survives this: `selected` is untouched.

        `semantic=True` with a non-empty query ranks by meaning (embeddings)
        instead of keyword/FTS overlap — see chatvault.embeddings. Platform/
        date/untagged-only are applied as a local post-filter over the
        ranked results, since the vector index itself only knows about
        conv_id + embedding. Falls back to the normal keyword path if no
        query is set (a semantic search needs something to embed) or if
        Ollama isn't reachable (semantic_error is set so the UI can show
        why, rather than silently returning keyword results unlabeled)."""
        if query is not None:
            self.query = query or None
        if platform is not None:
            self.platform = platform or None
        if untagged_only is not None:
            self.untagged_only = untagged_only
        if since is not None:
            self.since = since or None
        if before is not None:
            self.before = before or None
        if semantic is not None:
            self.semantic = semantic

        self.semantic_error = None
        if self.semantic and self.query:
            try:
                rows = embeddings.semantic_search(self.conn, self.query, limit=limit)
            except embeddings.EmbeddingUnavailable as exc:
                self.semantic_error = str(exc)
                rows = []
            else:
                rows = self._apply_local_filters(rows)
            self.results = rows
            self.total_matches = len(rows)
            return rows

        rows, total = browse.find(
            self.conn,
            query=self.query,
            platform=self.platform,
            since=self.since,
            before=self.before,
            untagged=self.untagged_only,
            limit=limit,
            sort="recent",
        )
        self.results = rows
        self.total_matches = total
        return rows

    def _apply_local_filters(self, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        out = []
        for r in rows:
            if self.platform and r["platform"] != self.platform:
                continue
            if self.since and (r["started_at"] or "") < self.since:
                continue
            if self.before and (r["started_at"] or "") >= self.before:
                continue
            if self.untagged_only:
                tagged = self.conn.execute(
                    "SELECT 1 FROM tag_links WHERE target_type = 'conversation' AND target_id = ?",
                    (r["id"],),
                ).fetchone()
                if tagged:
                    continue
            out.append(r)
        return out

    # ─────────────── selection ───────────────

    def toggle(self, conv_id: int) -> None:
        if conv_id in self.selected:
            self.selected.discard(conv_id)
        else:
            self.selected.add(conv_id)

    def select_all(self) -> None:
        self.selected = {r["id"] for r in self.results}

    def select_none(self) -> None:
        self.selected = set()

    # ─────────────── recently-used tags ───────────────

    def _touch_recent(self, tag_name: str) -> None:
        if tag_name in self.recently_used:
            self.recently_used.remove(tag_name)
        self.recently_used.insert(0, tag_name)
        del self.recently_used[RECENT_LIMIT:]

    # ─────────────── bulk tag / untag on the current selection ───────────────

    def apply_tag(self, tag_name: str) -> int:
        """Tags every currently-selected conversation with `tag_name`,
        source='manual'. Rows already carrying the tag are left alone (and
        don't count toward the undo set — undo must never remove a tag that
        predates this action)."""
        if not self.selected:
            return 0
        tag_id = tags_mod.get_or_create_tag(self.conn, tag_name)
        now = tags_mod.now()
        changed = []
        for conv_id in self.selected:
            cur = self.conn.execute(
                """
                INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
                VALUES (?, 'conversation', ?, 'manual', ?)
                ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
                """,
                (tag_id, conv_id, now),
            )
            if cur.rowcount:
                changed.append(conv_id)
        self.conn.commit()
        # recency tracks what you clicked, not just what changed — re-
        # applying an already-applied tag is still a real interaction and
        # should resurface it at the front of "recently used"
        self._touch_recent(tag_name)
        if changed:
            self._last_action = BulkAction("apply", tag_name, changed)
        return len(changed)

    def untag_selected(self, tag_name: str) -> int:
        """Removes `tag_name` from every currently-selected conversation that
        actually carries it. Rows that never had it are left alone."""
        if not self.selected:
            return 0
        row = self.conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
        if row is None:
            return 0
        tag_id = row["id"]
        changed = []
        for conv_id in self.selected:
            cur = self.conn.execute(
                "DELETE FROM tag_links WHERE tag_id = ? AND target_type = 'conversation' AND target_id = ?",
                (tag_id, conv_id),
            )
            if cur.rowcount:
                changed.append(conv_id)
        self.conn.commit()
        if changed:
            self._last_action = BulkAction("untag", tag_name, changed)
        return len(changed)

    # ─────────────── undo — reverses exactly the last action, once ───────────────

    def can_undo(self) -> bool:
        return self._last_action is not None

    def undo(self) -> int:
        """Reverses the single most recent apply/untag. Only one level deep —
        matches the doc's own wording ('reverses the last tagging action'),
        not a full history stack."""
        action = self._last_action
        if action is None:
            return 0
        self._last_action = None

        if action.kind == "apply":
            tag_id = tags_mod.get_or_create_tag(self.conn, action.tag_name)
            for conv_id in action.conv_ids:
                self.conn.execute(
                    "DELETE FROM tag_links WHERE tag_id = ? AND target_type = 'conversation' AND target_id = ?",
                    (tag_id, conv_id),
                )
        else:
            tag_id = tags_mod.get_or_create_tag(self.conn, action.tag_name)
            now = tags_mod.now()
            for conv_id in action.conv_ids:
                self.conn.execute(
                    """
                    INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
                    VALUES (?, 'conversation', ?, 'manual', ?)
                    ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
                    """,
                    (tag_id, conv_id, now),
                )
        self.conn.commit()
        return len(action.conv_ids)

    # ─────────────── tag tree, for the tags pane ───────────────

    def tag_tree(self) -> dict:
        return tags_mod.build_tag_tree(self.conn)

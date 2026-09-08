"""Second Brain, Layer 2+ (docs/45_SPEC_brain_v2_full.md — supersedes
docs/42_SPEC_brain_db.md §3/§4). Split into one module per noun so each
stays close in size to the rest of this codebase's per-concern modules
(chatvault/tags.py, chatvault/stash.py); this __init__ re-exports the full
surface so `from chatvault import brain as brain_mod` keeps working exactly
as it did when this was one file."""

from __future__ import annotations

from chatvault.brain.aliases import add_alias, list_aliases, remove_alias
from chatvault.brain import attachments as attachments_mod
from chatvault.brain import curation_io as curation_io_mod
from chatvault.brain import dedupe as dedupe_mod
from chatvault.brain import graph as graph_mod
from chatvault.brain import tag_rules as tag_rules_mod
from chatvault.brain import views as views_mod
from chatvault.brain.collections import (
    add_collection,
    add_to_collection,
    list_collections,
    remove_from_collection,
    reorder_collection,
    show_collection,
)
from chatvault.brain.core import BrainError, get_item_by_id, known_kinds, known_rels, now, resolve_collection, resolve_item, resolve_ref_or_id
from chatvault.brain.distill import DEFAULT_MODEL as DISTILL_DEFAULT_MODEL
from chatvault.brain.distill import OLLAMA_GENERATE_URL as DISTILL_OLLAMA_URL
from chatvault.brain.distill import DistillUnavailable, distill, distill_entry, resolve_conversation
from chatvault.brain.doctor import CheckResult, run_all as doctor_run_all
from chatvault.brain.embeddings import IndexResult, index_entries, semantic_find
from chatvault.brain.embeddings import status as embed_status
from chatvault.embeddings import EmbeddingUnavailable
from chatvault.brain.entries import (
    BrainEntry,
    PROMOTED_TAG,
    add_entry,
    archive_entry,
    edit_entry,
    exclude_entry,
    find_entries,
    get_item_row,
    list_entries,
    pick,
    promote,
    promote_from_stash,
    purge_entry,
    redact_entry,
    remove_entry,
    restore_entry,
    touch_used,
    tree_entry,
    variants,
)
from chatvault.brain.imports import import_csv, import_markdown, import_yaml
from chatvault.brain.inbox import accept, jot, jot_file, list_inbox, reject
from chatvault.brain.kinds import add_kind, list_kinds, remove_kind
from chatvault.brain import ledger as ledger_mod
from chatvault.brain.links import add_rel, link_entry, list_links, unlink_entry
from chatvault.brain.notes import add_note, list_notes
from chatvault.brain.review import done, due, orphans, review_entry, stale
from chatvault.brain.stats import EXPORT_EXTENSIONS, export_items, get_items_for_tree, render_rows, stats, stats_by, stats_edits, stats_usage, verify
from chatvault.brain.tags import add_tag, list_tags, merge_tags, rename_tag, tag_entry, tree_tags, untag_entry

__all__ = [
    "BrainError", "get_item_by_id", "known_kinds", "known_rels", "now", "resolve_collection", "resolve_item", "resolve_ref_or_id",
    "CheckResult", "doctor_run_all",
    "IndexResult", "index_entries", "semantic_find", "embed_status", "EmbeddingUnavailable",
    "distill", "distill_entry", "DistillUnavailable", "resolve_conversation", "DISTILL_DEFAULT_MODEL", "DISTILL_OLLAMA_URL",
    "jot", "jot_file", "list_inbox", "accept", "reject",
    "BrainEntry", "PROMOTED_TAG", "add_entry", "archive_entry", "edit_entry", "exclude_entry", "find_entries",
    "get_item_row", "list_entries", "pick", "promote", "promote_from_stash", "purge_entry", "redact_entry", "remove_entry",
    "restore_entry", "touch_used", "tree_entry", "variants",
    "add_alias", "list_aliases", "remove_alias",
    "add_collection", "add_to_collection", "list_collections", "remove_from_collection",
    "reorder_collection", "show_collection",
    "import_csv", "import_markdown", "import_yaml",
    "add_kind", "list_kinds", "remove_kind",
    "ledger_mod",
    "add_rel", "link_entry", "list_links", "unlink_entry",
    "add_note", "list_notes",
    "done", "due", "orphans", "review_entry", "stale",
    "EXPORT_EXTENSIONS", "export_items", "get_items_for_tree", "render_rows", "stats", "stats_by", "stats_edits", "stats_usage", "verify",
    "add_tag", "list_tags", "merge_tags", "rename_tag", "tag_entry", "tree_tags", "untag_entry",
    "attachments_mod", "curation_io_mod", "dedupe_mod", "graph_mod", "tag_rules_mod", "views_mod",
]

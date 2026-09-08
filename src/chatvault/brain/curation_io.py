from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# `brain curation export/import/diff` (docs/51_SPEC_brain_parity_and_naming.md
# §2.1 `curation export/import/diff` row), ported from ChatVault's own
# chatvault/curation_io.py — same shape (a JSON snapshot, an additive
# import with skip/overwrite conflict handling, a diff that's just a
# dry-run import), adapted to Brain's schema.
#
# Target addressing is the one place this genuinely differs from
# ChatVault's version, and is called out explicitly rather than silently
# assumed to work the same way: ChatVault addresses everything by a
# content-derived `uid`/`content_hash`, which is stable across a full
# wipe-and-reimport. Brain items have no equivalent stable content key by
# default — `ref` is optional and freely user-assigned. So each linkage
# below is exported with *both* the item's `ref` (if it has one) and its
# numeric id; import resolves by `ref` first (works even if ids were
# reassigned, e.g. after a rebuild), falling back to the id (works for a
# same-database restore where ids are untouched). An item with neither
# match is reported unresolved, never silently dropped.

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ImportSummary:
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    unresolved: list[dict] = field(default_factory=list)


def _resolve_item_id(conn: sqlite3.Connection, item_ref: str | None, item_id: int | None) -> int | None:
    if item_ref:
        row = conn.execute("SELECT id FROM items WHERE ref = ?", (item_ref,)).fetchone()
        if row is not None:
            return row["id"]
    if item_id is not None:
        row = conn.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is not None:
            return row["id"]
    return None


def _item_target(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT id, ref FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    return {"item_ref": row["ref"], "item_id": row["id"]}


# ─────────────── export ───────────────


def export_curation(conn: sqlite3.Connection) -> dict:
    tags = [dict(r) for r in conn.execute("SELECT name FROM tags ORDER BY name").fetchall()]

    tag_links = []
    for r in conn.execute(
        "SELECT tl.tag_id, t.name AS tag_name, tl.item_id, tl.source FROM tag_links tl JOIN tags t ON t.id = tl.tag_id"
    ).fetchall():
        target = _item_target(conn, r["item_id"])
        if target is None:
            continue
        tag_links.append({"tag": r["tag_name"], "target": target, "source": r["source"]})

    item_links = []
    for r in conn.execute("SELECT from_id, to_id, rel, note, created_at FROM item_links").fetchall():
        from_t = _item_target(conn, r["from_id"])
        to_t = _item_target(conn, r["to_id"])
        if from_t is None or to_t is None:
            continue
        item_links.append({"from": from_t, "to": to_t, "rel": r["rel"], "note": r["note"], "created_at": r["created_at"]})

    collections = [dict(r) for r in conn.execute("SELECT ref, name, why FROM collections ORDER BY name").fetchall()]

    collection_items = []
    for r in conn.execute(
        "SELECT ci.collection_id, c.ref AS coll_ref, c.name AS coll_name, ci.item_id, ci.position, ci.note "
        "FROM collection_items ci JOIN collections c ON c.id = ci.collection_id"
    ).fetchall():
        target = _item_target(conn, r["item_id"])
        if target is None:
            continue
        collection_items.append(
            {
                "collection": r["coll_ref"] or r["coll_name"],
                "target": target,
                "position": r["position"],
                "note": r["note"],
            }
        )

    notes = []
    for r in conn.execute("SELECT item_id, text, created_at FROM notes").fetchall():
        target = _item_target(conn, r["item_id"])
        if target is None:
            continue
        notes.append({"target": target, "text": r["text"], "created_at": r["created_at"]})

    exclusions = []
    for r in conn.execute("SELECT id FROM items WHERE excluded = 1").fetchall():
        target = _item_target(conn, r["id"])
        if target is not None:
            exclusions.append({"target": target})

    views = [dict(r) for r in conn.execute("SELECT name, query, description FROM views ORDER BY name").fetchall()]

    return {
        "schema": SCHEMA_VERSION,
        "exported_at": _now(),
        "counts": {
            "tags": len(tags), "tag_links": len(tag_links), "item_links": len(item_links),
            "collections": len(collections), "collection_items": len(collection_items),
            "notes": len(notes), "exclusions": len(exclusions), "views": len(views),
        },
        "tags": tags,
        "tag_links": tag_links,
        "item_links": item_links,
        "collections": collections,
        "collection_items": collection_items,
        "notes": notes,
        "exclusions": exclusions,
        "views": views,
    }


# ─────────────── import ───────────────


def _import_tags(conn, entries, dry_run, counts, unresolved):
    added = unchanged = 0
    for e in entries:
        existing = conn.execute("SELECT id FROM tags WHERE name = ?", (e["name"],)).fetchone()
        if existing is None:
            if not dry_run:
                conn.execute("INSERT INTO tags (name) VALUES (?)", (e["name"],))
            added += 1
        else:
            unchanged += 1
    counts["tags"] = {"added": added, "updated": 0, "unchanged": unchanged}


def _import_tag_links(conn, entries, on_conflict, dry_run, counts, unresolved):
    added = updated = unchanged = 0
    for e in entries:
        tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (e["tag"],)).fetchone()
        if tag_row is None:
            unresolved.append({"entity": "tag_links", "target": e["target"], "reason": "missing_tag"})
            continue
        item_id = _resolve_item_id(conn, e["target"].get("item_ref"), e["target"].get("item_id"))
        if item_id is None:
            unresolved.append({"entity": "tag_links", "target": e["target"], "reason": "missing_target"})
            continue
        existing = conn.execute(
            "SELECT source FROM tag_links WHERE tag_id = ? AND item_id = ?", (tag_row["id"], item_id)
        ).fetchone()
        if existing is None:
            if not dry_run:
                conn.execute(
                    "INSERT INTO tag_links (tag_id, item_id, source) VALUES (?, ?, ?)",
                    (tag_row["id"], item_id, e.get("source")),
                )
            added += 1
        elif on_conflict == "overwrite":
            if not dry_run:
                conn.execute(
                    "UPDATE tag_links SET source = ? WHERE tag_id = ? AND item_id = ?",
                    (e.get("source"), tag_row["id"], item_id),
                )
            updated += 1
        else:
            unchanged += 1
    counts["tag_links"] = {"added": added, "updated": updated, "unchanged": unchanged}


def _import_item_links(conn, entries, dry_run, counts, unresolved):
    added = unchanged = 0
    for e in entries:
        from_id = _resolve_item_id(conn, e["from"].get("item_ref"), e["from"].get("item_id"))
        to_id = _resolve_item_id(conn, e["to"].get("item_ref"), e["to"].get("item_id"))
        if from_id is None or to_id is None:
            unresolved.append({"entity": "item_links", "target": e, "reason": "missing_target"})
            continue
        existing = conn.execute(
            "SELECT 1 FROM item_links WHERE from_id = ? AND to_id = ? AND rel = ?", (from_id, to_id, e["rel"])
        ).fetchone()
        if existing is None:
            if not dry_run:
                conn.execute(
                    "INSERT INTO item_links (from_id, to_id, rel, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (from_id, to_id, e["rel"], e.get("note"), e.get("created_at") or _now()),
                )
            added += 1
        else:
            unchanged += 1
    counts["item_links"] = {"added": added, "updated": 0, "unchanged": unchanged}


def _import_collections(conn, entries, dry_run, counts, unresolved):
    added = unchanged = 0
    for e in entries:
        existing = conn.execute("SELECT id FROM collections WHERE name = ?", (e["name"],)).fetchone()
        if existing is None:
            if not dry_run:
                conn.execute(
                    "INSERT INTO collections (ref, name, why, created_at, status) VALUES (?, ?, ?, ?, 'active')",
                    (e.get("ref"), e["name"], e.get("why") or "(imported)", _now()),
                )
            added += 1
        else:
            unchanged += 1
    counts["collections"] = {"added": added, "updated": 0, "unchanged": unchanged}


def _import_collection_items(conn, entries, dry_run, counts, unresolved):
    added = unchanged = 0
    for e in entries:
        coll_row = conn.execute(
            "SELECT id FROM collections WHERE ref = ? OR name = ?", (e["collection"], e["collection"])
        ).fetchone()
        if coll_row is None:
            unresolved.append({"entity": "collection_items", "target": e, "reason": "missing_collection"})
            continue
        item_id = _resolve_item_id(conn, e["target"].get("item_ref"), e["target"].get("item_id"))
        if item_id is None:
            unresolved.append({"entity": "collection_items", "target": e["target"], "reason": "missing_target"})
            continue
        existing = conn.execute(
            "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?", (coll_row["id"], item_id)
        ).fetchone()
        if existing is None:
            if not dry_run:
                conn.execute(
                    "INSERT INTO collection_items (collection_id, item_id, position, note) VALUES (?, ?, ?, ?)",
                    (coll_row["id"], item_id, e.get("position") or 0, e.get("note")),
                )
            added += 1
        else:
            unchanged += 1
    counts["collection_items"] = {"added": added, "updated": 0, "unchanged": unchanged}


def _import_notes(conn, entries, dry_run, counts, unresolved):
    """Always additive-with-dedupe, same as ChatVault's annotations import —
    a running commentary log should never silently overwrite an earlier
    remark."""
    added = unchanged = 0
    for e in entries:
        item_id = _resolve_item_id(conn, e["target"].get("item_ref"), e["target"].get("item_id"))
        if item_id is None:
            unresolved.append({"entity": "notes", "target": e["target"], "reason": "missing_target"})
            continue
        exists = conn.execute(
            "SELECT 1 FROM notes WHERE item_id = ? AND text = ?", (item_id, e["text"])
        ).fetchone()
        if exists:
            unchanged += 1
            continue
        if not dry_run:
            conn.execute(
                "INSERT INTO notes (item_id, text, created_at) VALUES (?, ?, ?)",
                (item_id, e["text"], e.get("created_at") or _now()),
            )
        added += 1
    counts["notes"] = {"added": added, "updated": 0, "unchanged": unchanged}


def _import_exclusions(conn, entries, dry_run, counts, unresolved):
    added = unchanged = 0
    for e in entries:
        item_id = _resolve_item_id(conn, e["target"].get("item_ref"), e["target"].get("item_id"))
        if item_id is None:
            unresolved.append({"entity": "exclusions", "target": e["target"], "reason": "missing_target"})
            continue
        row = conn.execute("SELECT excluded FROM items WHERE id = ?", (item_id,)).fetchone()
        if row["excluded"] == 0:
            if not dry_run:
                conn.execute("UPDATE items SET excluded = 1 WHERE id = ?", (item_id,))
            added += 1
        else:
            unchanged += 1
    counts["exclusions"] = {"added": added, "updated": 0, "unchanged": unchanged}


def _import_views(conn, entries, on_conflict, dry_run, counts, unresolved):
    added = updated = unchanged = 0
    for e in entries:
        existing = conn.execute("SELECT id FROM views WHERE name = ?", (e["name"],)).fetchone()
        if existing is None:
            if not dry_run:
                conn.execute(
                    "INSERT INTO views (name, query, description, created_at) VALUES (?, ?, ?, ?)",
                    (e["name"], e["query"], e.get("description"), _now()),
                )
            added += 1
        elif on_conflict == "overwrite":
            if not dry_run:
                conn.execute(
                    "UPDATE views SET query = ?, description = ? WHERE name = ?",
                    (e["query"], e.get("description"), e["name"]),
                )
            updated += 1
        else:
            unchanged += 1
    counts["views"] = {"added": added, "updated": updated, "unchanged": unchanged}


def import_curation(conn: sqlite3.Connection, data: dict, on_conflict: str = "skip", dry_run: bool = False) -> ImportSummary:
    if on_conflict not in ("skip", "overwrite"):
        raise ValueError(f"on_conflict must be 'skip' or 'overwrite', got {on_conflict!r}")

    counts: dict[str, dict[str, int]] = {}
    unresolved: list[dict] = []

    _import_tags(conn, data.get("tags", []), dry_run, counts, unresolved)
    _import_collections(conn, data.get("collections", []), dry_run, counts, unresolved)
    _import_views(conn, data.get("views", []), on_conflict, dry_run, counts, unresolved)
    _import_tag_links(conn, data.get("tag_links", []), on_conflict, dry_run, counts, unresolved)
    _import_item_links(conn, data.get("item_links", []), dry_run, counts, unresolved)
    _import_collection_items(conn, data.get("collection_items", []), dry_run, counts, unresolved)
    _import_notes(conn, data.get("notes", []), dry_run, counts, unresolved)
    _import_exclusions(conn, data.get("exclusions", []), dry_run, counts, unresolved)

    if not dry_run:
        conn.commit()

    return ImportSummary(counts=counts, unresolved=unresolved)


def diff_curation(conn: sqlite3.Connection, data: dict) -> ImportSummary:
    """A diff is just a dry-run import: whatever would be `added` or
    `updated` is what's different between `data` and the current DB state.
    Same trick ChatVault's own diff_curation uses."""
    return import_curation(conn, data, dry_run=True)


def write_unresolved(out_file: Path, unresolved: list[dict]) -> Path:
    path = out_file.with_suffix(out_file.suffix + ".unresolved.json")
    path.write_text(json.dumps(unresolved, indent=2))
    return path

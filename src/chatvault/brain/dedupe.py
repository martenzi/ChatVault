from __future__ import annotations

import sqlite3

from chatvault.brain.core import BrainError, now, resolve_item, sha256_text

# `brain dedupe` (docs/51_SPEC_brain_parity_and_naming.md §2.1 `dedupe` row,
# ported from chatvault/dedupe.py's content-hash approach). Brain has no
# `blocks`/`messages` table to compare message-by-message the way
# ChatVault's `_prefix_containment` does, so this adapts the same idea
# ("compare stable content fingerprints, cheapest first") to `items`:
#
#   level 1 — exact body match: same body_sha256 (or a hash computed
#             on the fly for items with no stored hash), highest confidence.
#   level 2 — same title (case-insensitive) + same kind: likely the same
#             thing captured twice under slightly different bodies.
#   level 3 — same source_uid: two Brain items both pointing at the same
#             vault conversation — should be prevented by `copy
#             conversation`'s own existing-item guard, but a hand-added
#             entry or an older bug could still produce one.
#
# `brain dedupe` with no flags computes and prints candidates only —
# nothing is written to the database. D7/§8: never auto-merge; only
# `--confirm <a> <b>` acts, and only on that one explicit pair.


def _body_hash(row: sqlite3.Row) -> str | None:
    if row["body_sha256"]:
        return row["body_sha256"]
    return sha256_text(row["body"]) if row["body"] else None


def scan_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Read-only. Returns candidate duplicate pairs found across active,
    non-excluded items — never writes anything (there is no persistent
    dedupe_candidates table in the Brain schema; candidates are recomputed
    fresh on every call, which is cheap at Brain's scale and means a
    candidate can never go stale)."""
    rows = conn.execute("SELECT * FROM items WHERE status = 'active' AND excluded = 0").fetchall()
    candidates: list[dict] = []
    seen_pairs: set[tuple[int, int]] = set()

    def _add(a: sqlite3.Row, b: sqlite3.Row, reason: str, confidence: float) -> None:
        lo, hi = sorted((a["id"], b["id"]))
        if (lo, hi) in seen_pairs:
            return
        seen_pairs.add((lo, hi))
        candidates.append(
            {
                "item_id_a": lo,
                "item_id_b": hi,
                "title_a": a["title"] if a["id"] == lo else b["title"],
                "title_b": b["title"] if a["id"] == lo else a["title"],
                "reason": reason,
                "confidence": confidence,
            }
        )

    # level 1 — exact body hash match
    by_hash: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        h = _body_hash(r)
        if h:
            by_hash.setdefault(h, []).append(r)
    for group in by_hash.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                _add(group[i], group[j], "exact_body_hash", 1.0)

    # level 2 — same title (case-insensitive) + same kind
    by_title_kind: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        key = ((r["title"] or "").strip().lower(), r["kind"])
        by_title_kind.setdefault(key, []).append(r)
    for group in by_title_kind.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                _add(group[i], group[j], "same_title_and_kind", 0.6)

    # level 3 — same source_uid (should not happen via `copy conversation`'s
    # own guard, but not structurally impossible)
    by_uid: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if r["source_uid"]:
            by_uid.setdefault(r["source_uid"], []).append(r)
    for group in by_uid.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                _add(group[i], group[j], "same_source_uid", 0.9)

    candidates.sort(key=lambda c: -c["confidence"])
    return candidates


def merge_pair(conn: sqlite3.Connection, ref_a, ref_b, confirm: bool) -> dict:
    """The only thing `brain dedupe` is ever allowed to change, and only
    with --confirm on an explicit, named pair (D7). Never a hard delete:
    the lower-id item is kept as canonical, the other item's tags,
    outgoing/incoming links, and collection memberships are copied onto the
    canonical item, then the duplicate is archived (not removed) — fully
    reversible with `restore entry`, unlike a real merge would be."""
    if not confirm:
        raise BrainError("refusing to merge duplicates without --confirm")

    item_a = resolve_item(conn, ref_a)
    item_b = resolve_item(conn, ref_b)
    if item_a["id"] == item_b["id"]:
        raise BrainError("both refs resolve to the same item")

    keep, drop = (item_a, item_b) if item_a["id"] < item_b["id"] else (item_b, item_a)

    # tags
    for row in conn.execute("SELECT tag_id, source FROM tag_links WHERE item_id = ?", (drop["id"],)).fetchall():
        conn.execute(
            "INSERT INTO tag_links (tag_id, item_id, source) VALUES (?, ?, ?) ON CONFLICT(tag_id, item_id) DO NOTHING",
            (row["tag_id"], keep["id"], row["source"]),
        )
    # links (both directions)
    for row in conn.execute("SELECT to_id, rel, note FROM item_links WHERE from_id = ?", (drop["id"],)).fetchall():
        if row["to_id"] != keep["id"]:
            conn.execute(
                "INSERT INTO item_links (from_id, to_id, rel, note, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (keep["id"], row["to_id"], row["rel"], row["note"], now()),
            )
    for row in conn.execute("SELECT from_id, rel, note FROM item_links WHERE to_id = ?", (drop["id"],)).fetchall():
        if row["from_id"] != keep["id"]:
            conn.execute(
                "INSERT INTO item_links (from_id, to_id, rel, note, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (row["from_id"], keep["id"], row["rel"], row["note"], now()),
            )
    # collection memberships
    for row in conn.execute("SELECT collection_id, note FROM collection_items WHERE item_id = ?", (drop["id"],)).fetchall():
        conn.execute(
            "INSERT INTO collection_items (collection_id, item_id, position, note) "
            "SELECT ?, ?, COALESCE(MAX(position), 0) + 1, ? FROM collection_items WHERE collection_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?)",
            (row["collection_id"], keep["id"], row["note"], row["collection_id"], row["collection_id"], keep["id"]),
        )

    conn.execute("UPDATE items SET status = 'archived', updated_at = ? WHERE id = ?", (now(), drop["id"]))
    conn.commit()
    return {"kept": keep["id"], "archived": drop["id"], "kept_title": keep["title"], "archived_title": drop["title"]}

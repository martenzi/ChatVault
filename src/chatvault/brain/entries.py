from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from chatvault import export as export_mod
from chatvault import stash as stash_mod
from chatvault import tags as tags_mod
from chatvault.brain.core import BrainError, get_item_by_id, known_kinds, now, record_history, resolve_item, resolve_ref_or_id, sha256_text

PROMOTED_TAG = "stash/promoted"

# stash's kind vocabulary (method/decision/reference/thread) isn't the
# brain's — method/decision line up directly, reference/thread don't exist
# on the brain side, so promotion maps them onto the closest kind rather
# than refusing every stashed item that used them.
_STASH_KIND_MAP = {"reference": "fact", "thread": "conversation"}

# docs/51_SPEC_brain_parity_and_naming.md §4.3 — vault tags that describe
# *ingestion/curation provenance* rather than the user's own knowledge
# taxonomy. Carried into the Brain, but namespaced under `vault/` so they
# sort together, are trivially removable, and can never collide with a tag
# the user creates by hand in the Brain. Every other vault tag carries
# across unchanged — the whole point of a shared taxonomy across both
# stores.
_CURATION_TAG_PREFIXES = ("review/", "agent-app:", "chatgpt-account:", "stash/")


def _get_or_create_tag(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT INTO tags (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    return conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]


def _apply_tags(conn: sqlite3.Connection, item_id: int, tag_names: list[str]) -> None:
    for name in tag_names:
        tag_id = _get_or_create_tag(conn, name)
        conn.execute("INSERT INTO tag_links (tag_id, item_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (tag_id, item_id))


def _apply_aliases(conn: sqlite3.Connection, item_id: int, aliases: list[str]) -> None:
    for alias in aliases:
        conn.execute("INSERT INTO aliases (item_id, alias) VALUES (?, ?) ON CONFLICT(item_id, alias) DO NOTHING", (item_id, alias))


def _merge_meta(meta_json: str | None, set_pairs: list[str]) -> str | None:
    """--meta '{"length":"short"}' and repeated --set k=v both feed the same
    JSON column; --set is applied after --meta (and after any existing
    value, on edit) so it can override or add individual keys without
    requiring the whole object to be retyped."""
    data = json.loads(meta_json) if meta_json else {}
    for pair in set_pairs or []:
        if "=" not in pair:
            raise BrainError(f"--set expects key=value, got {pair!r}")
        k, _, v = pair.partition("=")
        data[k.strip()] = v.strip()
    if not data:
        return None
    return json.dumps(data)


def add_entry(
    conn: sqlite3.Connection,
    title: str,
    why: str,
    kind: str = "note",
    ref: str | None = None,
    parent: str | None = None,
    rank: int | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    meta_json: str | None = None,
    set_pairs: list[str] | None = None,
    collection: str | None = None,
    confidence: str | None = None,
    sensitivity: str = "normal",
    url: str | None = None,
    vetted: bool = False,
    source_path: str | None = None,
) -> dict:
    if why is None or not why.strip():
        raise BrainError("--why is required and must not be empty")
    if title is None or not title.strip():
        raise BrainError("title is required and must not be empty")
    if kind not in known_kinds(conn):
        raise BrainError(f"kind must be one of {sorted(known_kinds(conn))}, got {kind!r} — see `brain add kind`")
    if sensitivity not in {"normal", "personal", "secret"}:
        raise BrainError("--sensitivity must be one of normal, personal, secret")
    if ref is not None:
        existing = conn.execute("SELECT id FROM items WHERE ref = ?", (ref,)).fetchone()
        if existing is not None:
            raise BrainError(f"ref {ref!r} is already used by item {existing['id']}")

    parent_id = None
    if parent is not None:
        parent_id = resolve_ref_or_id(conn, parent)["id"]

    meta = _merge_meta(meta_json, set_pairs or [])
    ts = now()
    body_hash = sha256_text(body) if source_path else None

    cur = conn.execute(
        """
        INSERT INTO items (
            kind, title, why, body, url, source_kind, source_path, status,
            confidence, created_at, ref, parent_id, rank, meta, sensitivity,
            vetted_at, body_sha256
        ) VALUES (?, ?, ?, ?, ?, 'manual', ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kind, title.strip(), why.strip(), body, url, source_path,
            confidence, ts, ref, parent_id, rank, meta, sensitivity,
            ts if vetted else None, body_hash,
        ),
    )
    item_id = cur.lastrowid

    _apply_tags(conn, item_id, tags or [])
    _apply_aliases(conn, item_id, aliases or [])
    if collection is not None:
        from chatvault.brain.collections import add_to_collection

        add_to_collection(conn, collection, [ref or str(item_id)])

    conn.commit()

    # §2.1 `tag rule` row: auto-apply matching tag_rules on every new entry.
    from chatvault.brain.tag_rules import apply_rules_to_item

    apply_rules_to_item(conn, item_id)

    return {"item_id": item_id, "title": title.strip()}


_EDITABLE_FIELDS = {"title", "why", "rank", "kind"}


def edit_entry(
    conn: sqlite3.Connection,
    ref,
    title: str | None = None,
    why: str | None = None,
    body: str | None = None,
    rank: int | None = None,
    kind: str | None = None,
    set_pairs: list[str] | None = None,
    origin: str = "cli",
) -> dict:
    """§3: `brain edit entry <ref|n> [--title|--why|--body-editor|--rank|--set k=v|--kind]`.
    Every field actually changed writes exactly one history row (old_value,
    new_value) — the history_bumps_edit_count trigger takes care of
    edit_count and updated_at, so this function never touches either
    directly (acceptance criterion: three edits -> three history rows and
    edit_count=3, which only holds if nothing double-counts)."""
    item = resolve_item(conn, ref)
    item_id = item["id"]
    changed = []

    if title is not None and title.strip() != item["title"]:
        record_history(conn, item_id, "title", item["title"], title.strip(), origin=origin)
        conn.execute("UPDATE items SET title = ? WHERE id = ?", (title.strip(), item_id))
        changed.append("title")

    if why is not None and why.strip() != item["why"]:
        record_history(conn, item_id, "why", item["why"], why.strip(), origin=origin)
        conn.execute("UPDATE items SET why = ? WHERE id = ?", (why.strip(), item_id))
        changed.append("why")

    if body is not None and body != (item["body"] or ""):
        record_history(conn, item_id, "body", item["body"], body, origin=origin)
        new_hash = sha256_text(body) if item["source_path"] else None
        conn.execute("UPDATE items SET body = ?, body_sha256 = ? WHERE id = ?", (body, new_hash, item_id))
        changed.append("body")

    if rank is not None and rank != item["rank"]:
        record_history(conn, item_id, "rank", item["rank"], rank, origin=origin)
        conn.execute("UPDATE items SET rank = ? WHERE id = ?", (rank, item_id))
        changed.append("rank")

    if kind is not None and kind != item["kind"]:
        if kind not in known_kinds(conn):
            raise BrainError(f"kind must be one of {sorted(known_kinds(conn))}, got {kind!r}")
        record_history(conn, item_id, "kind", item["kind"], kind, origin=origin)
        conn.execute("UPDATE items SET kind = ? WHERE id = ?", (kind, item_id))
        changed.append("kind")

    if set_pairs:
        meta = json.loads(item["meta"]) if item["meta"] else {}
        for pair in set_pairs:
            if "=" not in pair:
                raise BrainError(f"--set expects key=value, got {pair!r}")
            k, _, v = pair.partition("=")
            k = k.strip()
            old = meta.get(k)
            new = v.strip()
            if old != new:
                record_history(conn, item_id, f"meta.{k}", old, new, origin=origin)
                meta[k] = new
                changed.append(f"meta.{k}")
        conn.execute("UPDATE items SET meta = ? WHERE id = ?", (json.dumps(meta), item_id))

    conn.commit()
    return {"item_id": item_id, "changed": changed}


def touch_used(conn: sqlite3.Connection, item_id: int) -> None:
    """Every `show entry` marks the item used — this is what makes
    `brain stale` (use_count = 0) and `brain stats usage` meaningful."""
    conn.execute("UPDATE items SET use_count = use_count + 1, last_used_at = ? WHERE id = ?", (now(), item_id))
    conn.commit()


def get_item_row(conn: sqlite3.Connection, ref) -> sqlite3.Row:
    """CLI-facing: resolves ref/alias/row-number, same as every other
    <ref>-taking command. For a direct id lookup use core.get_item_by_id."""
    return resolve_item(conn, ref)


@dataclass
class BrainEntry:
    n: int
    item_id: int
    ref: str | None
    kind: str
    title: str
    why: str
    parent_id: int | None
    rank: int | None
    confidence: str | None
    sensitivity: str
    status: str
    source_uid: str | None
    created_at: str


def _tag_matches(name: str, query: str) -> bool:
    for suffix in ("/**", "/*"):
        if query.endswith(suffix):
            prefix = query[: -len(suffix)]
            return name == prefix or name.startswith(prefix + "/")
    return name == query


def list_entries(
    conn: sqlite3.Connection,
    kind: str | None = None,
    tag: str | None = None,
    parent: str | None = None,
    collection: str | None = None,
    length: str | None = None,
    lang: str | None = None,
    confidence: str | None = None,
    sensitivity: str | None = None,
    rank: int | None = None,
    include_archived: bool = False,
) -> list[BrainEntry]:
    # `excluded` (migrations/brain/005_brain_parity.sql, §2.1 `exclude`/
    # `restore` row) hides an item from this list even under
    # --include-archived — exclude/archive are independent axes (§8 Q6),
    # both filter here regardless of which one(s) are in play.
    status_clause = "WHERE excluded = 0" if include_archived else "WHERE status = 'active' AND excluded = 0"
    rows = conn.execute(f"SELECT * FROM items {status_clause} ORDER BY created_at DESC, id DESC").fetchall()
    entries = [
        BrainEntry(
            n=i, item_id=r["id"], ref=r["ref"], kind=r["kind"], title=r["title"], why=r["why"],
            parent_id=r["parent_id"], rank=r["rank"], confidence=r["confidence"], sensitivity=r["sensitivity"],
            status=r["status"], source_uid=r["source_uid"], created_at=r["created_at"],
        )
        for i, r in enumerate(rows, start=1)
    ]

    if kind is not None:
        entries = [e for e in entries if e.kind == kind]
    if confidence is not None:
        entries = [e for e in entries if e.confidence == confidence]
    if sensitivity is not None:
        entries = [e for e in entries if e.sensitivity == sensitivity]
    if rank is not None:
        entries = [e for e in entries if e.rank == rank]
    if parent is not None:
        parent_id = resolve_item(conn, parent)["id"]
        entries = [e for e in entries if e.parent_id == parent_id]
    if tag is not None:
        tag_rows = conn.execute("SELECT t.name, tl.item_id FROM tag_links tl JOIN tags t ON t.id = tl.tag_id").fetchall()
        tagged_ids = {r["item_id"] for r in tag_rows if _tag_matches(r["name"], tag)}
        entries = [e for e in entries if e.item_id in tagged_ids]
    if collection is not None:
        from chatvault.brain.core import resolve_collection

        coll = resolve_collection(conn, collection)
        coll_ids = {r["item_id"] for r in conn.execute("SELECT item_id FROM collection_items WHERE collection_id = ?", (coll["id"],))}
        entries = [e for e in entries if e.item_id in coll_ids]
    if length is not None or lang is not None:
        rows_by_id = {r["id"]: r for r in rows}
        if length is not None:
            entries = [e for e in entries if (rows_by_id[e.item_id]["length"] or None) == length]
        if lang is not None:
            entries = [e for e in entries if (rows_by_id[e.item_id]["lang"] or None) == lang]
    return entries


def find_entries(conn: sqlite3.Connection, query: str, kind: str | None = None, tag: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    """FTS over title, why, body, ref, aliases, tags (§1.9) — aliases and
    tags are indexed in items_fts too, so a search hits even when the query
    matches only an alias you gave the item, never its title or body."""
    rows = conn.execute(
        """
        SELECT items.*, bm25(items_fts) AS search_rank
        FROM items_fts JOIN items ON items.id = items_fts.rowid
        WHERE items_fts MATCH ? AND items.status = 'active' AND items.excluded = 0
        ORDER BY search_rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    if kind is not None:
        rows = [r for r in rows if r["kind"] == kind]
    if tag is not None:
        tag_rows = conn.execute("SELECT t.name, tl.item_id FROM tag_links tl JOIN tags t ON t.id = tl.tag_id").fetchall()
        tagged_ids = {r["item_id"] for r in tag_rows if _tag_matches(r["name"], tag)}
        rows = [r for r in rows if r["id"] in tagged_ids]
    return rows


def tree_entry(conn: sqlite3.Connection, ref) -> list[dict]:
    """Entry + descendants, ranked (§3). A recursive CTE, one level of
    parent_id at a time — the FAQ's §5 point that parent_id alone gives you
    descendants 'in one line'."""
    root = resolve_item(conn, ref)
    rows = conn.execute(
        """
        WITH RECURSIVE descendants(id, depth) AS (
            SELECT ?, 0
            UNION ALL
            SELECT items.id, descendants.depth + 1
            FROM items JOIN descendants ON items.parent_id = descendants.id
            WHERE items.status = 'active'
        )
        SELECT items.*, descendants.depth
        FROM descendants JOIN items ON items.id = descendants.id
        ORDER BY descendants.depth, items.parent_id, items.rank IS NULL, items.rank, items.id
        """,
        (root["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


def variants(conn: sqlite3.Connection, ref) -> list[sqlite3.Row]:
    """All children of <ref>, side by side, ranked."""
    item = resolve_item(conn, ref)
    return conn.execute(
        "SELECT * FROM items WHERE parent_id = ? AND status = 'active' ORDER BY rank IS NULL, rank, id",
        (item["id"],),
    ).fetchall()


def pick(conn: sqlite3.Connection, ref, length: str | None = None, audience: str | None = None) -> sqlite3.Row:
    """The command that answers 'give me the shortest version' — one
    result, no reading (§3). Filters the children of <ref> by meta fields,
    orders by rank (1 = first choice), and if nothing there matches, falls
    back to the entry itself when it satisfies the filters."""
    item = resolve_item(conn, ref)
    candidates = conn.execute(
        "SELECT * FROM items WHERE parent_id = ? AND status = 'active' ORDER BY rank IS NULL, rank, id",
        (item["id"],),
    ).fetchall()

    def _matches(row: sqlite3.Row) -> bool:
        if length is not None and (row["length"] or None) != length:
            return False
        if audience is not None:
            aud = row["audience"] or ""
            if audience not in aud:
                return False
        return True

    for row in candidates:
        if _matches(row):
            return row
    if _matches(item):
        return item
    raise BrainError(f"no entry under {ref!r} matches the given filters")


def archive_entry(conn: sqlite3.Connection, ref) -> dict:
    item = resolve_item(conn, ref)
    conn.execute("UPDATE items SET status = 'archived', updated_at = ? WHERE id = ?", (now(), item["id"]))
    conn.commit()
    return {"item_id": item["id"], "title": item["title"]}


def restore_entry(conn: sqlite3.Connection, ref) -> dict:
    """Undoes `archive entry`. Also clears `excluded` (§8 Q6: `exclude` and
    `archive` are deliberately separate *assertions* — exclude never implies
    "this is done" — but `restore` reads naturally as "bring this back into
    view" regardless of which hidden-state put it out of view, so one
    command undoes either. An item that was never excluded is unaffected
    (excluded is already 0)."""
    item = resolve_item(conn, ref)
    conn.execute("UPDATE items SET status = 'active', excluded = 0, updated_at = ? WHERE id = ?", (now(), item["id"]))
    conn.commit()
    return {"item_id": item["id"], "title": item["title"]}


def exclude_entry(conn: sqlite3.Connection, ref) -> dict:
    """`brain exclude entry <ref>` (docs/51_SPEC_brain_parity_and_naming.md
    §2.1 `exclude`/`restore` row, D3 parity with ChatVault): soft-hide from
    `list entries`/`find`/`export` without asserting the entry is finished
    — that claim is `archive`'s alone (§8 Q6). Reversible with
    `restore entry`."""
    item = resolve_item(conn, ref)
    conn.execute("UPDATE items SET excluded = 1, updated_at = ? WHERE id = ?", (now(), item["id"]))
    conn.commit()
    return {"item_id": item["id"], "title": item["title"]}


def remove_entry(conn: sqlite3.Connection, ref, confirm: bool, config=None) -> dict:
    """The only hard delete in the Brain (§3, §7 acceptance). Backs up
    first — the Brain has no raw/ to rebuild from (BRAIN_FAQ.md §11).

    items.parent_id has no ON DELETE action (deliberately: a silent CASCADE
    here would mean 'remove entry' on a parent quietly destroys its
    children too, exactly the kind of hidden data loss a hard delete should
    never cause) so SQLite's own FK check already refuses to delete an item
    with children — this just turns that into a clear message instead of a
    raw IntegrityError reaching the CLI.

    item_embeddings (migrations/brain/003_semantic_search.sql) is a vec0
    virtual table, which can't carry a real FK/CASCADE at all — confirmed
    by testing a real hard delete against an embedded item, not assumed
    from ChatVault's equivalent comment (which gets away with skipping this
    only because ChatVault never hard-deletes a conversation; the Brain
    does). Deleted explicitly here so a removed item's vector doesn't
    silently keep influencing semantic_find rankings forever."""
    if not confirm:
        raise BrainError("remove entry is a hard delete — pass --confirm")
    item = resolve_item(conn, ref)
    children = conn.execute("SELECT COUNT(*) FROM items WHERE parent_id = ?", (item["id"],)).fetchone()[0]
    if children:
        raise BrainError(f"item {item['id']} ({item['title']!r}) has {children} child item(s) — archive, reparent, or remove them first")
    if config is not None:
        from chatvault.brain.backup import create_brain_backup

        create_brain_backup(config, label="pre-remove")
    conn.execute("DELETE FROM item_embeddings WHERE item_id = ?", (item["id"],))
    conn.execute("DELETE FROM items WHERE id = ?", (item["id"],))
    conn.commit()
    return {"item_id": item["id"], "title": item["title"]}


def redact_entry(conn: sqlite3.Connection, ref, confirm: bool) -> dict:
    """`brain redact <ref>` (docs/51_SPEC_brain_parity_and_naming.md §2.1
    `redact`/`purge` row): wipes an entry's body — the part most likely to
    hold something the user no longer wants stored in plain text — while
    keeping the row itself (title, why, tags, links, history) so the entry
    doesn't just vanish from collections/links pointing at it. Irreversible
    (the text itself is gone, same as ChatVault's own attachment redact),
    so gated by --confirm exactly like `remove entry`. Logged as a history
    row so `show entry --with history` records that a redaction happened."""
    if not confirm:
        raise BrainError("redact is irreversible — pass --confirm")
    item = resolve_item(conn, ref)
    if item["body"] is None:
        return {"item_id": item["id"], "title": item["title"], "action": "already_empty"}
    record_history(conn, item["id"], "body", "(redacted)", None, origin="cli", note="brain redact")
    conn.execute("UPDATE items SET body = NULL, body_sha256 = NULL WHERE id = ?", (item["id"],))
    conn.commit()
    return {"item_id": item["id"], "title": item["title"], "action": "redacted"}


def purge_entry(conn: sqlite3.Connection, ref, confirm: bool, config=None) -> dict:
    """`brain purge <ref>` (§2.1 `redact`/`purge` row): the same hard
    delete as `remove entry`, under the same top-level name the spec asks
    for. Deliberately implemented as a thin call-through to `remove_entry`
    rather than a parallel reimplementation, so the two can never drift
    apart on the safety rules that matter — same --confirm gate, same
    backup-first behaviour, same refusal on an item with children."""
    return remove_entry(conn, ref, confirm=confirm, config=config)


# ────────────────────── promote / copy conversation ──────────────────────
#
# `copy conversation`/`copy stashed` are the new, user-facing names for
# this function (docs/51_SPEC_brain_parity_and_naming.md §1.3) — the
# function itself keeps its original name (§1.4/§8 Q1: renaming internals
# would churn the 46 existing brain tests for zero user-visible benefit).
# What *did* change here is the tag-carryover bug fix (§4): this used to
# INSERT the copied item and stop, silently dropping every ChatVault tag.


def _namespace_tag(name: str) -> str:
    """§4.3: prefix curation-namespace tags with `vault/`; carry everything
    else unchanged."""
    if any(name.startswith(p) for p in _CURATION_TAG_PREFIXES):
        return f"vault/{name}"
    return name


def _fetch_vault_conversation_tags(vault_conn_ro: sqlite3.Connection, conv_id: int) -> list[str]:
    rows = vault_conn_ro.execute(
        """
        SELECT t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id
        WHERE tl.target_type = 'conversation' AND tl.target_id = ?
        """,
        (conv_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _link_brain_tag_with_source(conn: sqlite3.Connection, item_id: int, tag_name: str, source: str) -> None:
    tag_id = _get_or_create_tag(conn, tag_name)
    conn.execute(
        "INSERT INTO tag_links (tag_id, item_id, source) VALUES (?, ?, ?) ON CONFLICT(tag_id, item_id) DO NOTHING",
        (tag_id, item_id, source),
    )


def _carry_tags(
    brain_conn: sqlite3.Connection,
    vault_conn_ro: sqlite3.Connection,
    conv_id: int,
    item_id: int,
    no_tags: bool,
    extra_tags: list[str] | None,
) -> dict:
    """§4.2/§4.3/§4.5: reads the source conversation's tags, namespaces the
    curation-provenance ones, links every one of them to the new Brain item
    with tag_links.source='chatvault' (so `untag --source chatvault`
    reverses the whole carryover at once), and never silently drops a tag —
    anything that can't be carried is returned in `dropped` for the caller
    to print. `--tag` extras are linked with source='manual', same as any
    other manually-applied tag."""
    carried: list[str] = []
    dropped: list[dict] = []

    if not no_tags:
        try:
            source_tags = _fetch_vault_conversation_tags(vault_conn_ro, conv_id)
        except sqlite3.Error as exc:
            source_tags = []
            dropped.append({"tag": "*", "reason": f"couldn't read vault tags: {exc}"})
        for name in source_tags:
            mapped = _namespace_tag(name)
            try:
                _link_brain_tag_with_source(brain_conn, item_id, mapped, "chatvault")
                carried.append(mapped)
            except sqlite3.Error as exc:
                dropped.append({"tag": name, "reason": str(exc)})

    for name in extra_tags or []:
        try:
            _link_brain_tag_with_source(brain_conn, item_id, name, "manual")
            carried.append(name)
        except sqlite3.Error as exc:
            dropped.append({"tag": name, "reason": str(exc)})

    if carried:
        brain_conn.commit()
    return {"carried": carried, "dropped": dropped}


def _source_meta(conv_row: sqlite3.Row) -> str:
    return json.dumps(
        {
            "platform": conv_row["platform"],
            "project": conv_row["project"],
            "started_at": conv_row["started_at"],
            "model_primary": conv_row["model_primary"],
            "message_count": conv_row["message_count"],
        }
    )


def _existing_promoted_item(brain_conn: sqlite3.Connection, source_uid: str) -> sqlite3.Row | None:
    return brain_conn.execute(
        "SELECT * FROM items WHERE source_uid = ? AND status = 'active' ORDER BY id LIMIT 1",
        (source_uid,),
    ).fetchone()


def _mark_promoted(vault_conn_rw: sqlite3.Connection, conv_id: int) -> None:
    tag_id = tags_mod.get_or_create_tag(vault_conn_rw, PROMOTED_TAG)
    vault_conn_rw.execute(
        """
        INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
        VALUES (?, 'conversation', ?, 'brain', ?)
        ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
        """,
        (tag_id, conv_id, now()),
    )
    vault_conn_rw.commit()


def promote(
    brain_conn: sqlite3.Connection,
    vault_conn_ro: sqlite3.Connection,
    vault_conn_rw: sqlite3.Connection | None,
    uid: str,
    why: str,
    kind: str = "conversation",
    profile: str = "lean",
    no_tags: bool = False,
    extra_tags: list[str] | None = None,
) -> dict:
    """`copy conversation` (docs/51_SPEC_brain_parity_and_naming.md §1.3):
    snapshots a vault conversation into brain.sqlite. `profile` picks which
    `export.render_profile` tier the body is rendered with (§4.4, default
    unchanged at 'lean'); the profile actually used is recorded in
    `source_meta['copy_profile']`. Unless `no_tags`, the source
    conversation's own tags are carried over (§4.2/§4.3) — see `_carry_tags`
    for the namespacing/never-drop rules. `extra_tags` are added on top,
    tagged with source='manual'."""
    if why is None or not why.strip():
        raise BrainError("--why is required and must not be empty")
    if kind not in known_kinds(brain_conn):
        raise BrainError(f"kind must be one of {sorted(known_kinds(brain_conn))}, got {kind!r}")
    if profile not in export_mod.PROFILES:
        raise BrainError(f"--profile must be one of {sorted(export_mod.PROFILES)}, got {profile!r}")
    why = why.strip()

    existing = _existing_promoted_item(brain_conn, uid)
    if existing is not None:
        return {
            "item_id": existing["id"], "action": "already_promoted", "title": existing["title"],
            "tags_carried": [], "tags_dropped": [],
        }

    row = vault_conn_ro.execute("SELECT * FROM v_conversations WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        raise BrainError(f"no conversation with uid {uid!r} in the vault")

    body = export_mod.render_profile(vault_conn_ro, row, profile=profile)
    ts = now()
    meta = json.loads(_source_meta(row))
    meta["copy_profile"] = profile
    cur = brain_conn.execute(
        """
        INSERT INTO items (kind, title, why, body, source_kind, source_uid, source_meta, status, created_at, origin_detail)
        VALUES (?, ?, ?, ?, 'chatvault', ?, ?, 'active', ?, 'copy:chatvault')
        """,
        (kind, row["title"] or uid, why, body, uid, json.dumps(meta), ts),
    )
    brain_conn.commit()
    item_id = cur.lastrowid

    tag_result = _carry_tags(brain_conn, vault_conn_ro, row["id"], item_id, no_tags, extra_tags)

    if vault_conn_rw is not None:
        _mark_promoted(vault_conn_rw, row["id"])

    return {
        "item_id": item_id, "action": "promoted", "title": row["title"] or uid,
        "tags_carried": tag_result["carried"], "tags_dropped": tag_result["dropped"],
    }


def promote_from_stash(
    brain_conn: sqlite3.Connection,
    vault_conn_ro: sqlite3.Connection,
    vault_conn_rw: sqlite3.Connection,
    profile: str = "lean",
    no_tags: bool = False,
    extra_tags: list[str] | None = None,
) -> list[dict]:
    """`copy stashed` (§1.3): same tag-carryover and `--profile` behaviour
    as `promote`/`copy conversation`, applied to every not-yet-copied
    stash/ item (§4.2 point 4)."""
    results = []
    for entry in stash_mod.list_all(vault_conn_ro):
        already = vault_conn_ro.execute(
            """
            SELECT 1 FROM tag_links tl JOIN tags t ON t.id = tl.tag_id
            WHERE tl.target_type = 'conversation' AND tl.target_id = ? AND t.name = ?
            """,
            (entry.conv_id, PROMOTED_TAG),
        ).fetchone()
        if already:
            continue
        kind = _STASH_KIND_MAP.get(entry.kind, entry.kind) if entry.kind else "conversation"
        result = promote(
            brain_conn, vault_conn_ro, vault_conn_rw, entry.uid, entry.why, kind=kind,
            profile=profile, no_tags=no_tags, extra_tags=extra_tags,
        )
        results.append(result)
    return results

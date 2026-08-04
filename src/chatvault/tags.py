from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from chatvault.export import slugify

_TARGET_VIEW = {"conversation": "v_conversations", "message": "v_messages", "block": "v_blocks"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_or_create_tag(conn: sqlite3.Connection, name: str) -> int:
    now = _now()
    conn.execute("INSERT INTO tags (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING", (name, now))
    return conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]


# Public alias — the tagging screen (screen_state.py) needs to create tags
# one link at a time (to track exactly which conversations changed, for
# undo), so it calls straight into tag creation rather than tag_add()'s
# bulk --where path.
get_or_create_tag = _get_or_create_tag
now = _now


@dataclass
class TagTreeNode:
    name: str  # this segment only, e.g. 'macos'
    path: str  # full dotted... slashed path, e.g. 'tech/macos'
    uses: int = 0
    children: dict = field(default_factory=dict)  # name -> TagTreeNode


def build_tag_tree(conn: sqlite3.Connection) -> dict:
    """Every tag in the DB, nested into a tree by its '/'-delimited segments,
    for the tagging screen's clickable tree pane. Intermediate segments that
    have no tag row of their own (e.g. 'tech' when only 'tech/macos/npm'
    exists) still appear, as pure navigation nodes with uses=0."""
    rows = conn.execute(
        """
        SELECT t.name AS name, COUNT(tl.tag_id) AS uses
        FROM tags t LEFT JOIN tag_links tl ON tl.tag_id = t.id
        GROUP BY t.id ORDER BY t.name
        """
    ).fetchall()

    roots: dict = {}
    for row in rows:
        parts = row["name"].split("/")
        level = roots
        path = ""
        for i, part in enumerate(parts):
            path = f"{path}/{part}" if path else part
            if part not in level:
                level[part] = TagTreeNode(name=part, path=path)
            node = level[part]
            if i == len(parts) - 1:
                node.uses = row["uses"]
            level = node.children
    return roots


# ─────────────── hierarchical tag queries (spec §2, §9 item 2) ───────────────
# A trailing "/**" or "/*" means "this tag or anything nested under it, at any
# depth" — the naming convention (slash-delimited segments) already implies
# the hierarchy, so no schema change is needed, just query-time expansion.


def tag_match(column: str, tag_query: str) -> tuple[str, list]:
    """Returns (sql_fragment, params) matching `column` (a tag-name column,
    e.g. 't.name') against `tag_query`. A plain name matches exactly; a
    trailing '/**' or '/*' matches the prefix itself plus everything nested
    below it, at any depth (both wildcard forms are treated the same — the
    spec's own examples don't distinguish one level from all levels)."""
    for suffix in ("/**", "/*"):
        if tag_query.endswith(suffix):
            prefix = tag_query[: -len(suffix)]
            return f"({column} = ? OR {column} LIKE ?)", [prefix, f"{prefix}/%"]
    return f"{column} = ?", [tag_query]


def tag_add(conn: sqlite3.Connection, name: str, where: str, target: str = "conversation", source: str = "manual") -> int:
    """Like curation.tag_add but with a caller-chosen `source` — used by
    import-from-folders ('folder_import') and rules ('rule') so the
    resulting tag_links stay bulk-reversible by source."""
    view = _TARGET_VIEW[target]
    now = _now()
    tag_id = _get_or_create_tag(conn, name)
    ids = [row[0] for row in conn.execute(f"SELECT id FROM {view} WHERE {where}").fetchall()]  # noqa: S608
    for target_id in ids:
        conn.execute(
            """
            INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
            """,
            (tag_id, target, target_id, source, now),
        )
    conn.commit()
    return len(ids)


def untag(conn: sqlite3.Connection, name: str, where: str | None = None, target: str = "conversation", source: str | None = None) -> int:
    """Bulk-reversible companion to tag_add: removes tag_links for `name`,
    optionally narrowed by `source` (e.g. 'folder_import' or 'rule' to undo
    exactly one bulk operation) and/or `where` over the target view."""
    view = _TARGET_VIEW[target]
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is None:
        return 0
    tag_id = row[0]

    clauses = ["tag_id = ?", "target_type = ?"]
    params: list = [tag_id, target]
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if where:
        clauses.append(f"target_id IN (SELECT id FROM {view} WHERE {where})")  # noqa: S608

    cur = conn.execute(f"DELETE FROM tag_links WHERE {' AND '.join(clauses)}", params)  # noqa: S608
    conn.commit()
    return cur.rowcount


# ─────────────── tag import-from-folders (spec §3, §9 item 1) ───────────────

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class FolderImportReport:
    matched: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)


def _extract_date(stem: str) -> str | None:
    m = _DATE_RE.search(stem)
    return m.group(0) if m else None


def _candidate_slug(stem: str, date: str | None) -> str:
    text = stem.replace(date, "") if date else stem
    return slugify(text)


def _find_conversation(conn: sqlite3.Connection, stem: str) -> sqlite3.Row | str | None:
    """Returns a matching row, None (no match), or the string 'ambiguous'."""
    date = _extract_date(stem)
    candidate = _candidate_slug(stem, date)
    if not candidate or candidate == "untitled":
        return None

    def _search(rows: list[sqlite3.Row]):
        exact = [r for r in rows if slugify(r["title"]) == candidate]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return "ambiguous"
        partial = [
            r for r in rows
            if candidate in slugify(r["title"]) or slugify(r["title"]) in candidate
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            return "ambiguous"
        return None

    if date:
        dated_rows = conn.execute("SELECT * FROM v_conversations WHERE started_at LIKE ?", (f"{date}%",)).fetchall()
        result = _search(dated_rows)
        if result == "ambiguous":
            return "ambiguous"
        if result is not None:
            return result

    all_rows = conn.execute("SELECT * FROM v_conversations").fetchall()
    return _search(all_rows)


def import_from_folders(conn: sqlite3.Connection, root: Path, prefix: str | None = None, dry_run: bool = False) -> FolderImportReport:
    """Walk `root`, turn each file's path into a hierarchical tag
    ('PSA/troubleshooting/network.md' -> 'project/claude-web/PSA/
    troubleshooting/network' with --prefix 'project/claude-web'), match the
    file to a conversation by title+date, and tag it (source='folder_import',
    bulk-reversible via `chatvault tag untag --source folder_import`).
    Unmatched files are reported, never silently dropped."""
    report = FolderImportReport()
    now = _now()

    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))
    for file_path in files:
        rel = file_path.relative_to(root)
        # the leaf segment drops any embedded date (used for matching, not
        # naming) so the tag reads as a topic, not a dated log entry
        leaf = _candidate_slug(rel.stem, _extract_date(rel.stem)) or slugify(rel.stem)
        segments = [slugify(part) for part in rel.parts[:-1]] + [leaf]
        tag_name = "/".join(([prefix] if prefix else []) + segments)

        conv = _find_conversation(conn, rel.stem)
        if conv is None or conv == "ambiguous":
            reason = "ambiguous — multiple conversations matched" if conv == "ambiguous" else "no matching conversation"
            report.unmatched.append({"file": str(rel), "reason": reason})
            continue

        report.matched.append({"file": str(rel), "conv_uid": conv["uid"], "tag": tag_name})
        if not dry_run:
            tag_id = _get_or_create_tag(conn, tag_name)
            conn.execute(
                """
                INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
                VALUES (?, 'conversation', ?, 'folder_import', ?)
                ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
                """,
                (tag_id, conv["id"], now),
            )

    if not dry_run:
        conn.commit()
    return report


# ─────────────── tag rules — automatic tagging on ingest (spec §6, §9 item 5) ───────────────


def rule_add(conn: sqlite3.Connection, tag_name: str, where_sql: str, min_hits: int = 1) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO tag_rules (tag_name, where_sql, min_hits, enabled, created_at) VALUES (?, ?, ?, 1, ?)",
        (tag_name, where_sql, min_hits, now),
    )
    conn.commit()
    return cur.lastrowid


def rule_list(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tag_rules ORDER BY id").fetchall()


def rule_remove(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM tag_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def _rule_matches(conn: sqlite3.Connection, rule: sqlite3.Row, conv_id: int) -> bool:
    sql = f"SELECT COUNT(*) FROM blocks WHERE conv_id = ? AND excluded = 0 AND ({rule['where_sql']})"  # noqa: S608
    n = conn.execute(sql, (conv_id,)).fetchone()[0]
    return n >= rule["min_hits"]


def apply_rules(conn: sqlite3.Connection, conv_ids: list[int] | None = None) -> int:
    """Evaluate every enabled rule against `conv_ids` (default: every live
    conversation) and apply matching tags, source='rule'. Called
    automatically after ingest for newly inserted/superseded conversations;
    also callable directly to backfill existing ones."""
    rules = conn.execute("SELECT * FROM tag_rules WHERE enabled = 1").fetchall()
    if not rules:
        return 0

    target_ids = conv_ids
    if target_ids is None:
        target_ids = [row[0] for row in conn.execute("SELECT id FROM v_conversations").fetchall()]
    if not target_ids:
        return 0

    now = _now()
    applied = 0
    for rule in rules:
        tag_id = _get_or_create_tag(conn, rule["tag_name"])
        for conv_id in target_ids:
            if not _rule_matches(conn, rule, conv_id):
                continue
            cur = conn.execute(
                """
                INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
                VALUES (?, 'conversation', ?, 'rule', ?)
                ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
                """,
                (tag_id, conv_id, now),
            )
            if cur.rowcount:
                applied += 1
    conn.commit()
    return applied


# ─────────────── tag import-tree from YAML (Tagging Workflow §2, §8 item 1) ───────────────


def _walk_taxonomy(node, prefix: str):
    """Yields every tag path implied by a nested taxonomy structure. Both
    branch nodes (dict keys, list-holders) and leaf nodes (list items, or a
    dict key whose value is null) become tags — the doc's own mockup shows
    branches as expand/collapse nodes in the tree and leaves as click-to-
    apply buttons, but nothing stops a branch being applied directly too, so
    every node in the tree gets a real row."""
    if node is None:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}/{slugify(str(key))}" if prefix else slugify(str(key))
            yield path
            yield from _walk_taxonomy(value, path)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                yield from _walk_taxonomy(item, prefix)
            else:
                yield f"{prefix}/{slugify(str(item))}" if prefix else slugify(str(item))
    else:
        yield f"{prefix}/{slugify(str(node))}" if prefix else slugify(str(node))


def import_tree(conn: sqlite3.Connection, path: Path, dry_run: bool = False) -> list[str]:
    """Parses a nested YAML taxonomy into real tag rows — no tag_links, just
    makes the whole tree exist in the palette before any conversation is
    tagged. Idempotent: existing tags are left alone (ON CONFLICT DO NOTHING)."""
    data = yaml.safe_load(path.read_text()) or {}
    tag_names = sorted(set(_walk_taxonomy(data, "")))
    if not dry_run:
        for name in tag_names:
            _get_or_create_tag(conn, name)
        conn.commit()
    return tag_names


# ─────────────── tag rename / merge (Tagging Workflow §8 item 5) ───────────────


def rename(conn: sqlite3.Connection, old_name: str, new_name: str) -> int:
    """Renames `old_name` and everything nested under it (old_name/**) to the
    equivalent path under `new_name`. Every tag_link is preserved as-is (they
    reference tag_id, never name) — this only ever touches tags.name, merging
    into an existing tag if the new path collides with one that already
    exists (rather than erroring), same reasoning as merge() below."""
    affected = conn.execute(
        "SELECT id, name FROM tags WHERE name = ? OR name LIKE ?",
        (old_name, f"{old_name}/%"),
    ).fetchall()

    renamed = 0
    for row in affected:
        suffix = row["name"][len(old_name):]  # '' for the exact tag, '/child/...' for descendants
        target_name = new_name + suffix
        existing = conn.execute("SELECT id FROM tags WHERE name = ?", (target_name,)).fetchone()
        if existing is not None and existing["id"] != row["id"]:
            conn.execute("UPDATE OR IGNORE tag_links SET tag_id = ? WHERE tag_id = ?", (existing["id"], row["id"]))
            conn.execute("DELETE FROM tag_links WHERE tag_id = ?", (row["id"],))  # unmigratable duplicates
            conn.execute("DELETE FROM tags WHERE id = ?", (row["id"],))
        else:
            conn.execute("UPDATE tags SET name = ? WHERE id = ?", (target_name, row["id"]))
        renamed += 1

    conn.commit()
    return renamed


def merge(conn: sqlite3.Connection, source_name: str, target_name: str) -> int:
    """Moves every tag_link from `source_name` onto `target_name` (both must
    already exist), then deletes the now-empty source tag. Unlike rename,
    this does not recurse into nested tags — merge consolidates two specific
    tags, not a branch. Returns the number of links actually moved."""
    source = conn.execute("SELECT id FROM tags WHERE name = ?", (source_name,)).fetchone()
    target = conn.execute("SELECT id FROM tags WHERE name = ?", (target_name,)).fetchone()
    if source is None:
        raise ValueError(f"no tag named {source_name!r}")
    if target is None:
        raise ValueError(f"no tag named {target_name!r}")
    if source["id"] == target["id"]:
        return 0

    before = conn.execute("SELECT COUNT(*) FROM tag_links WHERE tag_id = ?", (source["id"],)).fetchone()[0]
    conn.execute("UPDATE OR IGNORE tag_links SET tag_id = ? WHERE tag_id = ?", (target["id"], source["id"]))
    remaining = conn.execute("SELECT COUNT(*) FROM tag_links WHERE tag_id = ?", (source["id"],)).fetchone()[0]
    moved = before - remaining

    conn.execute("DELETE FROM tag_links WHERE tag_id = ?", (source["id"],))  # links target already had too
    conn.execute("DELETE FROM tags WHERE id = ?", (source["id"],))
    conn.commit()
    return moved

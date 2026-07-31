from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chatvault import backup as backup_mod
from chatvault import hashing
from chatvault.config import Config

SCHEMA_VERSION = 1
_CONTENT_HASH_RULE_RE = re.compile(r"^\s*content_hash\s*=\s*'([0-9a-f]{64})'\s*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ImportSummary:
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    unresolved: list[dict] = field(default_factory=list)


# ─────────────── target resolution ───────────────


def _conv_row(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT id FROM conversations WHERE uid = ?", (uid,)).fetchone()


def _resolve_target(conn: sqlite3.Connection, target: dict) -> tuple[str, list[int]]:
    """Returns (status, ids). status in {"ok", "missing_target", "content_drift"}."""
    kind = target["kind"]

    if kind == "conversation":
        row = _conv_row(conn, target["conv_uid"])
        if row is None:
            return "missing_target", []
        return "ok", [row["id"]]

    if kind == "message":
        conv = _conv_row(conn, target["conv_uid"])
        if conv is None:
            return "missing_target", []
        row = conn.execute(
            "SELECT id, content_hash FROM messages WHERE conv_id = ? AND idx = ?",
            (conv["id"], target["msg_idx"]),
        ).fetchone()
        if row is None:
            return "missing_target", []
        if row["content_hash"] != target.get("content_hash"):
            return "content_drift", []
        return "ok", [row["id"]]

    if kind == "block":
        conv = _conv_row(conn, target["conv_uid"])
        if conv is None:
            return "missing_target", []
        msg = conn.execute(
            "SELECT id FROM messages WHERE conv_id = ? AND idx = ?",
            (conv["id"], target["msg_idx"]),
        ).fetchone()
        if msg is None:
            return "missing_target", []
        row = conn.execute(
            "SELECT id, content_hash FROM blocks WHERE msg_id = ? AND idx = ?",
            (msg["id"], target["block_idx"]),
        ).fetchone()
        if row is None:
            return "missing_target", []
        if row["content_hash"] != target.get("content_hash"):
            return "content_drift", []
        return "ok", [row["id"]]

    if kind == "block_class":
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM blocks WHERE content_hash = ?", (target["content_hash"],)
            ).fetchall()
        ]
        return "ok", ids  # empty is valid (no matching blocks yet) — not a resolution failure

    raise ValueError(f"unknown target kind {kind!r}")


def _target_for_row(conn: sqlite3.Connection, target_type: str, target_id: int) -> dict | None:
    """Inverse of _resolve_target — used by export."""
    if target_type == "conversation":
        row = conn.execute("SELECT uid FROM conversations WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            return None
        return {"kind": "conversation", "conv_uid": row["uid"]}

    if target_type == "message":
        row = conn.execute(
            """SELECT c.uid AS conv_uid, m.idx AS msg_idx, m.content_hash AS content_hash
               FROM messages m JOIN conversations c ON c.id = m.conv_id WHERE m.id = ?""",
            (target_id,),
        ).fetchone()
        if row is None:
            return None
        return {"kind": "message", "conv_uid": row["conv_uid"], "msg_idx": row["msg_idx"], "content_hash": row["content_hash"]}

    if target_type == "block":
        row = conn.execute(
            """SELECT c.uid AS conv_uid, m.idx AS msg_idx, b.idx AS block_idx, b.content_hash AS content_hash
               FROM blocks b JOIN messages m ON m.id = b.msg_id JOIN conversations c ON c.id = b.conv_id WHERE b.id = ?""",
            (target_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "kind": "block", "conv_uid": row["conv_uid"], "msg_idx": row["msg_idx"],
            "block_idx": row["block_idx"], "content_hash": row["content_hash"],
        }

    return None


# ─────────────── export ───────────────


def export_curation(conn: sqlite3.Connection) -> dict:
    tags = [dict(r) for r in conn.execute("SELECT name, colour, created_at FROM tags ORDER BY name").fetchall()]

    tag_links = []
    for r in conn.execute(
        "SELECT tl.tag_id, t.name AS tag_name, tl.target_type, tl.target_id, tl.source, tl.created_at "
        "FROM tag_links tl JOIN tags t ON t.id = tl.tag_id"
    ).fetchall():
        target = _target_for_row(conn, r["target_type"], r["target_id"])
        if target is None:
            continue
        tag_links.append({"tag": r["tag_name"], "target": target, "source": r["source"], "created_at": r["created_at"]})

    annotations = []
    for r in conn.execute("SELECT target_type, target_id, note, created_at FROM annotations").fetchall():
        target = _target_for_row(conn, r["target_type"], r["target_id"])
        if target is None:
            continue
        annotations.append({"target": target, "note": r["note"], "created_at": r["created_at"]})

    # exclusions: derived from CURRENT state (excluded=1 rows), each paired with
    # its most recent exclude-action log entry for reason/rule/created_at.
    exclusions = []
    seen_block_class_hashes: set[str] = set()
    for target_type, table in (("conversation", "conversations"), ("message", "messages"), ("block", "blocks")):
        for row in conn.execute(f"SELECT id, content_hash FROM {table} WHERE excluded = 1").fetchall():  # noqa: S608
            log_row = conn.execute(
                """SELECT reason, rule, created_at FROM exclusion_log
                   WHERE target_type = ? AND target_id = ? AND action = 'exclude'
                   ORDER BY id DESC LIMIT 1""",
                (target_type, row["id"]),
            ).fetchone()
            rule = log_row["rule"] if log_row else None

            m = _CONTENT_HASH_RULE_RE.match(rule) if (rule and target_type == "block") else None
            if m:
                content_hash = m.group(1)
                if content_hash in seen_block_class_hashes:
                    continue
                seen_block_class_hashes.add(content_hash)
                exclusions.append(
                    {
                        "target": {"kind": "block_class", "content_hash": content_hash},
                        "reason": log_row["reason"] if log_row else None,
                        "rule": rule,
                        "created_at": log_row["created_at"] if log_row else None,
                    }
                )
                continue

            target = _target_for_row(conn, target_type, row["id"])
            if target is None:
                continue
            exclusions.append(
                {
                    "target": target,
                    "reason": log_row["reason"] if log_row else None,
                    "rule": rule,
                    "created_at": log_row["created_at"] if log_row else None,
                }
            )

    saved_views = [dict(r) for r in conn.execute("SELECT name, description, sql FROM saved_views ORDER BY name").fetchall()]

    return {
        "schema": SCHEMA_VERSION,
        "exported_at": _now(),
        "counts": {
            "tags": len(tags), "tag_links": len(tag_links), "annotations": len(annotations),
            "exclusions": len(exclusions), "saved_views": len(saved_views),
        },
        "tags": tags,
        "tag_links": tag_links,
        "annotations": annotations,
        "exclusions": exclusions,
        "saved_views": saved_views,
    }


# ─────────────── import ───────────────


def _import_tags(conn, entries, on_conflict, dry_run, counts, unresolved):
    added = updated = unchanged = 0
    for e in entries:
        existing = conn.execute("SELECT colour FROM tags WHERE name = ?", (e["name"],)).fetchone()
        if existing is None:
            if not dry_run:
                conn.execute(
                    "INSERT INTO tags (name, colour, created_at) VALUES (?, ?, ?)",
                    (e["name"], e.get("colour"), e.get("created_at") or _now()),
                )
            added += 1
        elif on_conflict == "overwrite":
            if not dry_run:
                conn.execute("UPDATE tags SET colour = ? WHERE name = ?", (e.get("colour"), e["name"]))
            updated += 1
        else:
            unchanged += 1
    counts["tags"] = {"added": added, "updated": updated, "unchanged": unchanged}


def _import_tag_links(conn, entries, on_conflict, dry_run, counts, unresolved):
    added = updated = unchanged = 0
    for e in entries:
        tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (e["tag"],)).fetchone()
        if tag_row is None:
            unresolved.append({"entity": "tag_links", "target": e["target"], "reason": "missing_target", "detail": f"tag {e['tag']!r} not found"})
            continue
        status, ids = _resolve_target(conn, e["target"])
        if status != "ok" or not ids:
            unresolved.append({"entity": "tag_links", "target": e["target"], "reason": status if status != "ok" else "missing_target", "detail": ""})
            continue
        target_type = e["target"]["kind"]
        for target_id in ids:
            existing = conn.execute(
                "SELECT source FROM tag_links WHERE tag_id = ? AND target_type = ? AND target_id = ?",
                (tag_row["id"], target_type, target_id),
            ).fetchone()
            if existing is None:
                if not dry_run:
                    conn.execute(
                        "INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at) VALUES (?, ?, ?, ?, ?)",
                        (tag_row["id"], target_type, target_id, e.get("source", "manual"), e.get("created_at") or _now()),
                    )
                added += 1
            elif on_conflict == "overwrite":
                if not dry_run:
                    conn.execute(
                        "UPDATE tag_links SET source = ? WHERE tag_id = ? AND target_type = ? AND target_id = ?",
                        (e.get("source", "manual"), tag_row["id"], target_type, target_id),
                    )
                updated += 1
            else:
                unchanged += 1
    counts["tag_links"] = {"added": added, "updated": updated, "unchanged": unchanged}


def _import_saved_views(conn, entries, on_conflict, dry_run, counts, unresolved):
    added = updated = unchanged = 0
    for e in entries:
        existing = conn.execute("SELECT 1 FROM saved_views WHERE name = ?", (e["name"],)).fetchone()
        if existing is None:
            if not dry_run:
                conn.execute(
                    "INSERT INTO saved_views (name, description, sql, created_at) VALUES (?, ?, ?, ?)",
                    (e["name"], e.get("description"), e["sql"], _now()),
                )
            added += 1
        elif on_conflict == "overwrite":
            if not dry_run:
                conn.execute(
                    "UPDATE saved_views SET sql = ?, description = ? WHERE name = ?",
                    (e["sql"], e.get("description"), e["name"]),
                )
            updated += 1
        else:
            unchanged += 1
    counts["saved_views"] = {"added": added, "updated": updated, "unchanged": unchanged}


def _import_exclusions(conn, entries, on_conflict, dry_run, counts, unresolved):
    added = updated = unchanged = 0
    for e in entries:
        target = e["target"]
        status, ids = _resolve_target(conn, target)
        if status == "content_drift":
            unresolved.append({"entity": "exclusions", "target": target, "reason": "content_drift", "detail": ""})
            continue
        if status == "missing_target":
            unresolved.append({"entity": "exclusions", "target": target, "reason": "missing_target", "detail": ""})
            continue
        target_type = "block" if target["kind"] == "block_class" else target["kind"]
        for target_id in ids:
            row = conn.execute(f"SELECT excluded FROM {target_type}s WHERE id = ?", (target_id,)).fetchone()  # noqa: S608
            if row is None:
                continue
            if row["excluded"] == 0:
                if not dry_run:
                    conn.execute(f"UPDATE {target_type}s SET excluded = 1 WHERE id = ?", (target_id,))  # noqa: S608
                    conn.execute(
                        "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) VALUES (?, ?, 'exclude', ?, ?, ?)",
                        (target_type, target_id, e.get("reason"), e.get("rule"), _now()),
                    )
                added += 1
            elif on_conflict == "overwrite":
                if not dry_run:
                    conn.execute(
                        """UPDATE exclusion_log SET reason = ? WHERE id = (
                               SELECT id FROM exclusion_log WHERE target_type = ? AND target_id = ? AND action = 'exclude'
                               ORDER BY id DESC LIMIT 1
                           )""",
                        (e.get("reason"), target_type, target_id),
                    )
                updated += 1
            else:
                unchanged += 1
    counts["exclusions"] = {"added": added, "updated": updated, "unchanged": unchanged}


def _import_annotations(conn, entries, dry_run, counts, unresolved):
    """Always additive-with-dedupe. on_conflict does not apply (Patch 3)."""
    added = unchanged = 0
    for e in entries:
        status, ids = _resolve_target(conn, e["target"])
        if status != "ok" or not ids:
            unresolved.append({"entity": "annotations", "target": e["target"], "reason": status if status != "ok" else "missing_target", "detail": ""})
            continue
        target_type = e["target"]["kind"]
        key_hash = hashing.note_hash(e["note"])
        for target_id in ids:
            exists = conn.execute(
                "SELECT 1 FROM annotations WHERE target_type = ? AND target_id = ? AND note_hash = ?",
                (target_type, target_id, key_hash),
            ).fetchone()
            if exists:
                unchanged += 1
                continue
            if not dry_run:
                conn.execute(
                    "INSERT INTO annotations (target_type, target_id, note, note_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                    (target_type, target_id, e["note"], key_hash, e.get("created_at") or _now()),
                )
            added += 1
    counts["annotations"] = {"added": added, "updated": 0, "unchanged": unchanged}


def import_curation(conn: sqlite3.Connection, data: dict, on_conflict: str = "skip", dry_run: bool = False) -> ImportSummary:
    if on_conflict not in ("skip", "overwrite"):
        raise ValueError(f"on_conflict must be 'skip' or 'overwrite', got {on_conflict!r}")

    counts: dict[str, dict[str, int]] = {}
    unresolved: list[dict] = []

    _import_tags(conn, data.get("tags", []), on_conflict, dry_run, counts, unresolved)
    _import_saved_views(conn, data.get("saved_views", []), on_conflict, dry_run, counts, unresolved)
    _import_tag_links(conn, data.get("tag_links", []), on_conflict, dry_run, counts, unresolved)
    _import_exclusions(conn, data.get("exclusions", []), on_conflict, dry_run, counts, unresolved)
    _import_annotations(conn, data.get("annotations", []), dry_run, counts, unresolved)

    if not dry_run:
        conn.commit()

    return ImportSummary(counts=counts, unresolved=unresolved)


def diff_curation(conn: sqlite3.Connection, data: dict) -> ImportSummary:
    return import_curation(conn, data, dry_run=True)


def write_unresolved(out_file: Path, unresolved: list[dict]) -> Path:
    path = out_file.with_suffix(out_file.suffix + ".unresolved.json")
    path.write_text(json.dumps(unresolved, indent=2))
    return path


# ─────────────── replace-annotations (destructive escape hatch) ───────────────


def _annotations_under_conversation(conn: sqlite3.Connection, conv_id: int) -> list[sqlite3.Row]:
    rows = list(conn.execute(
        "SELECT id FROM annotations WHERE target_type = 'conversation' AND target_id = ?", (conv_id,)
    ).fetchall())
    for msg in conn.execute("SELECT id FROM messages WHERE conv_id = ?", (conv_id,)).fetchall():
        rows += conn.execute(
            "SELECT id FROM annotations WHERE target_type = 'message' AND target_id = ?", (msg["id"],)
        ).fetchall()
    for blk in conn.execute("SELECT id FROM blocks WHERE conv_id = ?", (conv_id,)).fetchall():
        rows += conn.execute(
            "SELECT id FROM annotations WHERE target_type = 'block' AND target_id = ?", (blk["id"],)
        ).fetchall()
    return rows


@dataclass
class ReplaceSummary:
    deleted: int
    added: int


def replace_annotations(conn: sqlite3.Connection, config: Config, data: dict, target_uid: str, confirm: bool) -> ReplaceSummary:
    """Destructive by explicit design (Law 2 exception): physical DELETE, no
    restore path. Refuses without --confirm. Takes a backup first. Every
    removal is logged to exclusion_log. Build last, or not at all."""
    if not confirm:
        raise ValueError("refusing to replace annotations without --confirm")

    backup_mod.create_backup(config, label="pre-replace-annotations")

    conv = _conv_row(conn, target_uid)
    if conv is None:
        raise ValueError(f"no conversation with uid {target_uid!r}")

    victims = _annotations_under_conversation(conn, conv["id"])
    now = _now()
    for row in victims:
        conn.execute("DELETE FROM annotations WHERE id = ?", (row["id"],))
        conn.execute(
            "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) VALUES ('annotation', ?, 'delete', 'replace-annotations', ?, ?)",
            (row["id"], target_uid, now),
        )

    counts: dict[str, dict[str, int]] = {}
    unresolved: list[dict] = []
    relevant = [a for a in data.get("annotations", []) if a["target"].get("conv_uid") == target_uid]
    _import_annotations(conn, relevant, dry_run=False, counts=counts, unresolved=unresolved)
    conn.commit()

    return ReplaceSummary(deleted=len(victims), added=counts.get("annotations", {}).get("added", 0))

"""One-off, idempotent repairs of rows stored before a rule existed.

`gemini_canvas` brings every already-ingested Gemini standalone event to the
shape the adapter now emits (docs/prompts/78): canvases become a short summary
plus an artifact attachment under a stable `canvas:` id, other events get the
normalised-title id, cross-export copies fold into one live row, and every
title in the vault passes `normalize_title`. Nothing is deleted (D3): the old
content blocks stay, excluded with a logged reason, and duplicates get
`superseded_by`. Dry-run by default; `apply=True` writes in one transaction.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import orjson

from chatvault import hashing
from chatvault import loader
from chatvault.adapters import gemini_takeout as gemini
from chatvault.adapters.base import normalize_title

EXCLUDE_REASON = "canvas moved to artifact"
EXCLUDE_RULE = "repair gemini-canvas"
_OLD_IDX_OFFSET = 1000  # excluded legacy blocks move here so the new shape owns idx 0 and 1


def recent_backup_exists(config, max_age: timedelta = timedelta(hours=1)) -> bool:
    from chatvault import backup as backup_mod

    cutoff = datetime.now(timezone.utc) - max_age
    for info in backup_mod.list_backups(config):
        try:
            created = datetime.fromisoformat(info.created_at)
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            return True
    return False


def _assistant_times(conn: sqlite3.Connection) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        """
        SELECT c.native_id, m.created_at
        FROM messages m JOIN conversations c ON c.id = m.conv_id
        WHERE c.platform = 'gemini' AND m.role = 'assistant' AND c.superseded_by IS NULL
          AND c.native_id IS NOT NULL AND c.native_id NOT LIKE 'event:%' AND c.native_id NOT LIKE 'canvas:%'
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _canvas_cir(conn: sqlite3.Connection, row: sqlite3.Row, assistant_times: list) -> tuple[dict | None, str]:
    """Rebuild what the adapter emits for a stored canvas row from its stored
    raw record (JSON variant: the conversation's raw_json; HTML variant: the
    cell kept on the original block). Returns (cir, "") or (None, reason)."""
    raw = orjson.loads(row["raw_json"]) if row["raw_json"] else {}
    if raw.get("html_cell"):
        first = conn.execute(
            "SELECT b.raw_json FROM blocks b JOIN messages m ON m.id = b.msg_id "
            "WHERE m.conv_id = ? AND m.idx = 0 AND b.idx = 0",
            (row["id"],),
        ).fetchone()
        cell = orjson.loads(first[0]) if first and first[0] else None
        if not isinstance(cell, str):
            return None, "original HTML cell not stored on block 0"
        rec = gemini._parse_html_cell(cell)
        if rec is None or not gemini.is_canvas_title(rec["title"]):
            return None, "stored cell does not parse as a canvas"
        return gemini.canvas_from_html_record(rec, "", assistant_times), ""
    if not raw.get("title"):
        return None, "no stored record"
    return gemini._parse_standalone_event(raw, assistant_times), ""


def _live(row) -> bool:
    return row["superseded_by"] is None and not row["excluded"]


def gemini_canvas(conn: sqlite3.Connection, apply: bool = False) -> dict:
    """Plan (dry-run) or perform (apply) the repair. Returns a report dict."""
    assistant_times = _assistant_times(conn)
    rows = conn.execute(
        """
        SELECT id, uid, native_id, title, started_at, raw_json, superseded_by, excluded, ingested_at
        FROM conversations WHERE platform = 'gemini' AND native_id LIKE 'event:%' ORDER BY id
        """
    ).fetchall()

    report: dict = {
        "events_selected": len(rows),
        "canvases_base": 0,
        "canvases_live": 0,
        "events_other_base": 0,
        "events_other_live": 0,
        "skipped": [],
        "duplicate_pairs": [],
        "event_id_changes": 0,
        "titles_normalised": {},
        "titles_normalised_live": {},
        "stale_conv_vectors_removed": 0,
        "stale_passages_removed": 0,
        "tagged": 0,
    }

    plan: list[dict] = []
    for row in rows:
        is_canvas = gemini.is_canvas_title(row["title"])
        report["canvases_base" if is_canvas else "events_other_base"] += 1
        if _live(row):
            report["canvases_live" if is_canvas else "events_other_live"] += 1
        if is_canvas:
            cir, why = _canvas_cir(conn, row, assistant_times)
            if cir is None:
                report["skipped"].append({"id": row["id"], "why": why})
                continue
            plan.append({"row": row, "cir": cir, "kind": "canvas", "native_id": cir["native_id"]})
        else:
            new_nid = gemini.event_native_id(row["title"] or "", row["started_at"])
            plan.append({"row": row, "cir": None, "kind": "event", "native_id": new_nid})

    # Fold rows that now share an identity: earliest-ingested live row stays.
    planned_ids = {p["row"]["id"] for p in plan}
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in plan:
        groups[p["native_id"]].append(p)
    for nid, members in groups.items():
        placeholders = {m["row"]["id"] for m in members}
        for other in conn.execute(
            "SELECT id, uid, native_id, title, ingested_at, superseded_by, excluded FROM conversations "
            "WHERE platform = 'gemini' AND native_id = ?",
            (nid,),
        ).fetchall():
            if other["id"] not in placeholders and other["id"] not in planned_ids:
                members.append({"row": other, "cir": None, "kind": "existing", "native_id": nid})
        live_members = [m for m in members if m["row"]["superseded_by"] is None]
        live_members.sort(key=lambda m: (m["row"]["ingested_at"], m["row"]["id"]))
        canonical = live_members[0] if live_members else None
        for m in members:
            m["canonical_id"] = canonical["row"]["id"] if canonical else None
        for m in live_members[1:]:
            if m["kind"] == "existing":
                continue
            m["fold_into"] = canonical["row"]["id"]
            report["duplicate_pairs"].append(
                {
                    "kept": canonical["row"]["id"],
                    "folded": m["row"]["id"],
                    "name": (canonical["cir"] or m["cir"] or {}).get("title") or m["row"]["title"],
                    "kept_chars": _content_len(canonical),
                    "folded_chars": _content_len(m),
                }
            )
    report["event_id_changes"] = sum(
        1 for p in plan if p["kind"] == "event" and p["native_id"] != p["row"]["native_id"]
    )

    # Titles: every other conversation, every platform.
    title_updates: list[tuple[str | None, int]] = []
    changed_by_canvas = {p["row"]["id"] for p in plan if p["kind"] == "canvas"}
    for r in conn.execute("SELECT id, platform, title, superseded_by, excluded FROM conversations WHERE title IS NOT NULL"):
        if r["id"] in changed_by_canvas:
            continue
        new = normalize_title(r["title"])
        if new != r["title"]:
            title_updates.append((new, r["id"]))
            report["titles_normalised"][r["platform"]] = report["titles_normalised"].get(r["platform"], 0) + 1
            if r["superseded_by"] is None and not r["excluded"]:
                report["titles_normalised_live"][r["platform"]] = report["titles_normalised_live"].get(r["platform"], 0) + 1

    report["rows_to_rewrite"] = sum(
        1 for p in plan if p["kind"] == "canvas" or p["native_id"] != p["row"]["native_id"] or p.get("fold_into")
    )
    if not apply:
        return report

    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN")
        # Folds first: a duplicate that already holds the new uid must step
        # aside (uid suffix) before the canonical row is renamed to it.
        for p in plan:
            if p.get("fold_into"):
                _fold(conn, p["row"]["id"], p["fold_into"], now)
        for p in plan:
            if p["kind"] == "canvas":
                _rewrite_canvas(conn, p, now)
            elif p["native_id"] != p["row"]["native_id"]:
                _rewrite_event_identity(conn, p)
        for new, cid in title_updates:
            conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (new, cid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    from chatvault import chunk_embeddings, embeddings, tags

    report["tagged"] = tags.flag_gemini_canvas(conn)
    report["stale_conv_vectors_removed"] = embeddings.purge_stale(conn)
    report["stale_passages_removed"] = chunk_embeddings.purge_stale(conn)
    return report


def _content_len(member: dict) -> int | None:
    cir = member.get("cir")
    if cir:
        att = cir["messages"][0]["blocks"][1]["attachment"]
        return att["bytes"]
    return None


def _fold(conn: sqlite3.Connection, dup_id: int, keep_id: int, now: str) -> None:
    uid = conn.execute("SELECT uid FROM conversations WHERE id = ?", (dup_id,)).fetchone()[0]
    suffix = f"#superseded-{dup_id}"
    new_uid = uid if uid.endswith(suffix) else uid + suffix
    conn.execute("UPDATE conversations SET superseded_by = ?, uid = ? WHERE id = ?", (keep_id, new_uid, dup_id))
    conn.execute(
        "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) "
        "VALUES ('conversation', ?, 'supersede', ?, ?, ?)",
        (dup_id, f"same canvas/event as conversation {keep_id} (cross-export copy)", EXCLUDE_RULE, now),
    )


def _uid(p: dict, content_hash: str) -> str:
    """Derived uid; a row about to be folded takes the superseded suffix up
    front so it can never collide with the canonical row's uid."""
    uid = hashing.make_uid("gemini", p["native_id"], content_hash)
    return f"{uid}#superseded-{p['row']['id']}" if p.get("fold_into") else uid


def _rewrite_event_identity(conn: sqlite3.Connection, p: dict) -> None:
    row = p["row"]
    conn.execute(
        "UPDATE conversations SET native_id = ?, uid = ?, title = ? WHERE id = ?",
        (p["native_id"], _uid(p, ""), normalize_title(row["title"]), row["id"]),
    )


def _rewrite_canvas(conn: sqlite3.Connection, p: dict, now: str) -> None:
    row, cir = p["row"], p["cir"]
    hashes = loader._compute_hashes(cir)
    msg = conn.execute("SELECT id FROM messages WHERE conv_id = ? AND idx = 0", (row["id"],)).fetchone()
    msg_id = msg["id"]
    message = cir["messages"][0]

    # Legacy content blocks: keep (D3), exclude with a logged reason, make room at idx 0/1.
    for b in conn.execute("SELECT id, idx, excluded FROM blocks WHERE msg_id = ? ORDER BY idx DESC", (msg_id,)).fetchall():
        if b["idx"] < _OLD_IDX_OFFSET:
            conn.execute("UPDATE blocks SET idx = ?, excluded = 1 WHERE id = ?", (b["idx"] + _OLD_IDX_OFFSET, b["id"]))
            conn.execute(
                "INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at) "
                "VALUES ('block', ?, 'exclude', ?, ?, ?)",
                (b["id"], EXCLUDE_REASON, EXCLUDE_RULE, now),
            )

    block_chars = 0
    for block, b_hash in zip(message["blocks"], hashes["per_message_block_hashes"][0]):
        attachment_id = None
        att = block.get("attachment")
        if att:
            attachment_id = loader._get_or_create_attachment(conn, cir["platform"], att, now)
            loader._link_attachment(conn, attachment_id, "message", msg_id, att.get("role", "upload"), now)
        conn.execute(
            """
            INSERT INTO blocks (msg_id, conv_id, idx, type, tool_name, text, char_count, content_hash, raw_json, attachment_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id, row["id"], block["idx"], block["type"], block.get("tool_name"), block["text"],
                len(block["text"]), b_hash, loader._json(block.get("raw")), attachment_id,
            ),
        )
        block_chars += len(block["text"])

    conn.execute(
        "UPDATE messages SET role = ?, content_hash = ?, char_count = ?, "
        "raw_json = json_set(COALESCE(raw_json, '{}'), '$.canvas', json('true')) WHERE id = ?",
        (message["role"], hashes["message_hashes"][0], block_chars, msg_id),
    )
    conn.execute(
        """
        UPDATE conversations
        SET title = ?, native_id = ?, uid = ?, message_count = 1, block_count = ?, char_count = ?,
            head_hash = ?, content_hash = ?
        WHERE id = ?
        """,
        (
            cir["title"], cir["native_id"], _uid(p, hashes["content_hash"]),
            len(message["blocks"]), block_chars, hashes["head_hash"], hashes["content_hash"], row["id"],
        ),
    )

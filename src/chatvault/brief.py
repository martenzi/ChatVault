from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from chatvault import export as export_mod
from chatvault import stash as stash_mod
from chatvault.export import slugify

# Second Brain v0, Part B (docs/40_SPEC_second_brain_v0.md §3). Deliberately
# reuses export.render_profile(profile='lean') for the body — no second
# renderer. The header (why/source/vault reference) is the only new part.


class BriefError(ValueError):
    pass


def _header(conv: dict, why: str) -> str:
    heading = why.strip().splitlines()[0] if why.strip() else (conv.get("title") or conv["uid"])
    date = (conv.get("started_at") or "")[:10] or "undated"

    source_parts = [conv["platform"], date]
    if conv.get("project"):
        source_parts.append(f"project: {conv['project']}")
    source_parts.append(f"{conv.get('message_count') or 0} messages")

    return (
        f"# Context: {heading}\n\n"
        f"**Why this was saved:** {why.strip()}\n\n"
        f"**Source:** {' · '.join(source_parts)}\n"
        f"**Vault reference:** {conv['uid']}\n"
    )


def render_one(conn: sqlite3.Connection, conv_id: int) -> str:
    """A conversation must be stashed (must carry a why) before it can be
    briefed — spec §3's whole point is turning a *saved* conversation into a
    context pack, and the header format is meaningless without a why."""
    why = stash_mod.get_why(conn, conv_id)
    if why is None:
        raise BriefError(
            f"conversation {conv_id} isn't stashed yet — run `chatvault stash add` first "
            "(brief needs a why; there isn't one to show)"
        )
    row = conn.execute("SELECT * FROM v_conversations WHERE id = ?", (conv_id,)).fetchone()
    if row is None:
        raise BriefError(f"no conversation with id {conv_id}")

    conv = dict(row)
    header = _header(conv, why)
    body = export_mod.render_profile(conn, row, profile="lean")
    today = datetime.now(timezone.utc).date().isoformat()
    return (
        f"{header}\n---\n\n"
        f"{body.strip()}\n\n"
        f"---\n*Context pack generated from ChatVault {today}. Reply as if continuing this work.*\n"
    )


def render_many(conn: sqlite3.Connection, conv_ids: list[int]) -> str:
    """Multiple picks combine into one pack (spec §3: `--pick 1,4` 'combine
    several') — each conversation keeps its own full header, concatenated
    into a single document so it stays one thing to paste or attach."""
    return "\n\n".join(render_one(conn, cid) for cid in conv_ids)


def filename_for(conn: sqlite3.Connection, conv_ids: list[int]) -> str:
    first = conn.execute("SELECT * FROM v_conversations WHERE id = ?", (conv_ids[0],)).fetchone()
    date = (first["started_at"] or "")[:10] or "undated"
    uid_short = first["uid"].split(":")[-1][:12]
    slug = slugify(first["title"])
    suffix = f"+{len(conv_ids) - 1}-more" if len(conv_ids) > 1 else ""
    return f"brief--{date}--{first['platform']}--{slug}{suffix}--{uid_short}.md"

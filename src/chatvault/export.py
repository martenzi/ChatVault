from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str | None) -> str:
    if not text:
        return "untitled"
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:60] or "untitled"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(disabled_extensions=(".j2",), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _fetch_conversation(conn: sqlite3.Connection, conv_row: sqlite3.Row) -> dict:
    conv_id = conv_row["id"]
    messages = []
    for m in conn.execute("SELECT * FROM v_messages WHERE conv_id = ? ORDER BY idx", (conv_id,)).fetchall():
        blocks = [
            dict(b)
            for b in conn.execute(
                "SELECT * FROM v_blocks WHERE msg_id = ? ORDER BY idx", (m["id"],)
            ).fetchall()
        ]
        md = dict(m)
        md["blocks"] = blocks
        messages.append(md)

    tags = [
        row[0]
        for row in conn.execute(
            """
            SELECT t.name FROM tags t
            JOIN tag_links tl ON tl.tag_id = t.id
            WHERE tl.target_type = 'conversation' AND tl.target_id = ?
            ORDER BY t.name
            """,
            (conv_id,),
        ).fetchall()
    ]

    conv = dict(conv_row)
    conv["messages"] = messages
    conv["tags"] = tags
    conv["uid_short"] = conv["uid"].split(":")[-1][:12]
    return conv


def get_conversations(
    conn: sqlite3.Connection,
    uid: str | None = None,
    where: str | None = None,
    view: str | None = None,
) -> list[sqlite3.Row]:
    """Note: `where` and saved-view SQL are trusted local input (this is a
    single-user CLI operating on the user's own database), not attacker
    input — never wire this to network input."""
    if uid:
        return conn.execute("SELECT * FROM v_conversations WHERE uid = ?", (uid,)).fetchall()

    if view:
        saved = conn.execute("SELECT sql FROM saved_views WHERE name = ?", (view,)).fetchone()
        if saved is None:
            raise ValueError(f"no saved view named {view!r}")
        ids = [row[0] for row in conn.execute(saved["sql"]).fetchall()]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return conn.execute(
            f"SELECT * FROM v_conversations WHERE id IN ({placeholders}) ORDER BY started_at", ids
        ).fetchall()

    predicate = where or "1=1"
    return conn.execute(f"SELECT * FROM v_conversations WHERE {predicate} ORDER BY started_at").fetchall()  # noqa: S608


def filename_for(conv: dict, template: str) -> str:
    date = (conv.get("started_at") or "")[:10] or "undated"
    return f"{date}--{conv['platform']}--{slugify(conv.get('title'))}--{conv['uid_short']}.{('json' if template == 'json' else 'md')}"


def render(conn: sqlite3.Connection, conv_row: sqlite3.Row, template: str) -> str:
    env = _env()
    tmpl = env.get_template(f"{template}.md.j2")
    conv = _fetch_conversation(conn, conv_row)
    return tmpl.render(conv=conv)


def render_digest(conn: sqlite3.Connection, conv_rows: list[sqlite3.Row]) -> str:
    env = _env()
    tmpl = env.get_template("digest.md.j2")
    convs = [_fetch_conversation(conn, r) for r in conv_rows]
    return tmpl.render(conversations=convs)


def get_conversations_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """Fetch conversations by id, preserving the given order (row order from
    a `find` pick, not insertion/id order)."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM v_conversations WHERE id IN ({placeholders})", ids  # noqa: S608
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


# ─────────────── export --tree (v1.5 §4) ───────────────
# Folders on demand: any tag tree becomes a real nested folder structure of
# markdown, generated fresh each time — a view, not a new source of truth
# (spec's own framing: "delete the folder, change the tags, regenerate").


def get_conversations_for_tree(conn: sqlite3.Connection, tree: str) -> list[sqlite3.Row]:
    """Every (conversation, matching tag) pair for a prefix/glob like
    'tech/**' — one row per match, not one per conversation: a conversation
    carrying two tags under the same tree (e.g. 'tech/macos/npm' and
    'tech/containers/docker') is paired with each separately, so it lands in
    both resulting subfolders when the tree is laid out on disk — the same
    "one conversation, many places" property the whole taxonomy is built on,
    just expressed within a single export run instead of across separate
    ones. Each row carries an extra `matched_tag` column alongside the
    normal conversation columns."""
    from chatvault import tags as tags_mod  # deferred: tags.py imports slugify from this module

    frag, params = tags_mod.tag_match("t.name", tree)
    return conn.execute(
        f"""
        SELECT c.*, t.name AS matched_tag
        FROM v_conversations c
        JOIN tag_links tl ON tl.target_type = 'conversation' AND tl.target_id = c.id
        JOIN tags t ON t.id = tl.tag_id
        WHERE {frag}
        ORDER BY t.name, c.started_at
        """,  # noqa: S608
        params,
    ).fetchall()


def tree_relative_dir(tree: str, matched_tag: str) -> Path:
    """The folder a matched conversation lands in, relative to --out: the
    portion of `matched_tag` below the tree's own root. 'tech/**' matching
    'tech/macos/npm' -> 'macos/npm' (a conversation tagged with the bare
    root itself, just 'tech', lands directly in --out — Path('.'))."""
    root = tree
    for suffix in ("/**", "/*"):
        if tree.endswith(suffix):
            root = tree[: -len(suffix)]
            break
    if matched_tag == root:
        return Path(".")
    rel = matched_tag[len(root):].lstrip("/")
    return Path(rel) if rel else Path(".")


# Spec v1.3 §4 — export profiles. `read` (renamed from the old "default"
# template) always collapses tool calls to one line, regardless of size.
# `lean` keeps short tool bodies but collapses anything over its budget.
# `full` never collapses. `--max-tool-chars` overrides whichever budget the
# profile picked; `--blocks` is a separate axis that restricts block types
# outright and wins over the profile's own thinking-block default.
#
# These are not a size ladder (spec §4 note): `read` optimises for human eyes
# (collapse every tool call), `lean` for an AI's context window (keep short
# tool results verbatim, they're often the signal). `lean` can legitimately
# come out larger than `read` on a conversation with many small tool calls —
# only `full >= read` and `full >= lean` are guaranteed. Text/code blocks are
# never touched by any profile, only thinking (dropped) and tool_use/
# tool_result (collapsed) are — this is what keeps dialogue integrity: lean
# reproduces every user/assistant prose block verbatim and complete.
PROFILES = {"read", "lean", "full"}
_PROFILE_TOOL_BUDGET = {"read": 0, "lean": 500, "full": None}


def _resolve_tool_budget(profile: str, max_tool_chars: int | None) -> tuple[int | None, str]:
    if max_tool_chars is not None:
        return max_tool_chars, "bracket"
    budget = _PROFILE_TOOL_BUDGET[profile]
    return budget, ("arrow" if profile == "read" else "bracket")


def _maybe_collapse_tool_block(block: dict, budget: int | None, style: str) -> dict:
    if budget is None:
        return block
    text = block["text"]
    if style != "arrow" and len(text) <= budget:
        return block

    name = block.get("tool_name") or "unknown"
    collapsed = dict(block)
    if style == "arrow":
        collapsed["text"] = (
            f"→ ran {name}(...)"
            if block["type"] == "tool_use"
            else f"→ {name} result ({len(text):,} chars, collapsed)"
        )
    else:
        collapsed["text"] = f"[tool: {name}, {len(text):,} chars omitted]"
    return collapsed


def render_profile(
    conn: sqlite3.Connection,
    conv_row: sqlite3.Row,
    profile: str = "read",
    blocks: str | None = None,
    max_tool_chars: int | None = None,
) -> str:
    """Render via a named profile (read/lean/full) instead of a raw template.
    `blocks` restricts output to a set of block types ('text,code'), overriding
    the profile's default thinking-block handling. `max_tool_chars` overrides
    the profile's tool-call collapsing budget."""
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {sorted(PROFILES)}, got {profile!r}")

    budget, style = _resolve_tool_budget(profile, max_tool_chars)
    allowed_types = {t.strip() for t in blocks.split(",") if t.strip()} if blocks else None

    conv = _fetch_conversation(conn, conv_row)
    for msg in conv["messages"]:
        filtered = []
        for block in msg["blocks"]:
            btype = block["type"]
            if allowed_types is not None:
                if btype not in allowed_types:
                    continue
            elif profile != "full" and btype == "thinking":
                continue
            if btype in ("tool_use", "tool_result"):
                block = _maybe_collapse_tool_block(block, budget, style)
            filtered.append(block)
        msg["blocks"] = filtered

    env = _env()
    tmpl = env.get_template("profile.md.j2")
    return tmpl.render(conv=conv, profile=profile)

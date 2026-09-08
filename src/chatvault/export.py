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
# outright and wins over the profile's own block-type default.
#
# These are not a size ladder (spec §4 note): `read` optimises for human eyes
# (collapse every tool call), `lean` for an AI's context window (keep short
# tool results verbatim, they're often the signal). `lean` can legitimately
# come out larger than `read` on a conversation with many small tool calls —
# only `full >= read` and `full >= lean` are guaranteed. Text/code blocks are
# never touched by any profile; thinking is dropped only below `lean` (it's
# part of `lean`'s "conversational process" tier per
# docs/50_SPEC_ingestion_fidelity_v1.md §1 — a corrected earlier draft had it
# `full`-only) and tool_use/tool_result are always collapsed per the active
# budget — this is what keeps dialogue integrity: every profile reproduces
# every user/assistant prose block verbatim and complete.
PROFILES = {"read", "lean", "full"}
_PROFILE_TOOL_BUDGET = {"read": 0, "lean": 500, "full": None}

# docs/50_SPEC_ingestion_fidelity_v1.md §2.2 — what each profile SHOWS, keyed
# on blocks.type — the only tier axis. Mirrors the standalone converter's
# three modes (CHAT/TOOLS/RAW), inverted per Decision D3: nothing is dropped
# from the DB, only from this render.
#
# Crucially, membership is NOT keyed on messages.role. Providers disagree
# about which role they file reasoning under (spec §1.1), so a role-keyed
# policy would hide correctly-classified `thinking` blocks for some providers
# and not others. Block type is the provider-independent fact. A message
# renders if at least one of its blocks survives this filter; role is purely
# presentational (it labels the heading).
#
# `tool_use`/`tool_result` are listed here for documentation fidelity to the
# spec's tier table, but at runtime their visibility is governed entirely by
# the pre-existing collapse budget (`_maybe_collapse_tool_block`), not by
# this matrix — that mechanism already makes them profile-appropriate
# (a one-line stub under `read`, budgeted under `lean`, verbatim under
# `full`) and predates this fix. An explicit `--blocks` override still wins
# outright and is applied to tool blocks too.
_PROFILE_BLOCK_TYPES: dict[str, set[str] | None] = {
    # CHAT tier — dialogue only.
    "read": {"text", "code", "attachment", "image"},
    # TOOLS tier — the conversational process: dialogue + what the model was
    # doing + how it failed. NOT system prompts, NOT injected rules files,
    # NOT internal system chatter (those are noisy and repetitive; they're RAW).
    "lean": {
        "text", "code", "attachment", "image",
        "thinking", "tool_use", "tool_result", "error", "refusal", "audio",
    },
    # RAW tier — everything, for debugging and model-performance evaluation.
    "full": None,  # None = no filtering whatsoever
}


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
    the profile's default block-type visibility (see `_PROFILE_BLOCK_TYPES`).
    `max_tool_chars` overrides the profile's tool-call collapsing budget."""
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {sorted(PROFILES)}, got {profile!r}")

    budget, style = _resolve_tool_budget(profile, max_tool_chars)
    # --blocks overrides the profile's default block-type set outright; same
    # axis as _PROFILE_BLOCK_TYPES, just made explicit rather than defaulted.
    explicit_override = blocks is not None
    allowed_types = (
        {t.strip() for t in blocks.split(",") if t.strip()}
        if explicit_override
        else _PROFILE_BLOCK_TYPES[profile]
    )

    conv = _fetch_conversation(conn, conv_row)
    for msg in conv["messages"]:
        filtered = []
        for block in msg["blocks"]:
            btype = block["type"]
            is_tool_block = btype in ("tool_use", "tool_result")
            # tool_use/tool_result visibility is governed entirely by the
            # existing collapse budget below, not by the block-type matrix —
            # that mechanism already makes them profile-appropriate (fully
            # collapsed to a one-line stub under `read`, budgeted under
            # `lean`, verbatim under `full`) and predates this fix; §2.2
            # keeps it "unchanged, orthogonal to visibility". An explicit
            # --blocks override still wins outright, since it's the more
            # specific ask.
            if not (is_tool_block and not explicit_override):
                if allowed_types is not None and btype not in allowed_types:
                    continue
            if is_tool_block:
                block = _maybe_collapse_tool_block(block, budget, style)
            filtered.append(block)
        # A message renders only if at least one of its blocks survived the
        # filter — spec §2.2. `messages.role` is never itself a filter; it
        # only labels the heading in the template.
        msg["blocks"] = filtered
    conv["messages"] = [msg for msg in conv["messages"] if msg["blocks"]]

    env = _env()
    tmpl = env.get_template("profile.md.j2")
    return tmpl.render(conv=conv, profile=profile)

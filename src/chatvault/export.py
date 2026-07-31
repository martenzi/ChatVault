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

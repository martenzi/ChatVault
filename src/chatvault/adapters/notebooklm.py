from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters._html_lite import html_to_text
from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "notebooklm"
name = "notebooklm"

# Takeout "NotebookLM/<notebook>/Chat History/Chat Session - <uuid>.html".
# Enumerated over all 18 real files (7 distinct sessions; each appears 2-3x
# byte-identically across Takeout generations/accounts): the file is plain
# text, not a page — alternating `MODEL: <html fragment>` / `USER: <html
# fragment>` blocks, NEWEST PAIR FIRST (the 13-turn session's auto-generated
# "Discuss what these sources say about ..." prompt, its first turn, is
# last). No timestamps, titles or model names anywhere in the file; the only
# date information is the notebook's sibling `<notebook> metadata.json`
# (createTime/lastViewed), kept in conversation raw, not used as a message
# time. Only Chat Session files are in scope — Sources/Artifacts/Notes are
# documents, not conversations.

_SPLIT_RE = re.compile(r"(?m)^(?=(?:USER|MODEL): )")
_FIRST_RE = re.compile(r"\s*(?:USER|MODEL): <")


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".html" or not path.name.startswith("Chat Session - "):
        return False
    if path.parent.name != "Chat History":
        return False
    try:
        with path.open("rb") as fh:
            head = fh.read(64).decode("utf-8", errors="replace")
    except OSError:
        return False
    return bool(_FIRST_RE.match(head))


def _notebook_metadata(notebook_dir: Path) -> dict | None:
    meta = notebook_dir / f"{notebook_dir.name} metadata.json"
    try:
        return orjson.loads(meta.read_bytes())
    except (OSError, ValueError):
        return None


def parse(path: Path) -> Iterator[dict]:
    text = path.read_text(encoding="utf-8")
    parts = [p for p in _SPLIT_RE.split(text) if p.strip()]
    turns = []
    for p in reversed(parts):  # file is newest-first; store chronologically
        role_tag, _, body = p.partition(": ")
        turns.append(("user" if role_tag == "USER" else "assistant", body.strip()))
    messages = []
    for i, (role, body) in enumerate(turns):
        rendered = html_to_text(body)
        messages.append(make_message(i, role, None, None, [make_block(0, "text", rendered, raw={"html": body})], raw={}))
    session_id = path.stem[len("Chat Session - "):]
    notebook_dir = path.parent.parent
    meta = _notebook_metadata(notebook_dir)
    first_user = next((m["blocks"][0]["text"] for m in messages if m["role"] == "user"), "")
    yield make_conversation(
        platform=platform,
        native_id=session_id,
        title=(first_user[:200] or notebook_dir.name),
        project=notebook_dir.name,
        started_at=None,
        ended_at=None,
        source_path=str(path),
        raw={"notebook": notebook_dir.name, "notebook_metadata": (meta or {}).get("metadata"), "turn_order_in_file": "newest_first"},
        messages=messages,
    )

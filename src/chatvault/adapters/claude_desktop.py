from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_attachment, make_block, make_conversation, make_message

platform = "claude_desktop"
name = "claude_desktop"

_SENDER_TO_ROLE = {"human": "user", "assistant": "assistant", "system": "system"}


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    return b'"chat_messages"' in prefix


def sniff_projects(path: Path) -> bool:
    """Claude's separate `projects.json` export — a list of {name, docs, ...},
    structurally unrelated to the conversations file. Routed through a
    different ingest path (chatvault.loader.load_project), not the
    conversation pipeline sniff() above feeds."""
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    return b'"prompt_template"' in prefix and b'"docs"' in prefix and b'"chat_messages"' not in prefix


def _blocks_from_content(content, fallback_text: str) -> list[dict]:
    if not content:
        if fallback_text:
            return [make_block(0, "text", fallback_text, raw={"text": fallback_text})]
        return []

    blocks: list[dict] = []
    for idx, item in enumerate(content):
        if not isinstance(item, dict):
            blocks.append(make_block(idx, "unknown", "", raw={"value": item}))
            continue
        item_type = item.get("type")
        if item_type == "text":
            blocks.append(make_block(idx, "text", item.get("text", ""), raw=item))
        elif item_type == "thinking":
            blocks.append(make_block(idx, "thinking", item.get("thinking", ""), raw=item))
        elif item_type == "tool_use":
            blocks.append(
                make_block(
                    idx,
                    "tool_use",
                    json.dumps(item.get("input", {}), default=str),
                    raw=item,
                    tool_name=item.get("name"),
                )
            )
        elif item_type == "tool_result":
            inner = item.get("content")
            text = inner if isinstance(inner, str) else json.dumps(inner, default=str)
            blocks.append(make_block(idx, "tool_result", text, raw=item))
        elif item_type in ("image",):
            blocks.append(make_block(idx, "image", "", raw=item))
        else:
            blocks.append(make_block(idx, "unknown", "", raw=item))
    return blocks


def _ext(filename: str | None) -> str | None:
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return None


def _attachment_blocks(cm: dict, start_idx: int) -> list[dict]:
    blocks: list[dict] = []
    seen_names: set[str] = set()
    for att in cm.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        seen_names.add(att.get("file_name"))
        text = att.get("extracted_content") or ""
        payload = make_attachment(
            filename=att.get("file_name"),
            ext=_ext(att.get("file_name")) or (att.get("file_type") or None),
            bytes_=att.get("file_size"),
            content=text or None,
            availability="text" if text else "missing",
            role="upload",
            raw=att,
        )
        blocks.append(make_block(start_idx + len(blocks), "attachment", text, raw=att, attachment=payload))
    for f in cm.get("files") or []:
        if not isinstance(f, dict):
            continue
        # "files" duplicates "attachments" by file_name when the latter has extracted
        # text; only add it when it's the only record of that file.
        if f.get("file_name") in seen_names:
            continue
        payload = make_attachment(
            filename=f.get("file_name"),
            ext=_ext(f.get("file_name")),
            availability="missing",
            role="upload",
            raw=f,
        )
        blocks.append(make_block(start_idx + len(blocks), "attachment", "", raw=f, attachment=payload))
    return blocks


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    conversations = data if isinstance(data, list) else [data]

    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        chat_messages = conv.get("chat_messages") or []
        messages = []
        for idx, cm in enumerate(chat_messages):
            role = _SENDER_TO_ROLE.get(cm.get("sender"), cm.get("sender") or "unknown")
            blocks = _blocks_from_content(cm.get("content"), cm.get("text", ""))
            blocks += _attachment_blocks(cm, len(blocks))
            messages.append(make_message(idx, role, None, cm.get("created_at"), blocks, raw=cm))

        yield make_conversation(
            platform=platform,
            native_id=conv.get("uuid"),
            title=conv.get("name"),
            project=None,
            started_at=conv.get("created_at"),
            ended_at=conv.get("updated_at"),
            source_path=str(path),
            raw={k: v for k, v in conv.items() if k != "chat_messages"},
            messages=messages,
        )


def parse_projects(path: Path) -> Iterator[dict]:
    """Claude Projects export: a list of {uuid, name, description,
    prompt_template, docs: [{uuid, filename, content, created_at}], ...}.
    No conversation reference anywhere in this shape (spec §B.2) — projects
    are ingested as standalone records; conv->project mapping is a separate,
    deliberately unhurried decision."""
    data = orjson.loads(path.read_bytes())
    projects = data if isinstance(data, list) else [data]

    for proj in projects:
        if not isinstance(proj, dict):
            continue

        docs = []
        for d in proj.get("docs") or []:
            if not isinstance(d, dict):
                continue
            content = d.get("content") or ""
            docs.append(
                make_attachment(
                    filename=d.get("filename"),
                    ext=_ext(d.get("filename")),
                    bytes_=len(content.encode("utf-8")) if content else None,
                    content=content or None,
                    availability="text" if content else "missing",
                    role="project_knowledge",
                    raw=d,
                )
            )

        yield {
            "platform": platform,
            "native_id": proj.get("uuid"),
            "name": proj.get("name") or "Untitled project",
            "description": proj.get("description"),
            "instructions": proj.get("prompt_template"),
            "created_at": proj.get("created_at"),
            "raw": {k: v for k, v in proj.items() if k != "docs"},
            "docs": docs,
        }

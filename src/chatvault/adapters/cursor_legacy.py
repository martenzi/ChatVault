from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "cursor"
name = "cursor_legacy"

# Source: tools/corpus_full/export_cursor_full.py. Each JSONL is one Cursor
# composer: line 0 is meta (extraction="cursor-composer-full-v1"), following
# lines are one record per bubble (role 1=user, 2=assistant; anything else
# comes through as "type_N" or "missing_bubble" when the bubble's KV row was
# gone). A single assistant bubble can carry text AND a tool call
# (toolFormerData) at once — both become separate blocks on one message.

_ROLE_TO_MESSAGE_ROLE = {"user": "user", "assistant": "assistant"}


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".jsonl":
        return False
    try:
        with path.open("rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = orjson.loads(line)
                return (
                    isinstance(obj, dict)
                    and obj.get("type") == "meta"
                    and obj.get("extraction") == "cursor-composer-full-v1"
                )
    except (orjson.JSONDecodeError, OSError):
        return False
    return False


def _iso_z(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.endswith("Z") else value + "Z"


def _clean(value: str | None, placeholder: str) -> str | None:
    return value if value and value != placeholder else None


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str) if value is not None else ""


def parse(path: Path) -> Iterator[dict]:
    meta: dict = {}
    records: list[dict] = []

    with path.open("rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "meta":
                meta = obj
            else:
                records.append(obj)

    if not records:
        return

    messages = []
    for idx, rec in enumerate(records):
        raw_role = rec.get("role")
        msg_role = _ROLE_TO_MESSAGE_ROLE.get(raw_role, "system")

        blocks = []
        text = rec.get("text") or rec.get("content") or ""
        if text:
            blocks.append(make_block(0, "text", text, raw=rec))

        tool = rec.get("toolFormerData")
        if isinstance(tool, dict):
            tool_name = tool.get("name")
            args = tool.get("params") or tool.get("rawArgs")
            blocks.append(make_block(0, "tool_use", _stringify(args), raw=tool, tool_name=tool_name))
            if tool.get("result"):
                blocks.append(make_block(0, "tool_result", _stringify(tool.get("result")), raw=tool, tool_name=tool_name))

        if not blocks:
            blocks.append(make_block(0, "unknown", "", raw=rec))

        for i, b in enumerate(blocks):
            b["idx"] = i

        messages.append(make_message(idx, msg_role, rec.get("model"), _iso_z(rec.get("iso")), blocks, raw=rec))

    yield make_conversation(
        platform=platform,
        native_id=meta.get("composer_id"),
        title=_clean(meta.get("title"), "untitled"),
        project=_clean(meta.get("project"), "unassigned"),
        started_at=_iso_z(meta.get("created")),
        ended_at=_iso_z(meta.get("updated")),
        source_path=str(path),
        raw=meta,
        messages=messages,
    )

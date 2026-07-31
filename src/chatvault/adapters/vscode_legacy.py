from __future__ import annotations

from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "vscode"
name = "vscode_legacy"

# Source: tools/corpus_full/export_vscode_full.py. One JSONL per VS Code
# Copilot chatSession: line 0 is meta (extraction="vscode-chatsessions-full-v1"),
# following lines are one record per request/response event. Role vocabulary
# observed in the real corpus: user, assistant (kind=text or a progress/
# control kind like confirmation/command/inlineReference), tool (kind in
# toolInvocationSerialized/prepareToolInvocation/textEditGroup), thinking,
# and result (the whole per-request result blob, no text of its own).


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
                    and obj.get("extraction") == "vscode-chatsessions-full-v1"
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


def _tool_name(part: dict) -> str | None:
    return part.get("toolId") or part.get("toolCallId")


def _tool_fallback_text(part: dict) -> str:
    inv = part.get("invocationMessage")
    if isinstance(inv, dict):
        return inv.get("value") or ""
    return ""


def _classify(rec: dict) -> tuple[str, str]:
    """Returns (message_role, block_type) for one event record."""
    role = rec.get("role")
    if role == "user":
        return "user", "text"
    if role == "thinking":
        return "assistant", "thinking"
    if role == "tool":
        return "tool", "tool_use"
    if role == "result":
        return "system", "unknown"
    # role == "assistant" (kind=="text" or any progress/control kind)
    return "assistant", "text" if rec.get("text") else "unknown"


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
        msg_role, block_type = _classify(rec)
        text = rec.get("text") or rec.get("content") or ""
        tool_name = None
        if rec.get("role") == "tool":
            part = rec.get("part") or {}
            tool_name = _tool_name(part)
            if not text:
                text = _tool_fallback_text(part)

        block = make_block(0, block_type, text, raw=rec, tool_name=tool_name)
        messages.append(make_message(idx, msg_role, rec.get("model"), _iso_z(rec.get("iso")), [block], raw=rec))

    yield make_conversation(
        platform=platform,
        native_id=meta.get("session_id"),
        title=_clean(meta.get("title"), "untitled"),
        project=_clean(meta.get("project"), "unknown"),
        started_at=_iso_z(meta.get("created")),
        ended_at=_iso_z(meta.get("updated")),
        source_path=str(path),
        raw=meta,
        messages=messages,
    )

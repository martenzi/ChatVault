from __future__ import annotations

from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "windsurf"
name = "windsurf_legacy"

# Bucket the reverse-engineered Cascade role vocabulary into CIR's role/block
# shape. Source: tools/cascade_decrypt/cascade_export_full.py TYPE_ROLE map —
# this is the *_full extraction (protobuf-wire-full-v1), not the thin
# dialogue-only windsurf_legacy/ export (protobuf-wire-v1), which lacks tool
# calls, per-message model, and most of these role types entirely.
_ROLE_TO_MESSAGE_ROLE = {
    "user": "user",
    "assistant": "assistant",
    "tool_call": "tool",
    "tool_result": "tool",
    "tool_error": "tool",
    "planner": "system",
    "context": "system",
    "memory": "system",
    "system": "system",
}

_ROLE_TO_BLOCK_TYPE = {
    "user": "text",
    "assistant": "text",
    "tool_call": "tool_use",
    "tool_result": "tool_result",
    "tool_error": "error",
    "planner": "thinking",
    "context": "attachment",
    "memory": "system",
    "system": "system",
}


def sniff(path: Path) -> bool:
    """Every meta line (thin or full extraction) self-declares its origin via
    "source": "windsurf_legacy_cascade" (see EXTRACTION_METHOD.md). Trust that
    explicit tag first; fall back to the structural shape in case a future
    export drops or renames the source string."""
    if path.suffix.lower() != ".jsonl":
        return False
    try:
        with path.open("rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = orjson.loads(line)
                if not isinstance(obj, dict) or obj.get("type") != "meta":
                    return False
                if obj.get("source") == "windsurf_legacy_cascade":
                    return True
                return "conversation_id" in obj and "cascade_file" in obj
    except (orjson.JSONDecodeError, OSError):
        return False
    return False


def _iso_z(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.endswith("Z") else value + "Z"


def _block_text(rec: dict, role: str) -> str:
    text = rec.get("text") or rec.get("content")
    if text:
        return text
    if role == "tool_call" and rec.get("tool_args"):
        return rec["tool_args"]
    payloads = rec.get("payloads")
    if payloads:
        return payloads[0]
    return ""


def _title_from_first_user_message(records: list[dict]) -> str | None:
    for rec in records:
        if rec.get("role") == "user":
            text = rec.get("text") or rec.get("content")
            if text:
                return text.strip().splitlines()[0][:120]
    return None


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
        raw_role = rec.get("role") or f"type_{rec.get('msg_type')}"
        msg_role = _ROLE_TO_MESSAGE_ROLE.get(raw_role, "system")
        block_type = _ROLE_TO_BLOCK_TYPE.get(raw_role, "unknown")

        text = _block_text(rec, raw_role)
        tool_name = rec.get("tool_name") if raw_role in ("tool_call", "tool_result", "tool_error") else None
        block = make_block(0, block_type, text, raw=rec, tool_name=tool_name)

        created_at = _iso_z(rec.get("iso"))
        messages.append(make_message(idx, msg_role, rec.get("model"), created_at, [block], raw=rec))

    conversation_id = meta.get("conversation_id")
    project_guess = meta.get("project_path_guess")
    project = Path(unquote(project_guess)).name if project_guess else None
    title = meta.get("title") or _title_from_first_user_message(records)

    yield make_conversation(
        platform=platform,
        native_id=conversation_id,
        title=title,
        project=project,
        started_at=_iso_z(meta.get("started")),
        ended_at=_iso_z(meta.get("ended")),
        source_path=str(path),
        raw=meta,
        messages=messages,
    )

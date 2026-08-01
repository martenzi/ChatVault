from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "openclaw"
name = "openclaw"

# OpenClaw session files are named "<uuid>.jsonl", but the app also writes
# pre-reset backups as "<uuid>.jsonl.reset" or "<uuid>.jsonl.reset.<ISO8601Z
# timestamp>" (and, at least once in the wild, "<uuid>.jsonl.reset2"). Those
# compound extensions mean Path.suffix is ".reset"/".403Z"/"" — not ".jsonl" —
# so a strict suffix check misses them, and ingest.py's KNOWN_EXTENSIONS gate
# (json/jsonl only) never even offers them as candidates. They are real
# session content (identical shape to the live file), not renamed originals,
# so we match them with a glob-style pattern on the filename instead of
# requiring the source folder to rename anything.
_JSONL_NAME_RE = re.compile(r"\.jsonl(\.|$)", re.IGNORECASE)

# CIR role is closed: user | assistant | system | tool.
_ROLE_MAP = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "toolResult": "tool",
}

# message.content[].type -> CIR block type.
_CONTENT_BLOCK_MAP = {
    "text": "text",
    "thinking": "thinking",
    "image": "image",
    "attachment": "attachment",
}


def is_openclaw_jsonl_name(path: Path) -> bool:
    return bool(_JSONL_NAME_RE.search(path.name))


def sniff(path: Path) -> bool:
    if not is_openclaw_jsonl_name(path):
        return False
    try:
        with path.open("rb") as fh:
            for i, line in enumerate(fh):
                if i >= 5:
                    break
                line = line.strip()
                if not line:
                    continue
                obj = orjson.loads(line)
                if not isinstance(obj, dict):
                    return False
                # The session header ({"type":"session","id":...,"timestamp":...})
                # is the strongest, most stable signature and (barring a
                # truncated reset backup) always appears first.
                if obj.get("type") == "session" and "id" in obj and "timestamp" in obj:
                    return True
                # Defensive fallback for a reset backup truncated before the
                # header line survived: OpenClaw's own vocabulary of top-level
                # event types, none of which collide with claude_code
                # (requires sessionId+uuid+cwd+version together) or with
                # generic_jsonl's role/content-key heuristic (these lines
                # don't carry a top-level "role" or "content" key at all —
                # role lives nested under "message").
                if (
                    obj.get("type")
                    in ("message", "model_change", "thinking_level_change", "custom", "compaction")
                    and "timestamp" in obj
                    and "role" not in obj
                    and "content" not in obj
                ):
                    return True
    except (orjson.JSONDecodeError, OSError):
        return False
    return False


def _stringify_tool_result_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item, default=str))
        return "\n".join(parts)
    return json.dumps(content, default=str) if content is not None else ""


def _blocks_from_content(content) -> list[dict]:
    blocks: list[dict] = []
    if isinstance(content, str):
        blocks.append(make_block(0, "text", content, raw={"text": content}))
        return blocks
    if not isinstance(content, list):
        if content is not None:
            blocks.append(make_block(0, "unknown", "", raw={"value": content}))
        return blocks

    for idx, item in enumerate(content):
        if not isinstance(item, dict):
            blocks.append(make_block(idx, "unknown", "", raw={"value": item}))
            continue
        item_type = item.get("type")
        if item_type == "toolCall":
            blocks.append(
                make_block(
                    idx,
                    "tool_use",
                    json.dumps(item.get("arguments", {}), default=str),
                    raw=item,
                    tool_name=item.get("name"),
                )
            )
        elif item_type in _CONTENT_BLOCK_MAP:
            block_type = _CONTENT_BLOCK_MAP[item_type]
            text = item.get("thinking", "") if item_type == "thinking" else item.get("text", "")
            blocks.append(make_block(idx, block_type, text, raw=item))
        else:
            blocks.append(make_block(idx, "unknown", "", raw=item))
    return blocks


def _session_title(message_events: list[dict]) -> str | None:
    for ev in message_events:
        msg = ev.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        text = content if isinstance(content, str) else None
        if text is None and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    break
        if text:
            return text.strip().splitlines()[0][:120]
    return None


def parse(path: Path) -> Iterator[dict]:
    events: list[dict] = []
    with path.open("rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                events.append(ev)

    if not events:
        return

    header = next((e for e in events if e.get("type") == "session"), None)
    native_id = (header or {}).get("id")
    if not native_id:
        # Reset backups occasionally lose the header line; fall back to the
        # stable part of the filename (the uuid before ".jsonl...").
        native_id = path.name.split(".jsonl")[0] or None
    cwd = (header or {}).get("cwd")
    project = Path(cwd).name if cwd else None

    current_model: str | None = None
    messages: list[dict] = []
    timestamps: list[str] = []
    meta_events: list[dict] = []  # non-message bookkeeping, kept for fidelity
    idx = 0

    for ev in events:
        ts = ev.get("timestamp")
        if isinstance(ts, str):
            timestamps.append(ts)
        ev_type = ev.get("type")

        if ev_type == "model_change":
            provider = ev.get("provider")
            model_id = ev.get("modelId")
            if provider or model_id:
                current_model = f"{provider}/{model_id}" if provider and model_id else (provider or model_id)
            meta_events.append(ev)
            continue

        if ev_type == "custom" and ev.get("customType") == "model-snapshot":
            data = ev.get("data") or {}
            provider = data.get("provider")
            model_id = data.get("modelId")
            if provider or model_id:
                current_model = f"{provider}/{model_id}" if provider and model_id else (provider or model_id)
            meta_events.append(ev)
            continue

        if ev_type == "session":
            # Captured via native_id/project/raw above; not a conversational turn.
            continue

        if ev_type in ("thinking_level_change", "custom"):
            # "custom" with customType == "model-snapshot" was already
            # handled (and skipped past) above; any other custom/
            # thinking_level_change event is pure bookkeeping.
            meta_events.append(ev)
            continue

        if ev_type == "compaction":
            block = make_block(0, "system", ev.get("summary", ""), raw=ev)
            messages.append(make_message(idx, "system", None, ev.get("timestamp"), [block], raw=ev))
            idx += 1
            continue

        if ev_type == "message":
            msg = ev.get("message") or {}
            raw_role = msg.get("role")
            role = _ROLE_MAP.get(raw_role, "system")

            if raw_role == "toolResult":
                text = _stringify_tool_result_content(msg.get("content"))
                block = make_block(
                    0, "tool_result", text, raw=msg, tool_name=msg.get("toolName")
                )
                blocks = [block]
                model = None
            else:
                blocks = _blocks_from_content(msg.get("content"))
                model = current_model if role == "assistant" else None

            messages.append(make_message(idx, role, model, ev.get("timestamp"), blocks, raw=ev))
            idx += 1
            continue

        # Any future/unrecognised top-level event type: preserve losslessly
        # at the conversation level rather than guessing at a shape.
        meta_events.append(ev)

    if not messages and not meta_events:
        return

    yield make_conversation(
        platform=platform,
        native_id=native_id,
        title=_session_title([e for e in events if e.get("type") == "message"]),
        project=project,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        source_path=str(path),
        raw={"session_header": header, "cwd": cwd, "meta_events": meta_events},
        messages=messages,
    )

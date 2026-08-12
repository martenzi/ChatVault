from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message
from chatvault.adapters.openclaw import is_openclaw_jsonl_name

platform = "hermes"
name = "hermes"

# Two on-disk shapes, one adapter:
#
#   A) whole-file JSON, either the bare dump ("hermes_conversation_*.json":
#      {model, session_start, messages:[...]}) or the richer session
#      envelope ("session_*.json": adds session_id, platform, tools, ...).
#      Both have a top-level "messages" list of {role, content} dicts.
#
#   B) one-JSON-object-per-line transcript ("sessions/*.jsonl"): an OpenAI
#      chat-completions-shaped log. First line is usually a synthetic
#      {"role": "session_meta", "tools": [...], "model": ..., ...} record;
#      subsequent lines are {"role": "user"|"assistant"|"tool", "content":
#      ..., "timestamp": ..., ["tool_calls": [...]]}.
#
# Explicitly NOT ours: "sessions.json" (an index keyed by session_key, no
# top-level "messages") and "request_dump_*.json" (crash/debug dumps with
# keys timestamp/session_id/reason/request/error, no "messages" either) —
# both fail the shape check below and fall through with no adapter, which is
# correct: they aren't conversations, ingesting them as one would be noise.

_ROLE_MAP = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "tool": "tool",
    "session_meta": "system",
}

_ID_FROM_NAME_RE = re.compile(r"(?:hermes_conversation_|session_)(\d{8}_\d{6}(?:_[0-9a-fA-F]+)?)")


def _native_id_from_name(path: Path) -> str | None:
    m = _ID_FROM_NAME_RE.search(path.stem)
    if m:
        return m.group(1)
    return path.stem or None


def _looks_like_message_list(messages) -> bool:
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    return isinstance(first, dict) and "role" in first and "content" in first


def sniff(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix == ".json":
        if path.name in ("sessions.json",) or path.name.startswith("request_dump_"):
            return False
        try:
            data = orjson.loads(path.read_bytes())
        except (orjson.JSONDecodeError, OSError):
            return False
        if not isinstance(data, dict):
            return False
        if "mapping" in data or "chat_messages" in data:
            return False  # chatgpt / claude_desktop territory
        return _looks_like_message_list(data.get("messages")) and (
            "session_start" in data or "session_id" in data or "model" in data
        )

    if is_openclaw_jsonl_name(path):
        # Require the specific "session_meta" header Hermes always writes as
        # the first line ({"role": "session_meta", "tools": [...], ...}).
        # A looser "any line with role+content" check would also match
        # plain OpenAI-shaped transcripts from other tools (see
        # tests/fixtures/generic/toolsession.jsonl, which generic_jsonl
        # legitimately owns) — sniff must stay specific, not just plausible.
        try:
            with path.open("rb") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    obj = orjson.loads(line)
                    if not isinstance(obj, dict):
                        return False
                    return obj.get("role") == "session_meta" and "tools" in obj
        except (orjson.JSONDecodeError, OSError):
            return False
    return False


def _stringify_content(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, default=str))
        return "\n".join(parts)
    return json.dumps(value, default=str)


def _tool_call_blocks(tool_calls: list, start_idx: int) -> list[dict]:
    blocks = []
    for offset, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            blocks.append(make_block(start_idx + offset, "unknown", "", raw={"value": call}))
            continue
        fn = call.get("function") or {}
        blocks.append(
            make_block(
                start_idx + offset,
                "tool_use",
                fn.get("arguments", ""),
                raw=call,
                tool_name=fn.get("name"),
            )
        )
    return blocks


def _message_blocks(msg: dict) -> list[dict]:
    blocks: list[dict] = []
    text = _stringify_content(msg.get("content"))
    if text:
        blocks.append(make_block(0, "text", text, raw={"content": msg.get("content")}))
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        blocks.extend(_tool_call_blocks(tool_calls, len(blocks)))
    if not blocks:
        blocks.append(make_block(0, "unknown", "", raw=msg))
    return blocks


def _parse_messages(
    raw_messages: list[dict], model: str | None
) -> tuple[list[dict], list[str]]:
    messages = []
    timestamps = []
    idx = 0
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        raw_role = m.get("role")
        role = _ROLE_MAP.get(raw_role, "system")
        created_at = m.get("timestamp")
        if isinstance(created_at, str):
            timestamps.append(created_at)

        if raw_role == "session_meta":
            # Tool manifest + runtime info, not a conversational turn, but
            # kept as a single system message so nothing is silently dropped.
            block = make_block(0, "system", f"session_meta: {len(m.get('tools') or [])} tool(s) available", raw=m)
            messages.append(make_message(idx, "system", None, created_at, [block], raw=m))
            idx += 1
            continue

        if raw_role == "tool":
            text = _stringify_content(m.get("content"))
            block = make_block(0, "tool_result", text, raw=m, tool_name=m.get("name"))
            messages.append(make_message(idx, "tool", None, created_at, [block], raw=m))
            idx += 1
            continue

        blocks = _message_blocks(m)
        msg_model = model if role == "assistant" else None
        messages.append(make_message(idx, role, msg_model, created_at, blocks, raw=m))
        idx += 1
    return messages, timestamps


def _title_from_messages(raw_messages: list[dict]) -> str | None:
    for m in raw_messages:
        if isinstance(m, dict) and m.get("role") == "user":
            text = _stringify_content(m.get("content"))
            if text:
                return text.strip().splitlines()[0][:120]
    return None


def _parse_json_shape(path: Path) -> Iterator[dict]:
    try:
        data = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError:
        return
    raw_messages = data.get("messages") or []
    if not raw_messages:
        return

    model = data.get("model") or None
    messages, timestamps = _parse_messages(raw_messages, model)
    if not messages:
        return

    native_id = data.get("session_id") or _native_id_from_name(path)
    started_at = data.get("session_start") or (min(timestamps) if timestamps else None)
    ended_at = data.get("last_updated") or (max(timestamps) if timestamps else None)

    yield make_conversation(
        platform=platform,
        native_id=native_id,
        title=_title_from_messages(raw_messages),
        project=data.get("platform"),
        started_at=started_at,
        ended_at=ended_at,
        source_path=str(path),
        raw={k: v for k, v in data.items() if k != "messages"},
        messages=messages,
    )


def _parse_jsonl_shape(path: Path) -> Iterator[dict]:
    raw_messages: list[dict] = []
    with path.open("rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                raw_messages.append(obj)

    if not raw_messages:
        return

    session_meta = next((m for m in raw_messages if m.get("role") == "session_meta"), None)
    model = (session_meta or {}).get("model") or None
    project = (session_meta or {}).get("platform")

    messages, timestamps = _parse_messages(raw_messages, model)
    if not messages:
        return

    native_id = _native_id_from_name(path)
    session_meta_ts = (session_meta or {}).get("timestamp")
    started_at = session_meta_ts or (min(timestamps) if timestamps else None)
    ended_at = max(timestamps) if timestamps else started_at

    yield make_conversation(
        platform=platform,
        native_id=native_id,
        title=_title_from_messages(raw_messages),
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        source_path=str(path),
        raw={"session_meta": session_meta},
        messages=messages,
    )


def parse(path: Path) -> Iterator[dict]:
    if path.suffix.lower() == ".json":
        yield from _parse_json_shape(path)
    else:
        yield from _parse_jsonl_shape(path)

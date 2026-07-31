from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "claude_code"
name = "claude_code"


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
                keys = set(obj.keys())
                return {"sessionId", "type"}.issubset(keys) or ("uuid" in keys and "type" in keys)
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
        return blocks

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
            blocks.append(
                make_block(
                    idx,
                    "tool_result",
                    _stringify_tool_result_content(item.get("content")),
                    raw=item,
                    tool_name=item.get("tool_use_id"),
                )
            )
        elif item_type == "image":
            blocks.append(make_block(idx, "image", "", raw=item))
        else:
            blocks.append(make_block(idx, "unknown", "", raw=item))
    return blocks


def _session_title(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") == "summary" and ev.get("summary"):
            return ev["summary"]
    for ev in events:
        if ev.get("type") == "user":
            content = ev.get("message", {}).get("content")
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
    sessions: dict[str, list[dict]] = {}
    order: list[str] = []
    with path.open("rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            sid = ev.get("sessionId") or "unknown"
            if sid not in sessions:
                sessions[sid] = []
                order.append(sid)
            sessions[sid].append(ev)

    for sid in order:
        events = sessions[sid]
        turn_events = [e for e in events if e.get("type") in ("user", "assistant")]
        if not turn_events:
            continue

        messages = []
        timestamps = []
        for idx, ev in enumerate(turn_events):
            msg = ev.get("message", {}) or {}
            role = msg.get("role") or ev.get("type")
            model = msg.get("model")
            created_at = ev.get("timestamp")
            if created_at:
                timestamps.append(created_at)
            blocks = _blocks_from_content(msg.get("content"))
            if ev.get("isSidechain"):
                for b in blocks:
                    b["raw"] = {**b["raw"], "_sidechain": True} if isinstance(b["raw"], dict) else b["raw"]
            messages.append(make_message(idx, role, model, created_at, blocks, raw=ev))

        cwd = next((e.get("cwd") for e in events if e.get("cwd")), None)
        project = Path(cwd).name if cwd else None

        yield make_conversation(
            platform=platform,
            native_id=sid,
            title=_session_title(events),
            project=project,
            started_at=min(timestamps) if timestamps else None,
            ended_at=max(timestamps) if timestamps else None,
            source_path=str(path),
            raw={"sessionId": sid, "cwd": cwd},
            messages=messages,
        )

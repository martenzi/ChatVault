from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "generic"
name = "generic_jsonl"

_ROLE_KEYS = ("role", "author", "sender", "speaker")
_CONTENT_KEYS = ("content", "text", "message", "body")
_TIME_KEYS = ("created_at", "timestamp", "time", "date")


def sniff(path: Path) -> bool:
    # Deliberately permissive fallback — any parseable JSONL file with at least
    # one role-ish and content-ish key qualifies. Registered last in the
    # detection order so specific adapters get first refusal.
    if path.suffix.lower() != ".jsonl":
        return False
    try:
        with path.open("rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = orjson.loads(line)
                if not isinstance(obj, dict):
                    return False
                keys = set(obj.keys())
                return bool(keys & set(_ROLE_KEYS)) or bool(keys & set(_CONTENT_KEYS))
    except (orjson.JSONDecodeError, OSError):
        return False
    return False


def _first_present(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _stringify_content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, default=str))
        return "\n".join(parts)
    if isinstance(value, dict):
        return value.get("text") or json.dumps(value, default=str)
    return json.dumps(value, default=str) if value is not None else ""


def parse(path: Path) -> Iterator[dict]:
    messages = []
    timestamps = []
    idx = 0
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

            role_raw = _first_present(obj, _ROLE_KEYS)
            role = role_raw if isinstance(role_raw, str) else (role_raw or {}).get("role", "unknown") if isinstance(role_raw, dict) else "unknown"

            content = _first_present(obj, _CONTENT_KEYS)
            text = _stringify_content(content)
            created_at = _first_present(obj, _TIME_KEYS)
            if isinstance(created_at, str):
                timestamps.append(created_at)

            blocks = [make_block(0, "text", text, raw=obj)] if text else [make_block(0, "unknown", "", raw=obj)]
            messages.append(make_message(idx, role or "unknown", obj.get("model"), created_at, blocks, raw=obj))
            idx += 1

    if not messages:
        return

    yield make_conversation(
        platform=platform,
        native_id=None,
        title=path.stem,
        project=None,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        source_path=str(path),
        raw={"source_file": str(path)},
        messages=messages,
    )

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol

# The closed set of block.type values every adapter must map into.
BLOCK_TYPES = {
    "text",
    "thinking",
    "tool_use",
    "tool_result",
    "code",
    "image",
    "attachment",
    "system",
    "error",
    "unknown",
}


class Adapter(Protocol):
    platform: str
    name: str

    def sniff(self, path: Path) -> bool:
        """Cheap, read-only check: could this file belong to this adapter?"""
        ...

    def parse(self, path: Path) -> Iterator[dict]:
        """Yield one CIR conversation dict per conversation found in path."""
        ...


def make_block(idx: int, type_: str, text: str, raw: dict, tool_name: str | None = None) -> dict:
    if type_ not in BLOCK_TYPES:
        type_ = "unknown"
    return {
        "idx": idx,
        "type": type_,
        "tool_name": tool_name,
        "text": text or "",
        "raw": raw,
    }


def make_message(idx: int, role: str, model: str | None, created_at: str | None, blocks: list[dict], raw: dict) -> dict:
    return {
        "idx": idx,
        "role": role,
        "model": model,
        "created_at": created_at,
        "raw": raw,
        "blocks": blocks,
    }


def make_conversation(
    *,
    platform: str,
    native_id: str | None,
    title: str | None,
    project: str | None,
    started_at: str | None,
    ended_at: str | None,
    source_path: str,
    raw: dict,
    messages: list[dict],
) -> dict:
    return {
        "platform": platform,
        "native_id": native_id,
        "title": title,
        "project": project,
        "started_at": started_at,
        "ended_at": ended_at,
        "source_path": source_path,
        "raw": raw,
        "messages": messages,
    }

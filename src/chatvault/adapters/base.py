from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol

# The closed set of block.type values every adapter must map into.
#
# injected_context / meta / refusal / audio added per
# docs/50_SPEC_ingestion_fidelity_v1.md §3.3 (Phase 2). No adapter emits
# them yet — that wiring is a later phase (§4) — this only extends the
# recognized vocabulary so make_block() doesn't demote them to "unknown"
# once an adapter starts using chatvault.adapters.identifiers.
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
    "injected_context",
    "meta",
    "refusal",
    "audio",
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


def make_block(
    idx: int,
    type_: str,
    text: str,
    raw: dict,
    tool_name: str | None = None,
    attachment: dict | None = None,
) -> dict:
    if type_ not in BLOCK_TYPES:
        type_ = "unknown"
    return {
        "idx": idx,
        "type": type_,
        "tool_name": tool_name,
        "text": text or "",
        "raw": raw,
        "attachment": attachment,
    }


def make_attachment(
    *,
    filename: str | None,
    mime: str | None = None,
    ext: str | None = None,
    bytes_: int | None = None,
    content: str | None = None,
    availability: str = "text",
    store_path: str | None = None,
    role: str = "upload",
    raw: dict | None = None,
) -> dict:
    """Payload attached to a make_block(type_='attachment', ...) call, or used
    directly for a project doc. `role` becomes attachment_links.role once the
    loader creates the link ('upload' | 'artifact' | 'project_knowledge')."""
    return {
        "filename": filename,
        "mime": mime,
        "ext": ext,
        "bytes": bytes_,
        "content": content,
        "availability": availability,
        "store_path": store_path,
        "role": role,
        "raw": raw if raw is not None else {},
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

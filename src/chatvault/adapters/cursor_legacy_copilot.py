from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "cursor_copilot"
name = "cursor_legacy_copilot"

# Source: tools/corpus_full/export_cursor_copilot_full.py. Two distinct
# record shapes share this one folder/platform, distinguished by
# meta["extraction"]:
#   "cursor-interactive-sessions-full-v1" — same request/response event shape
#     as vscode_legacy (role: user/tool/assistant, occasionally thinking).
#   "cursor-prompts-full-v1" — a single synthetic record dumping
#     aiService.prompts: a flat list of the user's raw prompts with NO
#     replies (no assistant turns exist for these at all — that's the real
#     shape of the source, not a parsing gap).

_SESSION_EXTRACTION = "cursor-interactive-sessions-full-v1"
_PROMPTS_EXTRACTION = "cursor-prompts-full-v1"


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
                return isinstance(obj, dict) and obj.get("type") == "meta" and obj.get("extraction") in (
                    _SESSION_EXTRACTION,
                    _PROMPTS_EXTRACTION,
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


def _classify(rec: dict) -> tuple[str, str]:
    role = rec.get("role")
    if role == "user":
        return "user", "text"
    if role == "thinking":
        return "assistant", "thinking"
    if role == "tool":
        return "tool", "tool_use"
    return "assistant", "text" if rec.get("text") else "unknown"


def _tool_name(part: dict) -> str | None:
    return part.get("toolId") or part.get("toolCallId")


def _tool_fallback_text(part: dict) -> str:
    inv = part.get("invocationMessage")
    return inv.get("value", "") if isinstance(inv, dict) else ""


def _parse_session(path: Path, meta: dict, records: list[dict]) -> Iterator[dict]:
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

    if not messages:
        return

    yield make_conversation(
        platform=platform,
        native_id=meta.get("session_id"),
        title=_clean(meta.get("title"), "untitled"),
        project=_clean(meta.get("project"), "unknown"),
        started_at=_iso_z(meta.get("created")),
        ended_at=None,
        source_path=str(path),
        raw=meta,
        messages=messages,
    )


def _parse_prompts_dump(path: Path, meta: dict, records: list[dict]) -> Iterator[dict]:
    content = records[0].get("content") if records else None
    prompts = content if isinstance(content, list) else ([content] if content else [])

    messages = []
    for idx, p in enumerate(prompts):
        if isinstance(p, dict):
            text = p.get("text") or json.dumps(p, default=str)
            raw = p
        elif isinstance(p, str):
            text = p
            raw = {"text": p}
        else:
            text = json.dumps(p, default=str) if p is not None else ""
            raw = {"value": p}
        block = make_block(0, "text", text, raw=raw)
        # No replies exist for this source (a raw prompt dump), so every
        # entry is a standalone user turn with no assistant/timestamp data.
        messages.append(make_message(idx, "user", None, None, [block], raw=raw))

    if not messages:
        return

    yield make_conversation(
        platform=platform,
        native_id=meta.get("session_id"),
        title=_clean(meta.get("title"), "untitled"),
        project=_clean(meta.get("project"), "unknown"),
        started_at=None,
        ended_at=None,
        source_path=str(path),
        raw=meta,
        messages=messages,
    )


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

    if meta.get("extraction") == _PROMPTS_EXTRACTION:
        yield from _parse_prompts_dump(path, meta, records)
    else:
        yield from _parse_session(path, meta, records)

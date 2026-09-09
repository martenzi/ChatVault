from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message
from chatvault.adapters.identifiers import coerce_timestamp

platform = "antigravity"
name = "antigravity"

# All findings cited below are from docs/reports/antigravity_shapes_2026-09-09.md.
# No .proto schema ships with the app (verified: grepped the entire app bundle,
# including the 14MB jetskiAgent/main.js where the protobuf agent client lives --
# no generated message descriptors found). Every field path below was confirmed
# by directly inspecting decoded bytes of real payloads, not guessed.

_SCHEMA_TAG = "antigravity_conversation_v1"


# ── Minimal schema-less protobuf reader, TARGETED not recursive ────────────
#
# A blind recursive walker (tried during investigation) occasionally
# mis-parses a plain string as a nested sub-message when its bytes
# coincidentally look like a valid protobuf frame, corrupting the value
# (observed: a UUID's tail got split off as a fake nested field). This
# reader only ever descends along an EXPLICIT field-number path the report
# verified -- it never guesses whether to recurse.


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _decode_fields(buf: bytes) -> list[tuple[int, int, object]]:
    """One level of protobuf decode -> [(field_no, wire_type, raw_value)]."""
    pos = 0
    n = len(buf)
    out: list[tuple[int, int, object]] = []
    try:
        while pos < n:
            tag, pos = _read_varint(buf, pos)
            field_no = tag >> 3
            wire_type = tag & 0x7
            if wire_type == 0:
                val, pos = _read_varint(buf, pos)
            elif wire_type == 1:
                val = buf[pos : pos + 8]
                pos += 8
            elif wire_type == 2:
                length, pos = _read_varint(buf, pos)
                val = buf[pos : pos + length]
                pos += length
            elif wire_type == 5:
                val = buf[pos : pos + 4]
                pos += 4
            else:
                return out
            out.append((field_no, wire_type, val))
    except (IndexError, ValueError):
        return out
    return out


def _get_all(buf: bytes | None, field_no: int) -> list[bytes]:
    if not buf:
        return []
    return [v for fn, wt, v in _decode_fields(buf) if fn == field_no and wt == 2]


def _get_first(buf: bytes | None, field_no: int) -> bytes | None:
    vals = _get_all(buf, field_no)
    return vals[0] if vals else None


def _get_path(buf: bytes | None, *field_nos: int) -> bytes | None:
    cur = buf
    for fn in field_nos:
        cur = _get_first(cur, fn)
        if cur is None:
            return None
    return cur


def _maybe_text(raw: bytes | None) -> str | None:
    """Report §4: a string field is sometimes the bytes directly, sometimes
    one more level of nesting under field 14 (observed on both tool-name and
    session-id fields) -- an apparent oneof/wrapper type. Try direct decode
    first; if that fails or isn't printable, try the field-14 wrapper."""
    if raw is None:
        return None
    text = _try_utf8(raw)
    if text is not None:
        return text
    wrapped = _get_first(raw, 14)
    return _try_utf8(wrapped) if wrapped is not None else None


def _try_utf8(raw: bytes | None) -> str | None:
    if not raw:
        return None
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s:
        return None
    printable = sum(1 for c in s if c.isprintable() or c in "\n\t")
    return s if printable / len(s) > 0.85 else None


def _all_string_leaves(buf: bytes, depth: int = 0, max_depth: int = 8) -> list[str]:
    """Fallback for the `unknown` case (report §6): recursive search, used
    ONLY to rank candidate text for an unmapped step_type's fallback block --
    never used for a mapped/trusted path, so a mis-parse here only affects
    quality of an already-uncertain classification, not a wrongly-labeled one."""
    out = []
    for fn, wt, v in _decode_fields(buf):
        if wt == 2 and isinstance(v, bytes):
            if depth < max_depth:
                sub = _decode_fields(v)
                if sub:
                    out.extend(_all_string_leaves(v, depth + 1, max_depth))
                    continue
            text = _try_utf8(v)
            if text:
                out.append(text)
    return out


# ── Tool-call step_types (report §4) — all share the same field-5 shape ────
_TOOL_STEP_TYPES = {5, 7, 9, 17, 21, 23, 28, 31, 33, 98, 132, 138}
# Result/notification step_types with a known text location.
_RESULT_PATHS: dict[int, tuple[tuple[int, ...], ...]] = {
    8: ((10, 2, 1, 2, 3, 3, 1), (14, 4)),
    13: ((13, 3),),
    101: ((114, 2, 2), (114, 4, 4)),
    132: ((140, 2, 1),),
}
_INJECTED_CONTEXT_TYPES = {90}
_USER_MESSAGE_TYPES = {14}
_ASSISTANT_TURN_TYPES = {15}
_NO_CONTENT_TYPES = {99}


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".jsonl":
        return False
    try:
        with path.open("rb") as fh:
            first_line = fh.readline()
    except OSError:
        return False
    first_line = first_line.strip()
    if not first_line:
        return False
    try:
        obj = orjson.loads(first_line)
    except orjson.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("schema") == _SCHEMA_TAG


def _iso(ts) -> str | None:
    dt = coerce_timestamp(ts)
    return dt.isoformat() if dt else None


def _b64_to_bytes(s: str | None) -> bytes | None:
    if not s:
        return None
    try:
        return base64.b64decode(s)
    except (ValueError, TypeError):
        return None


def _tool_use_block(payload: bytes, idx: int) -> dict:
    """Report §4: tool call fields are at 5.4.1/5.4.2/5.4.3, or nested one
    level under 5 -> 4 for the polymorphic step_type=15 case at 20.7.*."""
    name = _maybe_text(_get_path(payload, 5, 4, 2)) or _maybe_text(_get_path(payload, 20, 7, 2))
    args_raw = _get_path(payload, 5, 4, 3) or _get_path(payload, 20, 7, 3)
    args_text = _try_utf8(args_raw) or ""
    return make_block(idx, "tool_use", args_text, raw={"name": name, "args": args_text}, tool_name=name)


def _tool_result_text(step_type: int, payload: bytes) -> str | None:
    for path in _RESULT_PATHS.get(step_type, ()):
        text = _maybe_text(_get_path(payload, *path))
        if text:
            return text
    return None


def _blocks_from_step(step_type: int, payload: bytes | None) -> list[dict]:
    if payload is None:
        return []
    idx = 0
    blocks: list[dict] = []

    if step_type in _USER_MESSAGE_TYPES:
        text = _maybe_text(_get_path(payload, 19, 2)) or _maybe_text(_get_path(payload, 19, 3, 1))
        if text:
            blocks.append(make_block(idx, "text", text, raw={"path": "19.2/19.3.1"}))
            idx += 1

    elif step_type in _ASSISTANT_TURN_TYPES:
        thinking = _maybe_text(_get_path(payload, 20, 3))
        if thinking:
            blocks.append(make_block(idx, "thinking", thinking, raw={"path": "20.3"}))
            idx += 1
        tool_name = _maybe_text(_get_path(payload, 20, 7, 2)) or _maybe_text(_get_path(payload, 20, 7, 1))
        # Only emit a tool_use block here when a name actually resolves --
        # an unresolved field 20.7 with no name adds nothing over the
        # adjacent, more specific tool-family step (report §4's tool-call
        # table) that already carries the authoritative call.
        if tool_name:
            blocks.append(_tool_use_block(payload, idx))
            idx += 1

    elif step_type in _TOOL_STEP_TYPES:
        tb = _tool_use_block(payload, idx)
        if tb["tool_name"] or tb["text"]:
            blocks.append(tb)
            idx += 1
        result_text = _tool_result_text(step_type, payload)
        if result_text:
            blocks.append(make_block(idx, "tool_result", result_text, raw={"step_type": step_type}))
            idx += 1

    elif step_type in _INJECTED_CONTEXT_TYPES:
        text = _maybe_text(_get_path(payload, 103, 1))
        if text:
            blocks.append(make_block(idx, "injected_context", text, raw={"path": "103.1"}))
            idx += 1

    elif step_type in _RESULT_PATHS:
        text = _tool_result_text(step_type, payload)
        if text:
            blocks.append(make_block(idx, "tool_result", text, raw={"step_type": step_type}))
            idx += 1

    elif step_type in _NO_CONTENT_TYPES:
        return []

    if not blocks:
        # Report §6: unmapped step_type -> `unknown` with text from the
        # highest-volume string leaf, per D7, not silently empty.
        candidates = _all_string_leaves(payload)
        text = max(candidates, key=len) if candidates else ""
        blocks.append(make_block(0, "unknown", text, raw={"step_type": step_type}))

    return blocks


def _step_role(step_type: int) -> str:
    if step_type in _USER_MESSAGE_TYPES:
        return "user"
    if step_type in _INJECTED_CONTEXT_TYPES:
        return "system"
    return "assistant"


def _title(steps: list[dict]) -> str | None:
    for step in steps:
        if step["step_type"] in _USER_MESSAGE_TYPES:
            payload = _b64_to_bytes(step.get("step_payload"))
            text = _maybe_text(_get_path(payload, 19, 2)) if payload else None
            if text:
                return text.strip().splitlines()[0][:120]
    return None


def parse(path: Path) -> Iterator[dict]:
    with path.open("rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            if rec.get("schema") != _SCHEMA_TAG:
                continue

            uuid = rec.get("uuid")
            steps = rec.get("steps") or []
            if not uuid or not steps:
                continue

            messages = []
            timestamps = []
            for idx, step in enumerate(steps):
                step_type = step["step_type"]
                payload = _b64_to_bytes(step.get("step_payload"))
                blocks = _blocks_from_step(step_type, payload)
                if not blocks:
                    continue
                # Report §7: a Timestamp{seconds, nanos} pair at 5.1.1/5.1.2
                # on step_type=15 steps' own metadata -- varint fields, walked
                # via _varint_at (not _get_path, which only follows
                # length-delimited hops).
                created_at = _iso(_varint_at(payload, 5, 1, 1)) if payload is not None else None
                if created_at:
                    timestamps.append(created_at)
                role = _step_role(step_type)
                messages.append(make_message(idx, role, None, created_at, blocks, raw=step))

            if not messages:
                continue

            trajectory_meta = (rec.get("trajectory_meta") or [{}])[0]
            source_mtime = rec.get("source_mtime")
            fallback_ts = (
                datetime.fromtimestamp(source_mtime, tz=timezone.utc).astimezone().isoformat()
                if isinstance(source_mtime, (int, float))
                else None
            )

            yield make_conversation(
                platform=platform,
                native_id=uuid,
                title=_title(steps),
                project=None,
                started_at=min(timestamps) if timestamps else fallback_ts,
                ended_at=max(timestamps) if timestamps else fallback_ts,
                source_path=str(path),
                raw={
                    "uuid": uuid,
                    "surface": rec.get("surface"),
                    "trajectory_meta": trajectory_meta,
                    "steps": steps,
                    "gen_metadata": rec.get("gen_metadata"),
                    "executor_metadata": rec.get("executor_metadata"),
                    "trajectory_metadata_blob": rec.get("trajectory_metadata_blob"),
                },
                messages=messages,
            )


def _varint_at(buf: bytes | None, *field_nos: int) -> int | None:
    """Walk field_nos[:-1] as nested messages, then read the final field as
    a varint (wire_type 0) at that level -- distinct from _get_path, which
    only follows wire_type-2 (length-delimited) hops."""
    if buf is None:
        return None
    cur = buf
    for fn in field_nos[:-1]:
        cur = _get_first(cur, fn)
        if cur is None:
            return None
    last = field_nos[-1]
    for fn, wt, v in _decode_fields(cur):
        if fn == last and wt == 0:
            return v
    return None

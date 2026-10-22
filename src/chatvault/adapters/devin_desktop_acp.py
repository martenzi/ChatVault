from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_attachment, make_block, make_conversation, make_message
from chatvault.adapters.identifiers import coerce_timestamp

platform = "devin_desktop_acp"
name = "devin_desktop_acp"

# All findings cited below are from
# docs/reports/devin_desktop_acp_shapes_2026-09-09.md, produced by full
# enumeration of every message in every one of the 326 real+empty .db files
# -- not inferred from doc 48's provisional kind list.

_SCHEMA_TAG = "devin_desktop_acp_conversation_v1"


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


def _join_content_fragments(content_items: list) -> tuple[str, list[dict]]:
    """Report §4.1: agent_thought/agent_message/user_message rows carry
    `content` as a LIST of small chunk fragments (one row's `content[]` can
    hold hundreds of items, all sharing one streamingMessageId) --
    content.type is 'text' for 601,344/601,398 items; the two exceptions
    are 'resource_link' (an @file mention, no inline text) and 'resource'
    (one instance, carries inline text). Returns (joined_text,
    extra_attachment_dicts) so callers can emit the dialogue as one block
    plus any resource_link/resource items as separate attachment blocks."""
    parts: list[str] = []
    attachments: list[dict] = []
    for item in content_items:
        if not isinstance(item, dict):
            continue
        c = item.get("content")
        if not isinstance(c, dict):
            continue
        ctype = c.get("type")
        if ctype == "text":
            t = c.get("text")
            if t:
                parts.append(t)
        elif ctype == "resource_link":
            attachments.append(
                {
                    "filename": c.get("name"),
                    "content": None,
                    "availability": "missing",
                    "raw": c,
                }
            )
        elif ctype == "resource":
            resource = c.get("resource") or {}
            attachments.append(
                {
                    "filename": None,
                    "content": resource.get("text"),
                    "availability": "text" if resource.get("text") else "missing",
                    "mime": resource.get("mimeType"),
                    "raw": c,
                }
            )
    return "".join(parts), attachments


def _text_and_attachment_blocks(kind: str, block_type: str, content_items: list, start_idx: int) -> list[dict]:
    text, attachments = _join_content_fragments(content_items)
    blocks: list[dict] = []
    idx = start_idx
    if text:
        blocks.append(make_block(idx, block_type, text, raw={"content": content_items}))
        idx += 1
    for att in attachments:
        blocks.append(
            make_block(
                idx,
                "attachment",
                "",
                raw=att["raw"],
                attachment=make_attachment(
                    filename=att.get("filename"),
                    mime=att.get("mime"),
                    content=att.get("content"),
                    availability=att["availability"],
                    role="upload",
                    raw=att["raw"],
                ),
            )
        )
        idx += 1
    return blocks


def _tool_call_blocks(payload: dict, start_idx: int) -> list[dict]:
    """Report §4.2: call + result live in ONE row here (unlike sessions.db's
    two-row split) -- content.rawInput is the call, content.content[] is
    the result, both nested under the same content.toolCallId. Split into
    two blocks (tool_use, tool_result) to match the shared block-type
    vocabulary and the claude_code.py precedent, even though the source
    only needed one row."""
    content = payload.get("content") or {}
    if not isinstance(content, dict):
        return [make_block(start_idx, "unknown", "", raw=payload)]

    blocks = []
    idx = start_idx

    blocks.append(
        make_block(
            idx,
            "tool_use",
            "" if content.get("rawInput") is None else str(content.get("rawInput")),
            raw={k: v for k, v in content.items() if k != "content"},
            tool_name=content.get("title") or content.get("toolCallId"),
        )
    )
    idx += 1

    result_items = content.get("content")
    if isinstance(result_items, list) and result_items:
        parts = []
        for item in result_items:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(item["text"])
                elif "newText" in item:
                    parts.append(item.get("newText") or "")
                else:
                    nested = item.get("content")
                    if isinstance(nested, dict) and nested.get("type") == "text":
                        parts.append(nested.get("text") or "")
        status = content.get("status")
        text = "\n".join(p for p in parts if p)
        if status == "failed" and not text:
            text = f"[tool call failed: {content.get('title', '')}]"
        blocks.append(
            make_block(
                idx,
                "tool_result",
                text,
                raw={"content": result_items, "status": status},
                tool_name=content.get("toolCallId"),
            )
        )
        idx += 1

    return blocks


def _plan_block(payload: dict, idx: int) -> dict:
    """Report §4.3: plan/todo widget state, not dialogue -- same bucket as
    identifiers.NON_MESSAGE_RECORD_TYPES's session-bookkeeping records.
    Rendered as text so it's still inspectable in `full`, classified
    `meta` so it doesn't read as extra assistant dialogue in `read`/`lean`."""
    content = payload.get("content") or {}
    entries = content.get("entries") or []
    lines = []
    for e in entries:
        if isinstance(e, dict):
            lines.append(f"[{e.get('status', '?')}] ({e.get('priority', '?')}) {e.get('content', '')}")
    text = "\n".join(lines)
    return make_block(idx, "meta", text, raw=payload)


def _user_edits_block(payload: dict, idx: int) -> dict | None:
    """Report §4.4: `userEdits` on a subset of user_message rows -- files
    the user edited outside the chat, injected back into context. Not
    something the user typed; same conceptual bucket as
    identifiers.is_injected_context() though this is structured JSON, not
    a text marker."""
    user_edits = payload.get("userEdits")
    if not isinstance(user_edits, dict):
        return None
    files = user_edits.get("files") or []
    lines = [f"{f.get('kind', '?')} {f.get('path', '')} (+{f.get('insertions', 0)}/-{f.get('deletions', 0)})" for f in files if isinstance(f, dict)]
    return make_block(idx, "injected_context", "\n".join(lines), raw=user_edits)


def _subagent_block(payload: dict, idx: int) -> dict:
    """Report §4.5: only 6 rows in the whole corpus, each fully
    self-contained (task/summary/childMessages all inline in this one row
    -- no separate file to lose, unlike claude_code.py's subagent
    transcripts). Kept nested rather than split into a new conversation;
    task+summary become the visible text, the full childMessages tree
    stays in raw for full-profile/debugging."""
    task = payload.get("task") or ""
    summary = payload.get("summary") or ""
    title = payload.get("title") or "subagent"
    text = f"[subagent: {title}]\n\nTask: {task}\n\nResult: {summary}"
    return make_block(idx, "text", text, raw=payload)


def _blocks_from_message(kind: str, payload: dict) -> list[dict]:
    if kind == "agent_thought":
        return _text_and_attachment_blocks(kind, "thinking", payload.get("content") or [], 0)
    if kind == "agent_message":
        return _text_and_attachment_blocks(kind, "text", payload.get("content") or [], 0)
    if kind == "user_message":
        blocks = _text_and_attachment_blocks(kind, "text", payload.get("content") or [], 0)
        ue_block = _user_edits_block(payload, len(blocks))
        if ue_block:
            blocks.append(ue_block)
        return blocks or [make_block(0, "unknown", "", raw=payload)]
    if kind == "tool_call":
        return _tool_call_blocks(payload, 0)
    if kind == "plan":
        return [_plan_block(payload, 0)]
    if kind == "subagent":
        return [_subagent_block(payload, 0)]
    return [make_block(0, "unknown", "", raw=payload)]


_ROLE_FOR_KIND = {
    "agent_thought": "assistant",
    "agent_message": "assistant",
    "user_message": "user",
    "tool_call": "assistant",
    "plan": "system",
    "subagent": "assistant",
}


def _project_from_messages(messages: list[dict]) -> str | None:
    """Report §6: no workspace path lives in meta() at all -- the only
    place one appears is a tool_call row's content._meta['cognition.ai/cwd'].
    Take the first one observed in the session, basenamed, same pattern as
    claude_code.py's Path(cwd).name."""
    for m in messages:
        payload = m.get("payload") or {}
        if m.get("kind") != "tool_call":
            continue
        content = payload.get("content") or {}
        meta = content.get("_meta") or {}
        cwd = meta.get("cognition.ai/cwd")
        if cwd:
            return Path(cwd).name
    return None


def _title(meta: dict) -> str | None:
    info = meta.get("info") or {}
    return info.get("title")


def _model(meta: dict) -> str | None:
    info = meta.get("info") or {}
    for opt in info.get("configOptions") or []:
        if isinstance(opt, dict) and opt.get("id") == "model":
            return opt.get("currentValue")
    return None


def _message_timestamp(message: dict) -> str | None:
    """Per-message time. The only carrier in the source is `cognition.ai/timestamp`
    in the `_meta` of the message's content items (no `_meta` exists on the
    payload itself); a streamed thought can span several items, so the earliest
    is the moment the message began. `payload.sourceEventTimestampMs` (tool
    calls only) is the same instant and serves as the fallback. About half of
    all rows carry neither — those stay NULL, never interpolated."""
    payload = message.get("payload") or {}
    content = payload.get("content")
    items = content if isinstance(content, list) else [content] if isinstance(content, dict) else []
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ts = (item.get("_meta") or {}).get("cognition.ai/timestamp")
        dt = coerce_timestamp(ts) if ts else None
        if dt:
            found.append(dt)
    if not found:
        ms = payload.get("sourceEventTimestampMs")
        if isinstance(ms, (int, float)) and not isinstance(ms, bool):
            dt = coerce_timestamp(ms / 1000)
            if dt:
                found.append(dt)
    return min(found).isoformat() if found else None


def _message_timestamps(messages: list[dict]) -> list[str]:
    return [ts for ts in (_message_timestamp(m) for m in messages) if ts]


def parse(path: Path) -> Iterator[dict]:
    with path.open("rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            if record.get("schema") != _SCHEMA_TAG:
                continue

            uuid = record.get("uuid")
            raw_messages = record.get("messages") or []
            if not uuid or not raw_messages:
                continue

            meta = record.get("meta") or {}
            model = _model(meta)

            messages = []
            for idx, m in enumerate(raw_messages):
                kind = m.get("kind")
                payload = m.get("payload") or {}
                blocks = _blocks_from_message(kind, payload)
                if not blocks:
                    continue
                role = _ROLE_FOR_KIND.get(kind, "unknown")
                msg_model = model if role == "assistant" else None
                messages.append(make_message(idx, role, msg_model, _message_timestamp(m), blocks, raw=m))

            if not messages:
                continue

            timestamps = _message_timestamps(raw_messages)
            source_mtime = record.get("source_mtime")
            fallback_ts = (
                datetime.fromtimestamp(source_mtime, tz=timezone.utc).astimezone().isoformat()
                if isinstance(source_mtime, (int, float))
                else None
            )

            yield make_conversation(
                platform=platform,
                native_id=uuid,
                title=_title(meta),
                project=_project_from_messages(raw_messages),
                started_at=min(timestamps) if timestamps else fallback_ts,
                ended_at=max(timestamps) if timestamps else fallback_ts,
                source_path=str(path),
                raw={"uuid": uuid, "meta": meta, "messages": raw_messages},
                messages=messages,
            )

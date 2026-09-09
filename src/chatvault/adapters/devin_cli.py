from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message
from chatvault.adapters.identifiers import coerce_timestamp

platform = "devin_cli"
name = "devin_cli"

# All findings cited below (line numbers, key paths, occurrence counts) are
# from docs/reports/devin_cli_shapes_2026-09-09.md, produced by full
# enumeration of every row of the live sessions.db — not inferred.

_SCHEMA_TAG = "devin_cli_session_v1"


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


def _tool_call_blocks(tool_calls: list, start_idx: int) -> list[dict]:
    """chat_message.tool_calls[] -> tool_use blocks. Report §4.5: always a
    dict {id, name, arguments, index, kind}, arguments always a dict
    (29,565/29,565)."""
    blocks = []
    for offset, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            blocks.append(make_block(start_idx + offset, "unknown", "", raw={"value": call}))
            continue
        blocks.append(
            make_block(
                start_idx + offset,
                "tool_use",
                json.dumps(call.get("arguments", {}), default=str),
                raw=call,
                tool_name=call.get("name"),
            )
        )
    return blocks


def _image_blocks(images: list, start_idx: int) -> list[dict]:
    """Report §4.6: top-level `images` key, always on role='tool' rows
    (screenshot results), list of {width,height,base64_data,mime_type}.
    base64_data is the single highest-volume string leaf in the whole
    enumeration (50.9M chars / 102 occurrences) -- never duplicated into a
    text field, stays exactly where it already is in `raw`, same treatment
    as claude_code.py's `document` item type."""
    blocks = []
    for offset, img in enumerate(images):
        if not isinstance(img, dict):
            continue
        mime = img.get("mime_type")
        ext = mime.split("/")[-1] if isinstance(mime, str) and "/" in mime else None
        blocks.append(
            make_block(
                start_idx + offset,
                "image",
                "",
                raw={k: v for k, v in img.items() if k != "base64_data"},
                attachment={
                    "filename": None,
                    "mime": mime,
                    "ext": ext,
                    "bytes": len(img.get("base64_data", "")) if isinstance(img.get("base64_data"), str) else None,
                    "content": None,
                    "availability": "missing",
                    "store_path": None,
                    "role": "upload",
                    "raw": {"width": img.get("width"), "height": img.get("height")},
                },
            )
        )
    return blocks


def _blocks_from_chat_message(chat_message: dict) -> list[dict]:
    """One message_nodes row -> its list of blocks.

    Report §4.3: `content` is always a plain string (52,124/52,124 -- never
    a list/dict) for this platform; report §4.4: reasoning lives at a
    top-level `thinking` dict key (`{thinking, signature, signature_type?}`),
    never nested inside `content`."""
    blocks: list[dict] = []
    idx = 0

    content = chat_message.get("content")
    if isinstance(content, str) and content:
        blocks.append(make_block(idx, "text", content, raw={"content": content}))
        idx += 1

    thinking = chat_message.get("thinking")
    if isinstance(thinking, dict):
        text = thinking.get("thinking") or ""
        blocks.append(make_block(idx, "thinking", text, raw=thinking))
        idx += 1

    tool_calls = chat_message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        new_blocks = _tool_call_blocks(tool_calls, idx)
        blocks.extend(new_blocks)
        idx += len(new_blocks)

    images = chat_message.get("images")
    if isinstance(images, list) and images:
        new_blocks = _image_blocks(images, idx)
        blocks.extend(new_blocks)
        idx += len(new_blocks)

    if chat_message.get("role") == "tool" and isinstance(content, str):
        # Report §4.5: the tool RESULT is this row's own top-level `content`
        # string (role='tool' rows are keyed by tool_call_id, matched
        # against the calling tool_calls[].id on the preceding assistant
        # message). Already captured as a "text" block above -- reclassify
        # it tool_result instead of leaving it as a bare text block, so it
        # renders correctly as the other half of the tool_use/tool_result
        # pair (mirrors claude_code.py's split).
        for b in blocks:
            if b["type"] == "text" and b["text"] == content:
                b["type"] = "tool_result"
                b["tool_name"] = chat_message.get("tool_call_id")
                break

    if not blocks:
        blocks.append(make_block(0, "unknown", "", raw=chat_message))

    return blocks


def _linearise_main_chain(message_nodes: list[dict], main_chain_id: int | None) -> list[dict]:
    """Report §5: walk parent_node_id back from main_chain_id to the root,
    reverse to chronological order. Every session in the investigation had
    orphan nodes (81% of all nodes, corpus-wide) -- exact-duplicate
    resends from CLI reconnects and abandoned alternate-generation retry
    siblings. The main chain is the one narrative `main_chain_id` itself
    names as current; it structurally excludes both duplicate causes. The
    full node tree (all 52,124 nodes, orphans included) still reaches
    `raw` via the conversation's own raw payload -- nothing is deleted,
    only `messages`/`blocks` are limited to the main chain."""
    by_node_id = {n["node_id"]: n for n in message_nodes}
    chain: list[dict] = []
    seen: set[int] = set()
    cur = main_chain_id
    while cur is not None and cur in by_node_id and cur not in seen:
        seen.add(cur)
        node = by_node_id[cur]
        chain.append(node)
        cur = node.get("parent_node_id")
    chain.reverse()
    return chain


def _session_title(session: dict, chain: list[dict]) -> str | None:
    title = session.get("title")
    if title:
        return title
    for node in chain:
        cm = node.get("chat_message") or {}
        if cm.get("role") == "user" and isinstance(cm.get("content"), str) and cm["content"].strip():
            return cm["content"].strip().splitlines()[0][:120]
    return None


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

            session = record.get("session") or {}
            message_nodes = record.get("message_nodes") or []

            # Report §2: hidden=1 marks internally-spawned utility sessions
            # (the one instance found was a "You are powered by Summarizer."
            # title/compaction-summary run, working_directory='/', not a
            # user conversation). Excluded from ingestion by default; the
            # full record (this JSONL line) still exists in raw/devin_cli/
            # if this ever needs auditing (D3 -- nothing is deleted, only
            # not emitted as a conversation).
            if session.get("hidden"):
                continue

            chain = _linearise_main_chain(message_nodes, session.get("main_chain_id"))
            if not chain:
                continue

            messages = []
            timestamps = []
            for idx, node in enumerate(chain):
                cm = node.get("chat_message")
                if not isinstance(cm, dict):
                    continue
                role = cm.get("role") or "unknown"
                created_at = _iso(node.get("created_at"))
                if created_at:
                    timestamps.append(created_at)
                blocks = _blocks_from_chat_message(cm)
                if not blocks:
                    continue
                # role='assistant' rows don't carry their own generation
                # model per node; the session-level model is the closest
                # available fact (report §2 -- distinct per-session values
                # confirmed, no per-message override needed for this source).
                msg_model = session.get("model") if role == "assistant" else None
                messages.append(make_message(idx, role, msg_model, created_at, blocks, raw=node))

            if not messages:
                continue

            working_directory = session.get("working_directory")
            project = Path(working_directory).name if working_directory else None

            yield make_conversation(
                platform=platform,
                native_id=session.get("id"),
                title=_session_title(session, chain),
                project=project,
                started_at=_iso(session.get("created_at")),
                ended_at=_iso(session.get("last_activity_at")),
                source_path=str(path),
                raw={
                    "session": session,
                    # Full node tree, orphans included -- report §5's D3
                    # guarantee. main-chain node_ids are recoverable from
                    # each message's own raw.node_id, so a reader can
                    # reconstruct exactly what was excluded and why.
                    "message_nodes": message_nodes,
                    "tool_call_state": record.get("tool_call_state") or [],
                    "prompt_history": record.get("prompt_history") or [],
                },
                messages=messages,
            )

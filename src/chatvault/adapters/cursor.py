from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_attachment, make_block, make_conversation, make_message
from chatvault.adapters.identifiers import coerce_timestamp

platform = "cursor"
name = "cursor"

# One real composerId, of 57 in the corpus, was found to contain a literal
# embedded newline ("task-call-<id>-61\nfc_<hash>_5") -- confirmed real
# content (a task_v2-spawned subagent's own tool-call trace, glob_file_search
# calls, no composerHeaders row) and not an artifact of this adapter's own
# parsing (verified directly against the raw cursorDiskKV key). Cursor
# itself appears to have concatenated two id fragments with "\n" instead of
# properly parenting this session. A raw control character in `native_id`
# would break markdown export filenames, CLI display and shell-safety
# downstream, so it is sanitised here (control chars -> a visible
# placeholder) while `raw['composer_id']` keeps the exact original string --
# per D7, flag rather than silently drop or guess at the "real" id.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f]")


def _sanitise_native_id(composer_id: str) -> str:
    return _CONTROL_CHAR_RE.sub("_", composer_id)

# All findings cited below are from docs/reports/cursor_shapes_2026-09-09.md
# (full enumeration of the live state.vscdb + conversation-search.db) and
# its dated correction banner, plus scripts/extract_cursor.py's own
# docstring for the cloud-cache merge behaviour discovered while building.

_SCHEMA_TAG = "cursor_conversation_v1"


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


def _ordered_bubble_ids(composer_data: dict | None, bubbles_by_id: dict[str, dict]) -> list[str]:
    """Report §8: composerData.fullConversationHeadersOnly is the
    authoritative ordered list Cursor itself renders as the conversation --
    19.4% of bubbles corpus-wide are orphaned edit/regenerate history not in
    this list. Fall back to createdAt-sort (ISO strings, sort correctly as
    text) only when no cached list exists (1 of 57 real composers, always a
    single-bubble trivial case in the investigation)."""
    if composer_data:
        fch = composer_data.get("fullConversationHeadersOnly")
        if fch:
            ids = [h.get("bubbleId") for h in fch if isinstance(h, dict) and h.get("bubbleId") in bubbles_by_id]
            if ids:
                return ids
    return sorted(bubbles_by_id.keys(), key=lambda bid: bubbles_by_id[bid].get("createdAt") or "")


def _tool_blocks(tfd: dict, start_idx: int) -> list[dict]:
    """Report §4.3: toolFormerData carries both the call and its result in
    one bubble. `.name` alone identifies the tool (the numeric `.tool` enum
    is redundant). Use `.result` for the tool_result text, never
    `.toolCallBinary` (a same-content binary-safe duplicate, confirmed by
    inspection -- the highest-volume string leaf in the whole corpus for
    zero new information)."""
    blocks = []
    idx = start_idx
    args = tfd.get("params") or tfd.get("rawArgs") or ""
    if not isinstance(args, str):
        args = json.dumps(args, default=str)
    blocks.append(
        make_block(
            idx,
            "tool_use",
            args,
            raw={k: v for k, v in tfd.items() if k != "toolCallBinary"},
            tool_name=tfd.get("name"),
        )
    )
    idx += 1

    result = tfd.get("result")
    error = tfd.get("error")
    status = tfd.get("status")
    if result or error:
        text = result if isinstance(result, str) else (json.dumps(result, default=str) if result else "")
        if not text and error:
            text = f"[error] {error}" if isinstance(error, str) else json.dumps(error, default=str)
        blocks.append(
            make_block(
                idx,
                "tool_result",
                text,
                raw={"status": status, "error": error},
                tool_name=tfd.get("name"),
            )
        )
        idx += 1
    return blocks


def _attachment_blocks(bubble: dict, start_idx: int) -> list[dict]:
    """Report §4.6: real, substantial attached content -- full PDF text
    extraction, up to ~900KB per attachment. availability='text' is correct
    here (unlike the base64-image precedent elsewhere) since this is
    genuinely extracted text, not opaque binary."""
    blocks = []
    idx = start_idx
    context = bubble.get("context") or {}
    for link in context.get("externalLinks") or []:
        if not isinstance(link, dict):
            continue
        pdf_content = link.get("pdfContent")
        if pdf_content:
            blocks.append(
                make_block(
                    idx,
                    "attachment",
                    "",
                    raw={k: v for k, v in link.items() if k != "pdfContent"},
                    attachment=make_attachment(
                        filename=link.get("title") or link.get("url"),
                        mime="application/pdf",
                        content=pdf_content,
                        availability="text",
                        role="upload",
                        raw=link,
                    ),
                )
            )
            idx += 1
    return blocks


def _blocks_from_bubble(bubble: dict) -> list[dict]:
    blocks: list[dict] = []
    idx = 0

    text = bubble.get("text")
    if isinstance(text, str) and text:
        blocks.append(make_block(idx, "text", text, raw={"text": text, "richText": bubble.get("richText")}))
        idx += 1

    thinking = bubble.get("thinking")
    if isinstance(thinking, dict) and thinking.get("text"):
        blocks.append(make_block(idx, "thinking", thinking["text"], raw=thinking))
        idx += 1

    tfd = bubble.get("toolFormerData")
    if isinstance(tfd, dict) and (tfd.get("name") or tfd.get("additionalData")):
        new_blocks = _tool_blocks(tfd, idx)
        blocks.extend(new_blocks)
        idx += len(new_blocks)

    # report §4.5: codeBlocks[] largely co-occurs with toolFormerData for
    # edit tools (same edit, two views) -- only surface it as its own block
    # when there's no toolFormerData already covering this bubble, so the
    # same edit content isn't duplicated into text/embeddings twice.
    if not tfd:
        for cb in bubble.get("codeBlocks") or []:
            if isinstance(cb, dict) and cb.get("content"):
                uri = cb.get("uri") or {}
                blocks.append(
                    make_block(
                        idx,
                        "code",
                        cb["content"],
                        raw={k: v for k, v in cb.items() if k != "content"},
                        tool_name=uri.get("path") or uri.get("_fsPath"),
                    )
                )
                idx += 1

    new_blocks = _attachment_blocks(bubble, idx)
    blocks.extend(new_blocks)
    idx += len(new_blocks)

    if not blocks:
        blocks.append(make_block(0, "unknown", "", raw=bubble))

    return blocks


def _title(record: dict) -> str | None:
    composer_data = record.get("composer_data") or {}
    if composer_data.get("name"):
        return composer_data["name"]
    header = record.get("composer_header") or {}
    header_value = header.get("value") or {}
    if header_value.get("name"):
        return header_value["name"]
    search_index = record.get("search_index") or {}
    if search_index.get("title"):
        return search_index["title"]
    return None


def _model(record: dict) -> str | None:
    composer_data = record.get("composer_data") or {}
    model_config = composer_data.get("modelConfig") or {}
    model_name = model_config.get("modelName")
    if model_name and model_name != "default":
        return model_name
    cloud_meta = record.get("cloud_agent_meta") or {}
    model_details = cloud_meta.get("modelDetails") or {}
    return model_details.get("modelName") or model_name


def _project(record: dict) -> str | None:
    workspace_path = record.get("workspace_path")
    if workspace_path:
        return Path(workspace_path).name
    cloud_meta = record.get("cloud_agent_meta") or {}
    root = cloud_meta.get("workspaceRootPath")
    return Path(root).name if root else None


def parse(path: Path) -> Iterator[dict]:
    records = []
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
            records.append(rec)

    # Report §9: parent -> subagentComposerIds points one way; build the
    # reverse map so subagent conversations can carry their parent's id in
    # raw for provenance, without inventing a synthetic native_id (Cursor
    # subagent composerIds are already globally unique on their own).
    parent_of: dict[str, str] = {}
    for rec in records:
        composer_data = rec.get("composer_data") or {}
        for child_id in composer_data.get("subagentComposerIds") or []:
            parent_of[child_id] = rec["composer_id"]

    for rec in records:
        bubbles = rec.get("bubbles") or []
        bubbles_by_id = {b["bubbleId"]: b for b in bubbles if isinstance(b, dict) and b.get("bubbleId")}
        if not bubbles_by_id:
            continue

        ordered_ids = _ordered_bubble_ids(rec.get("composer_data"), bubbles_by_id)
        if not ordered_ids:
            continue

        messages = []
        timestamps = []
        model = _model(rec)
        for idx, bubble_id in enumerate(ordered_ids):
            bubble = bubbles_by_id[bubble_id]
            bubble_type = bubble.get("type")
            role = "user" if bubble_type == 1 else "assistant" if bubble_type == 2 else "unknown"
            created_at = _iso(bubble.get("createdAt"))
            if created_at:
                timestamps.append(created_at)
            blocks = _blocks_from_bubble(bubble)
            if not blocks:
                continue
            msg_model = model if role == "assistant" else None
            messages.append(make_message(idx, role, msg_model, created_at, blocks, raw=bubble))

        if not messages:
            continue

        composer_id = rec["composer_id"]
        native_id = _sanitise_native_id(composer_id)
        header = rec.get("composer_header") or {}
        title = _title(rec)
        if header.get("isSubagent"):
            subtype = header.get("subagentTypeName")
            title = f"[subagent{f':{subtype}' if subtype else ''}] {title}" if title else f"[subagent] {composer_id}"

        header_created = header.get("createdAt")
        header_updated = header.get("lastUpdatedAt")
        started_at = _iso(header_created) if header_created else (min(timestamps) if timestamps else None)
        ended_at = _iso(header_updated) if header_updated else (max(timestamps) if timestamps else started_at)

        raw_meta = dict(rec)
        if composer_id in parent_of:
            raw_meta["parent_composer_id"] = parent_of[composer_id]

        yield make_conversation(
            platform=platform,
            native_id=native_id,
            title=title,
            project=_project(rec),
            started_at=started_at,
            ended_at=ended_at,
            source_path=str(path),
            raw=raw_meta,
            messages=messages,
        )

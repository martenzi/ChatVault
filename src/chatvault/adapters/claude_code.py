from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_attachment, make_block, make_conversation, make_message

platform = "claude_code"
name = "claude_code"


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".jsonl":
        return False
    try:
        with path.open("rb") as fh:
            for i, line in enumerate(fh):
                if i >= 20:
                    # Real session files may open with a "summary" line
                    # ({"type": "summary", "summary": ..., "leafUuid": ...})
                    # which lacks sessionId/uuid/cwd/version. Scan a handful
                    # of lines rather than just the first.
                    break
                line = line.strip()
                if not line:
                    continue
                obj = orjson.loads(line)
                if not isinstance(obj, dict):
                    continue
                keys = set(obj.keys())
                # Genuine Claude Code transcript lines always co-occur with
                # sessionId, uuid, cwd and version (verified against real
                # ~/.claude/**/*.jsonl session files). Requiring all of the
                # session/uuid identity keys together (not just "type", which
                # other tools' JSONL also use) avoids false positives on
                # unrelated formats that happen to have a "type" field.
                if {"sessionId", "uuid", "cwd", "version"}.issubset(keys):
                    return True
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
        elif item_type == "fallback":
            # docs/audit-shapes/claude_code-unknown.md: 2 of this platform's 3
            # `unknown` blocks are this exact shape — `keys=from,to,type |
            # type='fallback'` (report line 12) — a model-fallback routing
            # event (`$.from.model`/`$.to.model`, 28/30 total chars, e.g.
            # "claude-fable-5" -> "claude-opus-4-8", report lines 24-25), not
            # a conversational turn. Unambiguous, same conceptual bucket as
            # identifiers.NON_MESSAGE_RECORD_TYPES's "model_change" (session
            # bookkeeping the tool itself drops; ChatVault classifies instead
            # of dropping, per spec §1/§3.3) — so `meta`, not `unknown`.
            from_model = (item.get("from") or {}).get("model")
            to_model = (item.get("to") or {}).get("model")
            text = f"model fallback: {from_model} -> {to_model}" if from_model or to_model else ""
            blocks.append(make_block(idx, "meta", text, raw=item))
        elif item_type == "document":
            # Same report, line 13 + lines 23/27-28: the remaining 1-of-3
            # `unknown` block is `keys=source,type | type='document'` with
            # `source.type='base64'`, `source.media_type='application/pdf'`
            # and `source.data` a 272,912-char base64 PDF payload — a real
            # attachment, not an ambiguous shape or a text blob to dump into
            # `text` (that would bloat `text`/embeddings with opaque base64
            # for no benefit). Classified `attachment`; the base64 bytes stay
            # exactly where they already are, in `raw` (spec §4.4 rule 4 —
            # raw_json is untouched), not duplicated into the attachment
            # payload's own `content` field.
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            media_type = source.get("media_type")
            ext = media_type.split("/")[-1] if isinstance(media_type, str) and "/" in media_type else None
            blocks.append(
                make_block(
                    idx,
                    "attachment",
                    "",
                    raw=item,
                    attachment=make_attachment(
                        filename=None,
                        mime=media_type,
                        ext=ext,
                        bytes_=len(source.get("data", "")) if isinstance(source.get("data"), str) else None,
                        content=None,
                        availability="missing",
                        role="upload",
                        raw=source,
                    ),
                )
            )
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


def _is_subagent_transcript(path: Path) -> bool:
    """Subagent transcripts (~/.claude/projects/<proj>/<parent-session-id>/subagents/agent-*.jsonl)
    carry the SAME sessionId as their parent session's own top-level file —
    verified against this machine's real files, not assumed. A naive
    parse() keyed purely on sessionId would therefore emit two conversations
    with the same native_id (parent + each subagent), which the loader then
    flags as an ambiguous duplicate (uid gets a #dup- suffix) forever,
    silently losing the subagent transcript's own real content (15-55+
    assistant turns each, seen in practice) to a review queue no one
    revisits. Detected structurally, not by filename pattern alone."""
    return "subagents" in path.parts


def parse(path: Path) -> Iterator[dict]:
    is_subagent = _is_subagent_transcript(path)
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

        title = _session_title(events)
        native_id = sid
        raw_meta = {"sessionId": sid, "cwd": cwd}
        if is_subagent:
            # path.stem is the subagent's own filename (e.g.
            # "agent-aa57f566aa5f109de"), globally unique on this machine —
            # unlike sid, which the parent session file already claims.
            native_id = f"{sid}#subagent-{path.stem}"
            title = f"[subagent] {title}" if title else f"[subagent] {path.stem}"
            raw_meta["parent_session_id"] = sid

        yield make_conversation(
            platform=platform,
            native_id=native_id,
            title=title,
            project=project,
            started_at=min(timestamps) if timestamps else None,
            ended_at=max(timestamps) if timestamps else None,
            source_path=str(path),
            raw=raw_meta,
            messages=messages,
        )

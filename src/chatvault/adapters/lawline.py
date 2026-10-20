from __future__ import annotations

from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "lawline"
name = "lawline"

# Lawline account data export (2026-10-14 file, enumerated in full on
# 2026-10-20 — see docs/reports/lawline_shapes_2026-10-17.md, whose counts
# were re-derived and matched). 13 top-level keys; only `chatSessions` is
# conversation content: [{threadId, title, createdAt, updatedAt, messages[]}]
# (no `id` key — `threadId` is unique on all 8). The other 12 keys (exportDate,
# user, sessions, accounts, tokenSubscriptions, seatAssignments, usageEvents,
# lawyerContacts, surveyResponses, chatFeedback, bugReports, autoErrorReports)
# are account/billing/telemetry and are left in the source file untouched;
# `chatFeedback` holds 0 rows, so there are no message ratings to keep.
#
# Messages use the Vercel AI SDK UI-message shape: {id, role, metadata, parts[]}
# with 16 part types, all mapped:
#   text                  -> text
#   reasoning             -> thinking
#   step-start            -> meta (bare step boundary, no payload)
#   data-progress         -> meta (UI narration of the tool call sharing its toolCallId)
#   data-system-reminder  -> injected_context (instruction injected on the system message)
#   tool-<name> (11)      -> tool_use (input) + tool_result (output)
# Tool result text is the text the model was shown (callProviderMetadata.mastra
# .modelOutput.value) when present, else output.summary; the full structured
# output (retrieved legal sources) stays in raw. Anything else -> unknown with
# its text if it has one, so a new part type is flagged rather than guessed at.

_TOOL_PREFIX = "tool-"


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        if path.stat().st_size > 200_000_000:
            return False
        raw = path.read_bytes()
        if b'"chatSessions"' not in raw:
            return False
        data = orjson.loads(raw)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("chatSessions"), list):
        return False
    sessions = data["chatSessions"]
    return bool(sessions) and all(isinstance(s, dict) and "threadId" in s and "messages" in s for s in sessions)


def _compact(value) -> str:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode("utf-8")


def _tool_result_text(part: dict) -> str:
    mo = ((part.get("callProviderMetadata") or {}).get("mastra") or {}).get("modelOutput")
    value = mo.get("value") if isinstance(mo, dict) else mo
    if isinstance(value, str) and value:
        return value
    out = part.get("output")
    if isinstance(out, dict):
        for key in ("summary", "body"):
            if isinstance(out.get(key), str) and out[key]:
                return out[key]
    return ""


def _blocks_from_parts(parts: list) -> list[dict]:
    blocks: list[dict] = []

    def add(type_: str, text: str, raw: dict, tool_name: str | None = None) -> None:
        blocks.append(make_block(len(blocks), type_, text, raw=raw, tool_name=tool_name))

    for part in parts:
        if not isinstance(part, dict):
            add("unknown", "", {"value": part})
            continue
        t = part.get("type")
        if t == "text":
            add("text", part.get("text") or "", part)
        elif t == "reasoning":
            add("thinking", part.get("text") or "", part)
        elif t == "step-start":
            add("meta", "", part)
        elif t == "data-progress":
            data = part.get("data") or {}
            steps = [s.get("label") for s in (data.get("steps") or []) if isinstance(s, dict) and s.get("label")]
            add("meta", " · ".join(x for x in [data.get("label"), *steps] if x), part)
        elif t == "data-system-reminder":
            add("injected_context", str((part.get("data") or {}).get("contents") or ""), part)
        elif isinstance(t, str) and t.startswith(_TOOL_PREFIX):
            tool = t[len(_TOOL_PREFIX):]
            add("tool_use", _compact(part.get("input") or {}), {"toolCallId": part.get("toolCallId"), "input": part.get("input")}, tool)
            add("tool_result", _tool_result_text(part), part, tool)
        else:
            add("unknown", part.get("text") if isinstance(part.get("text"), str) else "", part)
    return blocks


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    for session in data["chatSessions"]:
        messages = []
        for i, msg in enumerate(session.get("messages") or []):
            meta = msg.get("metadata") or {}
            blocks = _blocks_from_parts(msg.get("parts") or [])
            messages.append(
                make_message(i, msg.get("role") or "unknown", meta.get("modelId"), meta.get("createdAt"), blocks, raw=msg)
            )
        yield make_conversation(
            platform=platform,
            native_id=session.get("threadId"),
            title=session.get("title"),
            project=None,
            started_at=session.get("createdAt"),
            ended_at=session.get("updatedAt"),
            source_path=str(path),
            raw={k: v for k, v in session.items() if k != "messages"},
            messages=messages,
        )

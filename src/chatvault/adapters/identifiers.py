"""Shared identifier map ported from the user's vetted, reverse-engineered
JSON->Markdown converter (`_session_extractor_core_v4.0.py`, 626 lines,
`~/Desktop/history-r/tools/markdown_v4/`).

Per docs/50_SPEC_ingestion_fidelity_v1.md §3: this module is a **verbatim
port in logic** of that converter's identifier map — role normalisation,
content/thinking extraction, timestamp coercion, injected-context detection,
and safe UTF-8 decoding. Nothing here is paraphrased or "improved" beyond
what §3.3 explicitly asks for (the four new block-type names and their
mapping table, which has no equivalent in the source tool because the tool
DROPS this content instead of labelling it — see spec §1).

**Architectural inversion (spec §1):** the source tool is a converter whose
job is to decide what to DISCARD (`skip_roles`, `_SKIP_ITEM_TYPES`,
`is_boilerplate()`). ChatVault must never delete (Decision D3) — so every
"skip" list from the tool becomes a "classify as this `blocks.type`" map
here instead. Nothing in this module drops text; callers get back a
classification and can choose what a given export profile shows.

**Scope note — Phase 2 only.** This module is standalone and, as of this
commit, wired to nothing: no adapter, no `ingest.py`, imports it yet. That
wiring (routing every adapter's role/content/timestamp handling through
here) is Phase 3 (spec §4), a separate pass with its own per-platform
evidence requirement. Phase 2's job is only to make this module correct
and independently tested before anything depends on it.

**Never use `errors="ignore"`.** `decode_utf8_counting_replacements()`
exists specifically to replace that pattern everywhere in the ingest path
(spec §3.1, §7.8) — invalid bytes must become visible (U+FFFD, counted),
never silently vanish.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

# ── Compiled patterns (ported verbatim from the core's module-level regexes) ──

_LONG_B64 = re.compile(r"^[A-Za-z0-9+/]{60,}={0,2}$")
_HEX_LONG = re.compile(r"^[0-9a-fA-F]{60,}$")

_METADATA_RE = re.compile(
    r'(?:Conversation info|Sender)\s*\(untrusted metadata\):\s*```json.*?```\s*',
    re.DOTALL | re.IGNORECASE,
)
_TS_RE = re.compile(r'^\[[\w\s,:/+\-]+\]\s*', re.MULTILINE)
_GEMINI_REF_RE = re.compile(
    r'\n*-{3}\s*Content from referenced files\s*-{3}.*?-{3}\s*End of content\s*-{3}\s*',
    re.DOTALL | re.IGNORECASE,
)

# ── Collections / non-message record types (ported from COLLECTION_KEYS and
# _BASE_SKIP_TYPES) ────────────────────────────────────────────────────────

# Where the core descends when a record is a container rather than a leaf
# message. Ported verbatim from `COLLECTION_KEYS`.
COLLECTION_KEYS: tuple[str, ...] = (
    "messages", "items", "turns", "conversation", "conversations", "entries", "mapping",
)

# Ported verbatim from `_BASE_SKIP_TYPES`. The core drops records of these
# types entirely (they are session/turn bookkeeping, not conversational
# turns). Per spec §1 / §3.1, ChatVault does not drop — a caller routing a
# record whose `type` is in this set should classify it as
# `blocks.type='meta'` (spec §3.3) rather than emit an ordinary message, or
# skip emitting a message while keeping the record in the conversation's
# `raw_json`. This module does not decide which; it only identifies the
# record types that qualify, so a wired-up adapter (Phase 3) can choose.
NON_MESSAGE_RECORD_TYPES: set[str] = {
    "session", "session_meta", "event_msg", "turn_context",
    "thinking_level_change", "model_change", "custom", "info",
}

# Ported verbatim from `_SKIP_ITEM_TYPES`, inverted per spec §1: the core
# drops content items of these types; ChatVault classifies them into a
# `blocks.type` instead. `refusal` and `audio` map to the two new block
# types spec §3.3 adds specifically for them. The remaining mappings are
# this port's own judgment call (the source tool has no equivalent target
# vocabulary to copy, since it discards these items) reusing chatvault's
# existing `tool_use` / `tool_result` / `image` / `error` types where the
# item name plainly indicates which side of a tool call it is.
CONTENT_ITEM_TYPE_MAP: dict[str, str] = {
    "tool_call": "tool_use",
    "tool_result": "tool_result",
    "tool_use": "tool_use",
    "function_call": "tool_use",
    "function_call_output": "tool_result",
    "function_result": "tool_result",
    "custom_tool_call": "tool_use",
    "custom_tool_call_output": "tool_result",
    "image": "image",
    "image_url": "image",
    "input_image": "image",
    "output_image": "image",
    "audio": "audio",
    "refusal": "refusal",
    "error": "error",
    "toolcall": "tool_use",
}

# ── Role normalisation (ported verbatim from `norm_role()`) ───────────────

_ROLE_SYNONYMS = {
    "human": "user",
    "assistant_response": "assistant",
    "gemini": "assistant",
    "bot": "assistant",
    "ai": "assistant",
}


def normalise_role(value: Any) -> str:
    """Port of `norm_role()`. Handles the dict-valued-role case (`role` /
    `name` / `type` keys), the synonym map, and case/whitespace folding."""
    role = value
    if isinstance(role, dict):
        role = role.get("role") or role.get("name") or role.get("type")
    if role is None:
        return "unknown"
    v = str(role).strip().lower()
    return _ROLE_SYNONYMS.get(v, v or "unknown")


# ── Timestamp coercion (ported verbatim from `coerce_dt()`) ────────────────


def coerce_timestamp(value: Any) -> datetime | None:
    """Port of `coerce_dt()`: epoch-seconds vs milliseconds disambiguation
    at the `1_000_000_000_000` boundary, digit-string handling, and
    ISO-8601 with trailing `Z`."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        # bool is a subclass of int; the source never intends to accept it
        # as a timestamp and neither should this port.
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1_000_000_000_000:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return coerce_timestamp(int(raw))
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone() if dt.tzinfo else dt
        except ValueError:
            return None
    return None


# ── Content / thinking extraction (ported verbatim from `extract_content()`
# and `extract_thinking()`) ─────────────────────────────────────────────────


def strip_referenced_file_dump(text: str) -> str:
    """Port of `strip_gemini_refs()`: strips Gemini-CLI's
    `--- Content from referenced files --- ... --- End of content ---` dump."""
    return _GEMINI_REF_RE.sub("", text).strip() if text else text


def strip_metadata_envelope(text: str) -> str:
    """Port of `strip_meta()`: strips the `Conversation info`/`Sender`
    "untrusted metadata" JSON envelope and leading `[timestamp]` line
    prefixes. Text only — never applied to `raw_json` storage."""
    text = _METADATA_RE.sub("", text)
    return _TS_RE.sub("", text).strip()


def _first_of(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def extract_content(content: Any) -> tuple[str, str | None]:
    """Port of `extract_content()`: the str / list-of-dicts / dict
    tri-branch, including the `itype == ""` case (Gemini-CLI `{text: "..."}`
    items with no `type` field) and the nested `content.content` recursion.

    Returns (dialogue_text, thinking_text_or_None).
    """
    if content is None:
        return ("", None)
    if isinstance(content, str):
        return (strip_referenced_file_dump(content), None)

    if isinstance(content, list):
        parts: list[str] = []
        thinking: str | None = None
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = str(item.get("type", "")).lower().strip()
            if itype in CONTENT_ITEM_TYPE_MAP:
                continue
            if itype == "text":
                t = item.get("text", "").strip()
                if t:
                    parts.append(t)
            elif itype in ("thinking", "reasoning"):
                thinking = (
                    item.get("thinking") or item.get("text")
                    or item.get("redacted_thinking") or ""
                ).strip() or thinking
            elif itype == "":
                # Gemini CLI format: {text: "..."} with no type field.
                t = item.get("text", "").strip()
                if (
                    t
                    and not re.match(
                        r'^-{3}\s*(Content from referenced files|End of content)', t, re.I
                    )
                    and not re.match(r'^Content from @', t)
                ):
                    parts.append(t)
        dialogue = strip_referenced_file_dump("\n\n".join(p for p in parts if p))
        return (dialogue.strip(), thinking)

    if isinstance(content, dict):
        ct = str(content.get("type", "")).lower().strip()
        if ct == "text" and "text" in content:
            return (content["text"], None)
        if ct in ("input_text", "output_text"):
            return (content.get("text", ""), None)
        if isinstance(content.get("content"), list):
            return extract_content(content["content"])
    return ("", None)


def extract_thinking(rec: dict) -> str | None:
    """Port of `extract_thinking()`: searches `thoughts`/`thinking`/
    `reasoning` top-level keys, falls back to a nested content item typed
    `thinking`/`reasoning` (including `redacted_thinking`), and handles the
    list-of-dicts `{subject, description}` shape."""
    think = _first_of(rec, "thoughts", "thinking", "reasoning")
    if think is None:
        content = rec.get("content")
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") in ("thinking", "reasoning"):
                think = item.get("thinking") or item.get("text") or item.get("redacted_thinking")
                break
    if think is None:
        return None
    if isinstance(think, list):
        parts = []
        for t in think:
            if isinstance(t, dict):
                subj = t.get("subject") or t.get("name") or ""
                desc = t.get("description") or t.get("text") or ""
                parts.append(f"**{subj}**\n\n{desc}" if subj and desc else desc or subj)
            elif isinstance(t, str):
                parts.append(t)
        return "\n\n".join(p for p in parts if p) or None
    return str(think) if think else None


# ── Text cleaning (ported verbatim from `clean()`) ──────────────────────────


def clean_text(text: str) -> str:
    """Port of `clean()`: CRLF normalisation, NUL strip, `<final>` tag
    strip, blank-line collapse, and drops for `thinkingsignature` lines and
    long (>=60 char) base64/hex leak lines.

    Applies to extracted `text` only. Never touch `blocks.raw_json` with
    this — raw storage keeps the original bytes (spec §3.1).
    """
    text = (
        text.replace("<final>", "").replace("</final>", "")
        .replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    )
    lines: list[str] = []
    prev_blank = False
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            if not prev_blank and lines:
                lines.append("")
            prev_blank = True
            continue
        if "thinkingsignature" in s.lower():
            continue
        if _LONG_B64.match(s) or _HEX_LONG.match(s):
            continue
        lines.append(raw.rstrip())
        prev_blank = False
    while lines and not lines[-1].strip():
        lines.pop()
    cleaned = "\n".join(lines).strip()
    return "" if cleaned.upper() in {"N/A", "NA", "NULL", "NONE"} else cleaned


# ── Injected-context detection (ported verbatim from `boilerplate_chat()`'s
# marker list, `_is_tool_json()`, `_is_ts_listing()`) ───────────────────────

# Ported verbatim from `boilerplate_chat()`'s substring marker list. All
# lowercase, matched against `text.lower()`. The Windsurf
# `<system-retrieved-memory[` entry is the v4.0 fix documented in
# EXTRACTION_v4.0_CHANGELOG.txt (spec §3.2 regression test #1).
INJECTED_CONTEXT_MARKERS: tuple[str, ...] = (
    "# agents.md instructions",
    "<instructions>",
    "<environment_context>",
    "<permissions_instructions>",
    "---\n*scout ·",
    "---\n*orchestrator ·",
    "---\n*archivist ·",
    "# user.md",
    "# soul.md",
    "# identity.md",
    "# memory.md",
    "<system-retrieved-memory[",
)


def _is_tool_json(text: str) -> bool:
    """Port of `_is_tool_json()`: detects a `{results|tool_use_id}` JSON
    body pasted into a text field."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(obj, dict) and ("results" in obj or "tool_use_id" in obj)


def _is_ts_listing(text: str, lowered: str) -> bool:
    """Port of `_is_ts_listing()`."""
    return (
        text.startswith("healthz")
        or "params.ts" in lowered
        or ("apiV1" in text and text.count(".ts") > 5)
    )


def is_injected_context(role: str, text: str) -> bool:
    """Port of `boilerplate_chat()`. In the source tool this decides
    whether CHAT mode drops the message; per spec §3.3 that verdict is
    inverted into a `blocks.type='injected_context'` classification —
    retained, hidden by default (shown only in `full`), never dropped."""
    if role not in ("user", "assistant", "unknown"):
        return False
    lowered = text.lower()
    for marker in INJECTED_CONTEXT_MARKERS:
        if marker in lowered:
            return True
    return _is_tool_json(text) or _is_ts_listing(text, lowered)


# ── Safe UTF-8 decoding (ported verbatim from `read_records()`'s decode
# step) ──────────────────────────────────────────────────────────────────


def decode_utf8_counting_replacements(raw: bytes) -> tuple[str, int]:
    """Port of the decode step inside `read_records()`.

    Decodes `raw` as UTF-8 using `errors="replace"` — **never**
    `errors="ignore"`, which silently deletes invalid bytes with no trace.
    Returns (decoded_text, replacement_count) so callers can surface a
    warning (spec §3.4) instead of losing bytes silently (spec §7.8).
    """
    text = raw.decode("utf-8", errors="replace")
    return text, text.count("�")

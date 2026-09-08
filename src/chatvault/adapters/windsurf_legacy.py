from __future__ import annotations

from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

import orjson

from chatvault.adapters import identifiers
from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "windsurf"
name = "windsurf_legacy"

# Bucket the reverse-engineered Cascade role vocabulary into CIR's role/block
# shape. Source: tools/cascade_decrypt/cascade_export_full.py TYPE_ROLE map —
# this is the *_full extraction (protobuf-wire-full-v1), not the thin
# dialogue-only windsurf_legacy/ export (protobuf-wire-v1), which lacks tool
# calls, per-message model, and most of these role types entirely.
_ROLE_TO_MESSAGE_ROLE = {
    "user": "user",
    "assistant": "assistant",
    "tool_call": "tool",
    "tool_result": "tool",
    "tool_error": "tool",
    "planner": "system",
    "context": "system",
    "memory": "system",
    "system": "system",
}

_ROLE_TO_BLOCK_TYPE = {
    "user": "text",
    "assistant": "text",
    "tool_call": "tool_use",
    "tool_result": "tool_result",
    "tool_error": "error",
    "planner": "thinking",
    "context": "attachment",
    "memory": "system",
    "system": "system",
}

# Phase 3 fix #1 — spec §4.4 / docs/audit-shapes/windsurf-unknown.md.
#
# The 727-record `unknown` corpus is raw protobuf enum leakage: 14 distinct
# shapes, all keyed on a numeric `role="type_NN"` (no `role` string field on
# the record at all, so `parse()` falls back to `f"type_{msg_type}"`). Of
# those 14 shapes, exactly one is unambiguous enough to alias to a known
# logical role instead of leaving as an anonymous enum value:
#
#   `type_84` (msg_type 84) — 11 occurrences — is the *only* numeric shape
#   carrying `tool_name`/`tool_id`/`tool_args` (11/11/11 occurrences,
#   evidenced in the "Leaf key paths" table), and unlike the literal
#   `tool_call` role elsewhere in this map, it *also* carries `content`/
#   `text` holding the tool's own output (e.g. a generated code-map JSON
#   blob), not dialogue. That is a completed tool invocation with its
#   result attached — the same shape `tool_result` already captures for the
#   literal-role records — so it is aliased to `tool_result` rather than
#   invented a role of its own.
#
# Every other numeric shape (`type_6/33/38/43/44/52/89/90/99` — 716 of the
# 727 records, per the "Shape signatures" table) has no field beyond
# `content`/`text`/`model`/`step_id`/`ts` to discriminate its meaning from
# plain dialogue, tool traffic, planner output, or session bookkeeping.
# Per spec §4.4 rule 2, guessing a role for those would be a confident wrong
# label, not a gap — worse than `unknown`. They are deliberately left
# unaliased: `block_type` falls through to `unknown` and `_block_text()`'s
# existing `content`/`text` fallback (used for every role, not just mapped
# ones) still populates `text` from the richest available leaf when one
# exists (`$.content`/`$.text`, 78 of the 727 occurrences) — satisfying
# "populate text from the best available leaf" without inventing a type.
# `chatvault.tags.flag_shape_unclear()` (wired into `ingest_path()`) tags the
# conversation `review/shape-unclear` automatically whenever any block in it
# is still `unknown` after this — no adapter-side call needed here.
_TYPE_NN_ROLE_ALIAS: dict[str, str] = {
    "type_84": "tool_result",
}


def sniff(path: Path) -> bool:
    """Every meta line (thin or full extraction) self-declares its origin via
    "source": "windsurf_legacy_cascade" (see EXTRACTION_METHOD.md). Trust that
    explicit tag first; fall back to the structural shape in case a future
    export drops or renames the source string."""
    if path.suffix.lower() != ".jsonl":
        return False
    try:
        with path.open("rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = orjson.loads(line)
                if not isinstance(obj, dict) or obj.get("type") != "meta":
                    return False
                if obj.get("source") == "windsurf_legacy_cascade":
                    return True
                return "conversation_id" in obj and "cascade_file" in obj
    except (orjson.JSONDecodeError, OSError):
        return False
    return False


def _iso_z(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.endswith("Z") else value + "Z"


def _block_text(rec: dict, role: str) -> str:
    text = rec.get("text") or rec.get("content")
    if text:
        return text
    if role == "tool_call" and rec.get("tool_args"):
        return rec["tool_args"]
    payloads = rec.get("payloads")
    if payloads:
        return payloads[0]
    return ""


def _title_from_first_user_message(records: list[dict]) -> str | None:
    for rec in records:
        if rec.get("role") == "user":
            text = rec.get("text") or rec.get("content")
            if text:
                return text.strip().splitlines()[0][:120]
    return None


# Phase 3 fix #2 — spec §7.9 / identifiers.is_injected_context() docstring
# ("The Windsurf `<system-retrieved-memory[` entry is the v4.0 fix..."), and
# confirmed directly against production: of every windsurf block whose
# `content`/`text` contains the literal `<SYSTEM-RETRIEVED-MEMORY[` marker,
# 100% carry raw `role` "memory" (77 occurrences) or "context" (28
# occurrences) — no other role does. Those roles currently bucket to
# `blocks.type` "system"/"attachment" respectively (see _ROLE_TO_BLOCK_TYPE),
# which is not the same as "hidden below `full`, retained, never dropped"
# that `injected_context` specifically guarantees (spec §3.3, acceptance
# §7.9). `is_injected_context()`'s own role gate (`user`/`assistant`/
# `unknown`) is tuned for chat-shaped roles, not Cascade's memory/context
# vocabulary, so it is called with role="unknown" here deliberately — this
# is a content check (the marker itself), not a role check; the role gate
# exists in that function only to skip roles that can never be dialogue
# (e.g. pure tool plumbing), which memory/context records are not.
def _maybe_injected_context(block_type: str, text: str) -> str:
    if text and identifiers.is_injected_context("unknown", text):
        return "injected_context"
    return block_type


def _build_blocks(rec: dict, role: str, default_block_type: str) -> list[dict]:
    """Almost always one block per record. The one exception: an assistant
    turn carrying both a `thinking` field (Claude extended-thinking trace,
    additively exported by the history-r pbwire fix) and separate final-answer
    text — those get a dedicated `thinking` block ahead of the text block,
    surfacing content this adapter previously discarded entirely (present
    only in `raw`, never in a block's own text/type).

    A turn whose *only* text is its thinking trace (no separate final answer)
    isn't duplicated into two blocks: `_block_text`'s payloads[0] fallback
    already resolves to that same string post-fix, so using `thinking`
    directly just gives it the correct `thinking` type instead of the
    generic `text` type it would otherwise fall into.

    Every extracted string is routed through `identifiers.clean_text()`
    (NUL/CRLF normalisation, thinking-signature and long base64/hex leak-line
    stripping — ported verbatim from the vetted converter's `clean()`) before
    becoming a block's `text`; `raw_json` (passed separately as `raw=rec`)
    is untouched, per spec §4.4 rule 4."""
    thinking = rec.get("thinking") if role == "assistant" else None
    primary_text = rec.get("text") or rec.get("content")
    tool_name = rec.get("tool_name") if role in ("tool_call", "tool_result", "tool_error") else None

    blocks = []
    if thinking and primary_text:
        blocks.append(make_block(len(blocks), "thinking", identifiers.clean_text(thinking), raw=rec))
        text_type = _maybe_injected_context(default_block_type, primary_text)
        blocks.append(make_block(len(blocks), text_type, identifiers.clean_text(primary_text), raw=rec, tool_name=tool_name))
    elif thinking:
        blocks.append(make_block(len(blocks), "thinking", identifiers.clean_text(thinking), raw=rec))
    else:
        text = _block_text(rec, role)
        text_type = _maybe_injected_context(default_block_type, text)
        blocks.append(make_block(len(blocks), text_type, identifiers.clean_text(text), raw=rec, tool_name=tool_name))
    return blocks


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

    messages = []
    for idx, rec in enumerate(records):
        role_field = rec.get("role")
        # `identifiers.normalise_role()` only case/whitespace-folds and
        # applies a small chat-role synonym table (human->user etc.) — none
        # of those synonyms appear in windsurf-unknown.md's evidence, and
        # windsurf's literal role strings ("user", "tool_call", "planner",
        # ...) are this adapter's own vetted bucketing, so it adds no value
        # to force-map through it. It IS applied defensively here (case/
        # whitespace folding only) so a stray-cased literal role still hits
        # the dicts below instead of silently falling through to the
        # `type_NN` numeric fallback, which normalise_role() cannot resolve
        # (it has no `msg_type` to fall back to) — hence the `role_field`
        # truthiness guard before calling it.
        raw_role = identifiers.normalise_role(role_field) if role_field else f"type_{rec.get('msg_type')}"
        # See _TYPE_NN_ROLE_ALIAS above: the one evidenced numeric-role ->
        # logical-role alias (type_84 -> tool_result). Every other raw_role
        # (literal or type_NN) passes through unchanged.
        lookup_role = _TYPE_NN_ROLE_ALIAS.get(raw_role, raw_role)
        msg_role = _ROLE_TO_MESSAGE_ROLE.get(lookup_role, "system")
        block_type = _ROLE_TO_BLOCK_TYPE.get(lookup_role, "unknown")

        blocks = _build_blocks(rec, lookup_role, block_type)

        created_at = _iso_z(rec.get("iso"))
        messages.append(make_message(idx, msg_role, rec.get("model"), created_at, blocks, raw=rec))

    conversation_id = meta.get("conversation_id")
    project_guess = meta.get("project_path_guess")
    project = Path(unquote(project_guess)).name if project_guess else None
    title = meta.get("title") or _title_from_first_user_message(records)

    yield make_conversation(
        platform=platform,
        native_id=conversation_id,
        title=title,
        project=project,
        started_at=_iso_z(meta.get("started")),
        ended_at=_iso_z(meta.get("ended")),
        source_path=str(path),
        raw=meta,
        messages=messages,
    )

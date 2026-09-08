from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters import identifiers
from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "vscode"
name = "vscode_legacy"

# Source: tools/corpus_full/export_vscode_full.py. One JSONL per VS Code
# Copilot chatSession: line 0 is meta (extraction="vscode-chatsessions-full-v1"),
# following lines are one record per request/response event. Role vocabulary
# observed in the real corpus: user, assistant (kind=text or a progress/
# control kind like confirmation/command/inlineReference), tool (kind in
# toolInvocationSerialized/prepareToolInvocation/textEditGroup), thinking,
# and result (the whole per-request result blob, no top-level text of its
# own — see _blocks_for_result()).
#
# Evidence base for every branch below (per docs/50_SPEC_ingestion_fidelity_v1.md
# §4.4 rule 1): docs/audit-shapes/vscode-unknown.md (6,288 unknown blocks,
# 11 distinct shapes, enumerated over every record — not sampled) and the
# vscode row of docs/audit-shapes/reasoning-sweep.md.


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
                return (
                    isinstance(obj, dict)
                    and obj.get("type") == "meta"
                    and obj.get("extraction") == "vscode-chatsessions-full-v1"
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


def _tool_name(part: dict) -> str | None:
    return part.get("toolId") or part.get("toolCallId")


def _tool_fallback_text(part: dict) -> str:
    inv = part.get("invocationMessage")
    if isinstance(inv, dict):
        return inv.get("value") or ""
    return ""


def _join(parts: list[str | None], sep: str = "\n\n") -> str:
    return sep.join(p.strip() for p in parts if p and p.strip())


# ── role='result' record decomposition ──────────────────────────────────
#
# spec §4.3 quotes the old catch-all verbatim: `role == "result"` records
# were labelled `unknown` with `text` read from `rec.get("text") or
# rec.get("content")` — neither key exists on these records (confirmed:
# `$.text`/`$.content` are always present-but-empty for the whole unknown
# corpus, see the "Zero-volume leaf paths" section of vscode-unknown.md).
# The real payload sits under `result.metadata.*`, which is not one thing
# but a bundle of several distinct kinds of content produced by one VS Code
# Copilot request. One block per record can't honestly represent a bundle,
# so this emits 0+ typed blocks per 'result' record — make_message() already
# accepts a list of blocks, so this is not a schema change.


def _extract_tool_call_result_text(node: object) -> str:
    """Recursively pulls text out of the `toolCallResults` dynamic
    per-call-id UI "node tree" shape.

    Evidence (vscode-unknown.md): dozens of dynamically-keyed paths of the
    form `$.result.metadata.toolCallResults.<call_id>__vscode-<ts>.content[]
    .value(.node.children[]...).text`, individually up to 1,816,401 chars
    (e.g. `call_92255688__vscode-1762520362919...` ) and collectively the
    single largest unaddressed content family in the report — web-fetch and
    command-output tool results rendered as a UI node tree instead of a
    flat string, so no fixed key path can name them (the call id is
    per-record). This walks the shape structurally instead of by key name.
    """
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        parts: list[str] = []
        text = node.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
        value = node.get("value")
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, (dict, list)):
            parts.append(_extract_tool_call_result_text(value))
        nested = node.get("node")
        if isinstance(nested, dict):
            parts.append(_extract_tool_call_result_text(nested))
        children = node.get("children")
        if isinstance(children, list):
            parts.append(_extract_tool_call_result_text(children))
        return _join(parts, "")
    if isinstance(node, list):
        return _join([_extract_tool_call_result_text(x) for x in node], "")
    return ""


def _tool_call_results_text(metadata: dict) -> str:
    results = metadata.get("toolCallResults")
    if not isinstance(results, dict):
        return ""
    chunks: list[str] = []
    for payload in results.values():
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            text = _extract_tool_call_result_text(item)
            if text.strip():
                chunks.append(text.strip())
    return _join(chunks)


def _blocks_for_result(rec: dict) -> list[tuple[str, str, str | None]]:
    """Returns a list of (block_type, text, tool_name) for a role='result'
    record. Each branch cites its evidence from vscode-unknown.md."""
    result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    blocks: list[tuple[str, str, str | None]] = []

    # Evidence: `$.result.metadata.renderedUserMessage[].text` — 881
    # occurrences, 7,348,600 chars. Content is the fully rendered prompt
    # sent to the model (starts with `<context>`/`<editorContext>`
    # environment info) ahead of the user's actual turn for that request —
    # matches spec §3.3's `injected_context` definition and §1's "full"-tier
    # description of "system/start prompts fed from external rules files
    # before the user's prompt".
    rendered = metadata.get("renderedUserMessage")
    if isinstance(rendered, list):
        text = _join([i.get("text", "") for i in rendered if isinstance(i, dict)])
        if text:
            blocks.append(("injected_context", text, None))

    rounds = metadata.get("toolCallRounds")

    # Evidence: `$.result.metadata.toolCallRounds[].thinking.text` — 1,156
    # occurrences (1,080 non-empty), 1,066,716 chars of real reasoning
    # content (e.g. "**Analyzing Thesis Discussion Section** I'm focused
    # on the Discussion section..."). This is exactly the reasoning content
    # spec §1.1 warns providers file under unexpected identifiers — here it
    # was typed `unknown`, which is worse than misfiled: invisible even in
    # `full`.
    if isinstance(rounds, list):
        thoughts = [
            rnd["thinking"]["text"]
            for rnd in rounds
            if isinstance(rnd, dict)
            and isinstance(rnd.get("thinking"), dict)
            and isinstance(rnd["thinking"].get("text"), str)
            and rnd["thinking"]["text"].strip()
        ]
        thinking_text = _join(thoughts)
        if thinking_text:
            blocks.append(("thinking", thinking_text, None))

    # Evidence: `$.result.metadata.edits.request` (190 occ, 1,776,561 chars),
    # `.workingSet[].text` (462 occ, 14,712,731 chars — the single largest
    # leaf in the whole report) and `.response` (190 occ, 382,426 chars).
    # This is VS Code's "edit agent" round: workingSet is file content given
    # to the model as tool context, request/response are the edit
    # instruction and the model's applied-edit summary — a tool call+result
    # pair, not dialogue, hence `tool_result` rather than `text`.
    edits = metadata.get("edits")
    if isinstance(edits, dict):
        working_set = edits.get("workingSet")
        ws_text = (
            _join([w.get("text", "") for w in working_set if isinstance(w, dict)])
            if isinstance(working_set, list)
            else ""
        )
        edit_text = _join([edits.get("request"), ws_text, edits.get("response")])
        if edit_text:
            blocks.append(("tool_result", edit_text, "vscode.editsAgent"))

    # Evidence: see _extract_tool_call_result_text() docstring above.
    tcr_text = _tool_call_results_text(metadata)
    if tcr_text:
        blocks.append(("tool_result", tcr_text, "vscode.toolCallResults"))

    # Evidence: `$.result.metadata.summary.text` — 59 occurrences, 532,714
    # chars; content is literally "<analysis> [Chronological Review: The
    # conversation began with...]" — a conversation-compaction summary,
    # matching spec §1's "full"-tier example "compaction events" verbatim.
    summary = metadata.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("text"), str) and summary["text"].strip():
        blocks.append(("system", summary["text"], None))

    # Evidence: `$.result.metadata.toolCallRounds[].response` (5,970 occ,
    # 1,872,857 chars, e.g. "Yes, I can identify the departments for the job
    # roles listed on xAI's careers page...") and
    # `$.result.metadata.messages[].content` (386 occ, 457,477 chars) — the
    # assistant's actual natural-language reply for this turn. This is the
    # primary dialogue content that was previously dropped as `unknown`.
    response_parts: list[str] = []
    if isinstance(rounds, list):
        for rnd in rounds:
            if isinstance(rnd, dict) and isinstance(rnd.get("response"), str) and rnd["response"].strip():
                response_parts.append(rnd["response"])
    messages = metadata.get("messages")
    if isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict) and isinstance(m.get("content"), str) and m["content"].strip():
                response_parts.append(m["content"])
    response_text = _join(response_parts)
    if response_text:
        blocks.append(("text", response_text, None))

    if not blocks:
        # Per spec §4.4 rule 2: no evidenced content-bearing leaf matched on
        # this record (e.g. `result.metadata` is `{}` or carries only
        # `timings`, as in the pure-latency-report case) — do not guess a
        # type. Keep `unknown` but still surface the richest thing we have
        # so the block isn't silently empty; the caller tags the
        # conversation `review/shape-unclear` (see parse()).
        fallback = json.dumps(result, default=str)[:2000] if result else ""
        blocks.append(("unknown", fallback, None))

    return blocks


# ── role='assistant', kind=<control/progress kind> decomposition ────────
#
# Evidence: the "Shape signatures" table in vscode-unknown.md lists exactly
# 10 `kind` values under `role='assistant'` (2,008 + 1,166 + 1,096 + 669 +
# 100 + 34 + 13 + 4 + 2 + 1 = 5,093 records — matching `$.kind`'s occurrence
# count exactly). `$.text`/`$.content` are always empty for all of them
# (see "Zero-volume leaf paths"), which is why the old catch-all
# (`"text" if rec.get("text") else "unknown"`) always fell through to
# `unknown` for every one of these.


def _blocks_for_assistant_kind(kind: str, part: dict) -> tuple[str, str]:
    """Returns (block_type, text) for a role='assistant' record identified
    by its `kind`. Falls back to ('unknown', '') for any kind not in the
    11-shape inventory the audit enumerated, so a genuinely new shape still
    surfaces honestly rather than being guessed at."""

    if kind == "inlineReference":
        # Evidence: `part.inlineReference.path` (1,886 occ, 150,265 chars),
        # `.external` (1,876 occ, 179,523 chars), `.fsPath` (1,438 occ,
        # 124,697 chars), `.name` (120 occ, 1,951 chars) — a file/symbol
        # reference the model attached (an @-mention style context chip).
        ref = part.get("inlineReference") if isinstance(part.get("inlineReference"), dict) else {}
        uri = ref.get("location", {}).get("uri", {}) if isinstance(ref.get("location"), dict) else {}
        text = ref.get("name") or ref.get("path") or ref.get("external") or ref.get("fsPath") or uri.get("path") or ""
        return "attachment", text

    if kind == "codeblockUri":
        # Evidence: `part.uri.external` (1,166 occ, 115,313 chars),
        # `.path` (1,166 occ, 101,651 chars), `.fsPath` (1,157 occ,
        # 101,114 chars) — a link to the resource a generated code block
        # refers to.
        uri = part.get("uri") if isinstance(part.get("uri"), dict) else {}
        text = uri.get("path") or uri.get("external") or uri.get("fsPath") or ""
        return "attachment", text

    if kind == "undoStop":
        # Evidence: `part.id` (1,096 occ, always exactly a 36-char uuid,
        # 39,456 chars total = 1,096 * 36) is the record's only content —
        # a checkpoint marker for VS Code's undo stack, not conversational
        # turn content. Session bookkeeping -> `meta` (spec §3.3).
        return "meta", ""

    if kind == "mcpServersStarting":
        # Evidence: `part.didStartServerIds` is `empty_list` for all 669
        # occurrences — genuinely zero recoverable content, a pure status
        # marker.
        return "meta", "[MCP servers starting]"

    if kind in ("progressTaskSerialized", "progressMessage"):
        # Evidence: `part.progress` is `empty_list` for all 104 occurrences
        # spanning these two kinds (100 + 13, plus 4 for progressTask
        # handled separately below since that one *does* carry content) —
        # a UI progress-spinner event with no text payload.
        return "meta", "[progress update]"

    if kind == "progressTask":
        # Evidence: `part.deferred.d.value` (4 occ, 124 chars, example
        # "Summarized conversation history") — matches spec §1's "full"-tier
        # "compaction events" example, unlike its content-free siblings
        # above.
        deferred = part.get("deferred") if isinstance(part.get("deferred"), dict) else {}
        d = deferred.get("d") if isinstance(deferred.get("d"), dict) else {}
        return "system", d.get("value") or ""

    if kind == "confirmation":
        # Evidence: `part.title` (34 occ, 807 chars, example "Continue to
        # iterate?") — an interactive confirmation prompt shown mid-turn.
        return "system", part.get("title") or ""

    if kind == "elicitation":
        # Evidence: `part.title.value` (2 occ, 79 chars, example "Continue
        # waiting for `./process_thesis_pdfs.sh`?").
        title = part.get("title") if isinstance(part.get("title"), dict) else {}
        return "system", title.get("value") or ""

    if kind == "command":
        # Evidence: `part.command.title` (1 occ, 14 chars, "Create File...").
        command = part.get("command") if isinstance(part.get("command"), dict) else {}
        return "system", command.get("title") or ""

    return "unknown", ""


def _classify(rec: dict) -> tuple[str, str]:
    """Returns (message_role, block_type) for the SIMPLE (single-block)
    cases only — role='result' and role='assistant' kind-records go through
    _blocks_for_result()/_blocks_for_assistant_kind() in parse() instead,
    since they can require more than one block per record."""
    role = identifiers.normalise_role(rec.get("role"))
    if role == "user":
        return "user", "text"
    if role == "thinking":
        return "assistant", "thinking"
    if role == "tool":
        return "tool", "tool_use"
    if role == "result":
        return "system", "unknown"
    # role == "assistant" (kind=="text" or any progress/control kind)
    return "assistant", "text" if rec.get("text") else "unknown"


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
        role = identifiers.normalise_role(rec.get("role"))
        msg_role, block_type = _classify(rec)
        blocks: list[dict] = []

        if role == "result":
            for i, (btype, text, tool_name) in enumerate(_blocks_for_result(rec)):
                blocks.append(make_block(i, btype, identifiers.clean_text(text), raw=rec, tool_name=tool_name))

        elif role == "assistant" and rec.get("kind") and not rec.get("text"):
            btype, text = _blocks_for_assistant_kind(str(rec.get("kind")), rec.get("part") or {})
            blocks.append(make_block(0, btype, identifiers.clean_text(text), raw=rec))

        elif role == "tool":
            part = rec.get("part") or {}
            tool_name = _tool_name(part)
            text = rec.get("text") or rec.get("content") or _tool_fallback_text(part)
            # Evidence (docs/audit-shapes/reasoning-sweep.md, vscode row):
            # `$.part.toolSpecificData.rawInput.thought` — 8 occurrences,
            # 12,776 chars, currently absorbed into the tool_use block's raw
            # JSON with no `thinking` block emitted. ReAct-style tool calls
            # carry a scratchpad "thought" alongside the real arguments;
            # spec §1.1 says block type must reflect content, not the
            # role/kind it happened to be filed under.
            raw_input = (part.get("toolSpecificData") or {}).get("rawInput")
            thought = raw_input.get("thought") if isinstance(raw_input, dict) else None
            if isinstance(thought, str) and thought.strip():
                blocks.append(make_block(0, "thinking", identifiers.clean_text(thought), raw=rec))
            blocks.append(
                make_block(len(blocks), "tool_use", identifiers.clean_text(text), raw=rec, tool_name=tool_name)
            )

        else:
            text = rec.get("text") or rec.get("content") or ""
            blocks.append(make_block(0, block_type, identifiers.clean_text(text), raw=rec))

        messages.append(make_message(idx, msg_role, rec.get("model"), _iso_z(rec.get("iso")), blocks, raw=rec))

    conversation = make_conversation(
        platform=platform,
        native_id=meta.get("session_id"),
        title=_clean(meta.get("title"), "untitled"),
        project=_clean(meta.get("project"), "unknown"),
        started_at=_iso_z(meta.get("created")),
        ended_at=_iso_z(meta.get("updated")),
        source_path=str(path),
        raw=meta,
        messages=messages,
    )
    # Per spec §4.4 rule 2 / §7.6: every remaining `unknown` block must
    # either have populated text or its conversation must carry
    # `review/shape-unclear`. There is no adapter-level tags hook in the
    # CIR (`make_conversation` has no tags field, and no existing adapter
    # tags at parse time) — the one existing precedent for a per-conversation
    # review flag, `chatvault.tags.flag_model_unknown()`, works the other
    # way round: a DB-side query run in a post-load pass in ingest.py,
    # after conversations/blocks are actually in the database (blocks.type
    # can't be queried before that). `chatvault.tags.flag_shape_unclear()`
    # follows that same, already-established pattern instead of inventing a
    # new adapter->loader channel for one platform.
    yield conversation

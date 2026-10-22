from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters import identifiers
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
#
# Evidence base for every branch in _blocks_for_assistant_kind() below (per
# docs/50_SPEC_ingestion_fidelity_v1.md §4.4 rule 1):
# docs/audit-shapes/cursor_copilot-unknown.md (620 `unknown` blocks, 7
# distinct shapes, all `role='assistant'` records keyed on `kind`, enumerated
# over every record — not sampled) and the cursor_copilot row of
# docs/audit-shapes/reasoning-sweep.md, which found zero known-vocabulary
# reasoning hits and confirmed `cursor_copilot` genuinely has no `thinking`
# content hiding under an unexpected key (unlike e.g. windsurf) — so this
# adapter needs no reasoning-misclassification fix, only the `unknown`-block
# classification below.

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
    role = identifiers.normalise_role(rec.get("role"))
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


# ── role='assistant', kind=<part kind> decomposition ────────────────────
#
# Evidence: cursor_copilot-unknown.md's "Shape signatures" table lists
# exactly 7 `kind` values under `role='assistant'`, all sharing
# `keys=content,idx,iso,kind,model,part,role,text,ts` with `$.content` and
# `$.text` always present-but-empty (see "Zero-volume leaf paths") — which
# is why the old catch-all (`"text" if rec.get("text") else "unknown"`)
# always fell through to `unknown` for every one of these 620 records.
#
# Confirmed against real production `raw_json` (blocks.id 43047, 43177,
# 43049, 43841, 43845, 43854, 44721 — the audit's own example ids), not
# guessed from the flattened key-path table alone: several paths that look
# like top-level content in the audit (`part.data.prompt`, `part.message`,
# `part.title`, `part.buttons[]`) all belong to the single `confirmation`
# record, and the one `kind='text'` record is, despite its name, a
# working-set folder/file tree (label + nested children), not dialogue —
# exactly the kind of "confident wrong label" spec §4.4 rule 2 warns about,
# which is why this reads the real JSON structurally instead of pattern-
# matching key names.


def _uri_text(uri: dict) -> str:
    return uri.get("path") or uri.get("external") or uri.get("fsPath") or ""


def _inline_reference_text(ref: dict) -> str:
    location = ref.get("location") if isinstance(ref.get("location"), dict) else {}
    uri = location.get("uri") if isinstance(location.get("uri"), dict) else {}
    return (
        ref.get("name")
        or ref.get("path")
        or ref.get("fsPath")
        or ref.get("external")
        or uri.get("path")
        or uri.get("fsPath")
        or uri.get("external")
        or ""
    )


def _flatten_edit_texts(edits: object) -> list[str]:
    texts: list[str] = []
    if isinstance(edits, list):
        for group in edits:
            if isinstance(group, list):
                for edit in group:
                    if isinstance(edit, dict) and isinstance(edit.get("text"), str) and edit["text"]:
                        texts.append(edit["text"])
    return texts


def _tree_labels(node: dict, depth: int = 0) -> list[str]:
    labels: list[str] = []
    label = node.get("label") if isinstance(node, dict) else None
    if isinstance(label, str) and label:
        labels.append("  " * depth + "- " + label)
    children = node.get("children") if isinstance(node, dict) else None
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                labels.extend(_tree_labels(child, depth + 1))
    return labels


def _blocks_for_assistant_kind(kind: str, part: dict) -> tuple[str, str]:
    """Returns (block_type, text) for a role='assistant' record identified
    by its `kind`. Falls back to ('unknown', '') for any kind outside the
    7-shape inventory the audit enumerated, so a genuinely new shape still
    surfaces honestly (spec §4.4 rule 2) instead of being guessed at."""

    if kind == "codeblockUri":
        # Evidence: `part.uri.path`/`.external`/`.fsPath` — 391 occurrences
        # combined (matches the 215-record `codeblockUri` shape count plus
        # the 176 sharing `uri` under other edit-bearing kinds), 21-25k
        # chars each — a link to the file the assistant's generated code
        # block refers to. A resource pointer, not dialogue -> attachment.
        uri = part.get("uri") if isinstance(part.get("uri"), dict) else {}
        return "attachment", _uri_text(uri)

    if kind == "inlineReference":
        # Evidence: 211 records. Two sub-shapes confirmed against real JSON
        # (blocks.id 43177 and 43206): a direct
        # `inlineReference.{fsPath,path,external,scheme}` variant (142
        # occurrences) and a symbol-reference variant nested under
        # `inlineReference.location.uri.*` carrying `name`/`resolveId`/
        # `containerName` (69 occurrences) — an @-mention style context
        # chip either way -> attachment.
        ref = part.get("inlineReference") if isinstance(part.get("inlineReference"), dict) else {}
        return "attachment", _inline_reference_text(ref)

    if kind == "textEditGroup":
        # Evidence: `part.edits[][].text` — 12,132 occurrences, 460,906
        # chars, the single largest leaf in the report (e.g. "import sys").
        # `part.edits` is a list of edit groups, each a list of
        # `{text, range}` insert fragments against `part.uri` (confirmed on
        # blocks.id 43049) — the code diff the assistant applied, i.e. a
        # tool-call result rather than conversational text.
        uri = part.get("uri") if isinstance(part.get("uri"), dict) else {}
        file_ref = _uri_text(uri)
        body = "".join(_flatten_edit_texts(part.get("edits")))
        return "tool_result", f"### {file_ref}\n\n{body}" if file_ref else body

    if kind == "progressMessage":
        # Evidence: `part.content.value` — 14 occurrences (exactly matching
        # the 14-record `progressMessage` shape count), e.g. "Refining
        # question to improve search accuracy." — a transient status
        # message, not a dialogue turn.
        content = part.get("content") if isinstance(part.get("content"), dict) else {}
        value = content.get("value") or ""
        return "meta", f"[{value}]" if value else ""

    if kind == "command":
        # Evidence: `part.command.title`/`.id`/`.$ident`/`.arguments[]` — 3
        # occurrences each (matches the 3-record `command` shape count
        # exactly), e.g. "Show in Settings Editor" — a UI command
        # invocation surfaced inline, matching vscode_legacy's identical
        # `command` classification.
        command = part.get("command") if isinstance(part.get("command"), dict) else {}
        return "system", command.get("title") or command.get("id") or ""

    if kind == "confirmation":
        # Evidence: blocks.id 44721 (the audit's own example) shows
        # `part.title` (1 occ, "Enable Claude 3.7 Sonnet..."), `part.message`
        # (1 occ, the consent copy) and `part.data.prompt` (1 occ, 460
        # chars) all belong to this single confirmation record, not to a
        # separate `text`-kind record as the flattened key-path table alone
        # would suggest — an interactive consent dialog, replayed alongside
        # the user prompt that triggered it.
        parts = [part.get("title"), part.get("message")]
        data = part.get("data") if isinstance(part.get("data"), dict) else {}
        parts.append(data.get("prompt"))
        return "system", "\n\n".join(p for p in parts if isinstance(p, str) and p.strip())

    if kind == "text":
        # Evidence: blocks.id 43854, the sole `kind='text'` example. Despite
        # the name, `part` here has no `text` field at all — it is a
        # working-set folder/file reference tree (`label` + nested
        # `children[]`, each carrying its own `uri`), confirmed by reading
        # the raw JSON directly rather than trusting the kind label. An
        # attached file/folder listing, not prose -> attachment.
        return "attachment", "\n".join(_tree_labels(part))

    # Per spec §4.4 rule 2: no kind in the audit's 7-shape inventory matches
    # — do not guess a mapping for a shape the evidence hasn't covered.
    # Stay `unknown`, but populate `text` from the raw `part` payload
    # instead of leaving it silently empty, and let the caller tag the
    # conversation `review/shape-unclear` (same DB-side post-load pass
    # ingest.py already runs for vscode via flag_shape_unclear()).
    fallback = json.dumps(part, default=str)[:2000] if part else ""
    return "unknown", fallback


def _parse_session(path: Path, meta: dict, records: list[dict]) -> Iterator[dict]:
    messages = []
    for idx, rec in enumerate(records):
        role = identifiers.normalise_role(rec.get("role"))
        msg_role, block_type = _classify(rec)
        tool_name = None

        if role == "assistant" and rec.get("kind") and not rec.get("text") and not rec.get("content"):
            # Evidence: cursor_copilot-unknown.md — every `unknown` block in
            # the corpus is a `role='assistant'` record identified only by
            # `part.kind`, with `$.text`/`$.content` always present-but-
            # empty (see module-level comment above _blocks_for_assistant_kind
            # for full citations).
            block_type, text = _blocks_for_assistant_kind(str(rec.get("kind")), rec.get("part") or {})
        elif role == "tool":
            part = rec.get("part") or {}
            tool_name = _tool_name(part)
            text = rec.get("text") or rec.get("content") or _tool_fallback_text(part)
        else:
            text = rec.get("text") or rec.get("content") or ""

        block = make_block(0, block_type, identifiers.clean_text(text), raw=rec, tool_name=tool_name)
        messages.append(make_message(idx, msg_role, rec.get("model"), _iso_z(rec.get("iso")), [block], raw=rec))

    if not messages:
        return

    # Last-message time: every session record carries `iso`; the prompts dump
    # below has no times at all and stays NULL (flagged review/no-end-date).
    stamps = [m["created_at"] for m in messages if m.get("created_at")]
    yield make_conversation(
        platform=platform,
        native_id=meta.get("session_id"),
        title=_clean(meta.get("title"), "untitled"),
        project=_clean(meta.get("project"), "unknown"),
        started_at=_iso_z(meta.get("created")),
        ended_at=max(stamps) if stamps else None,
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

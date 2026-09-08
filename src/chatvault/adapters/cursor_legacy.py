from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message
from chatvault.adapters.identifiers import clean_text, normalise_role

platform = "cursor"
name = "cursor_legacy"

# Source: tools/corpus_full/export_cursor_full.py. Each JSONL is one Cursor
# composer: line 0 is meta (extraction="cursor-composer-full-v1"), following
# lines are one record per bubble (role 1=user, 2=assistant; anything else
# comes through as "type_N" or "missing_bubble" when the bubble's KV row was
# gone). A single assistant bubble can carry text AND a tool call
# (toolFormerData) at once — both become separate blocks on one message.
#
# docs/50_SPEC_ingestion_fidelity_v1.md §4.4 rule 3: role normalisation is
# routed through identifiers.normalise_role() rather than a private map.
# Verified against production (`SELECT DISTINCT role FROM messages JOIN
# conversations ... WHERE platform='cursor'`) that only "user" and
# "assistant" ever appear as message roles for this adapter today, so this
# is a no-op for every record currently ingested — normalise_role() gives
# identical output for both. It only changes the *default* for a role this
# platform has never actually produced (previously silently "system",
# now the raw value itself, matching every other identifiers.py-routed
# adapter).


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
                    and obj.get("extraction") == "cursor-composer-full-v1"
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


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str) if value is not None else ""


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
        raw_role = rec.get("role")
        msg_role = normalise_role(raw_role)

        blocks = []
        text = rec.get("text") or rec.get("content") or ""
        if text:
            blocks.append(make_block(0, "text", text, raw=rec))

        tool = rec.get("toolFormerData")
        if isinstance(tool, dict):
            tool_name = tool.get("name")
            args = tool.get("params") or tool.get("rawArgs")
            blocks.append(make_block(0, "tool_use", _stringify(args), raw=tool, tool_name=tool_name))
            if tool.get("result"):
                blocks.append(make_block(0, "tool_result", _stringify(tool.get("result")), raw=tool, tool_name=tool_name))

        # docs/audit-shapes/cursor-unknown.md: 1512 blocks classified `unknown`
        # on this platform, 1501 of them one exact shape —
        # `keys=bubble,bubble_id,content,idx,iso,model,role,text,thinking,
        # toolResults,ts | role='assistant'` — where `$.text`/`$.content`
        # are *always* empty (report's zero-volume table) so the `text`
        # branch above never fires, and there is no `toolFormerData` on
        # this shape at all. The real content sits at `$.thinking.text`
        # (== `$.bubble.thinking.text`, mirrored): 1501 occurrences,
        # 249,230 total chars of real reasoning prose (report line 26-27;
        # confirmed non-boilerplate by the reasoning-locator sweep, which
        # separately reports `cursor` has 0 `thinking`-typed blocks before
        # this fix — docs/audit-shapes/reasoning-sweep.md, "Over-
        # classification check" table).
        #
        # `$.thinking.signature` (== `$.bubble.thinking.signature`) has the
        # *same* occurrence count and a larger total (552,511 chars) but is
        # NOT reasoning text: its example value is itself a JSON envelope
        # `{"id":"rs_...","type":"reasoning","encrypted_content":
        # "gAAAAABpF5NCwDH..."}` — "gAAAAA" is the fixed prefix of a
        # Fernet-encrypted token, i.e. an opaque authentication/
        # verification blob for the reasoning trace, not human-readable
        # content. It is deliberately never read into `blocks.text` here;
        # it stays exactly where it already is, inside `raw` (spec §4.4
        # rule 4 — raw_json is untouched by this phase).
        if not blocks and isinstance(rec.get("thinking"), dict):
            reasoning = clean_text(str(rec["thinking"].get("text") or "").strip())
            if reasoning:
                blocks.append(make_block(0, "thinking", reasoning, raw=rec["thinking"]))

        # Same audit, same shape: a minority of these otherwise-empty
        # records carry a different content-bearing field nested under
        # `bubble` instead of (or in addition to an empty/missing)
        # `thinking`. Per spec §4.4 rule 2 ("never invent a mapping to make
        # a number go down"), these are genuinely ambiguous — an internal
        # context-compaction summary is neither dialogue nor reasoning —
        # so they stay `type='unknown'` but get `text` populated from the
        # most content-bearing leaf available, rather than a confident
        # wrong label. `$.bubble.cachedConversationSummary`: 17
        # occurrences / 424,231 chars (median len 21,562). `$.bubble.
        # conversationSummary`: 5 occurrences / 112,005 chars. (Report
        # lines 25, 28.)
        bubble = rec.get("bubble") if isinstance(rec.get("bubble"), dict) else {}
        if not blocks:
            summary = bubble.get("cachedConversationSummary") or bubble.get("conversationSummary")
            if isinstance(summary, str) and summary.strip():
                blocks.append(make_block(0, "unknown", clean_text(summary), raw=rec))

        # Same shape again, a single occurrence in the audited corpus but
        # an unambiguous one: `$.bubble.errorDetails.{message,error,
        # stackTrace}` (report lines 41, 44, 47-48 — 26/323/1577 chars
        # respectively) is a surfaced API failure (example: rate-limit
        # error on a premium model), not a shape whose meaning is unclear.
        # Classified `error`, not left `unknown`.
        if not blocks:
            err = bubble.get("errorDetails")
            if isinstance(err, dict):
                err_text = "\n".join(
                    str(err[k]) for k in ("message", "error", "stackTrace") if err.get(k)
                )
                if err_text.strip():
                    blocks.append(make_block(0, "error", clean_text(err_text), raw=err))

        # Whatever is left after all of the above is a genuinely empty
        # assistant turn on this shape (no thinking, no summary, no error —
        # the report's remaining 11-of-1512 signature variant, which lacks
        # a `thinking` key entirely) or a truly unrecognised record. Per
        # D7 / spec §4.4 rule 2, this stays `unknown` rather than being
        # guessed at. Conversation-level `review/shape-unclear` tagging for
        # these is not applied here: no adapter in this codebase writes
        # tags (adapters emit CIR dicts only, with no DB handle) — the
        # existing hook is chatvault.tags.apply_rules(), driven by a
        # `tag_rules` row evaluated automatically after ingest. See the
        # implementation report for the exact rule to register once Phase
        # 5 (reprocess) runs against production.
        if not blocks:
            blocks.append(make_block(0, "unknown", "", raw=rec))

        for i, b in enumerate(blocks):
            b["idx"] = i

        messages.append(make_message(idx, msg_role, rec.get("model"), _iso_z(rec.get("iso")), blocks, raw=rec))

    yield make_conversation(
        platform=platform,
        native_id=meta.get("composer_id"),
        title=_clean(meta.get("title"), "untitled"),
        project=_clean(meta.get("project"), "unassigned"),
        started_at=_iso_z(meta.get("created")),
        ended_at=_iso_z(meta.get("updated")),
        source_path=str(path),
        raw=meta,
        messages=messages,
    )

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "grok"
name = "grok"

# Verified 2026-08-12 against a real xAI privacy-request export
# (~/ChatVault/raw/h1-grok-account-ttl/30d/export_data/<user_id>/
# prod-grok-backend.json — 599 real conversations, 33 projects). No public
# schema exists for this file (xAI has no documented Takeout-equivalent;
# checked docs.x.ai and x.ai/legal directly, found none) so every field
# below was confirmed by reading the actual export, not assumed from
# other providers' conventions.
#
# Shape: {conversations: [...], projects: [...], media_posts: [...],
# tasks: [...]}. Each conversations[] entry is {conversation, responses}.
# `conversation` carries id/title/create_time/workspace_id/project fields.
# `responses` is a FLAT array of {response: {...}, share_link}, each
# response already a graph node (_id, parent_response_id, children, path)
# — there is no separate node/mapping dict to dereference the way ChatGPT
# and DeepSeek have.
#
# IMPORTANT, found by direct verification, not assumed: conversation.
# leaf_response_id does NOT reliably point at a real response._id — checked
# across 50 real conversations and 47/50 failed to resolve. Do not use it
# as an active-path anchor. Instead walk from any response with no
# children (a true leaf) back through parent_response_id, and take the
# longest such chain, matching the same "no reliable pointer, fall back to
# deepest path" approach already used in chatgpt.py and deepseek.py for
# their own missing/unreliable pointers.
#
# sender values seen: "human", "assistant", "ASSISTANT" (case
# inconsistency confirmed in real data — normalized below, not guessed).
# model is "" (empty string) on human turns, a real model slug on
# assistant turns (e.g. "grok-4", "grok-4-1-thinking-1129") — normalized
# to None on the human side rather than kept as "".


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    # "leaf_response_id" + "parent_response_id" together are specific to
    # this export shape; neither appears in chatgpt/claude/deepseek exports.
    return b'"leaf_response_id"' in prefix and b'"parent_response_id"' in prefix


def _mongo_date_to_iso(value) -> str | None:
    """create_time is MongoDB extended JSON: {"$date": {"$numberLong":
    "<epoch_ms>"}}. Confirmed this is the only shape present — no bare
    ISO strings or bare epoch ints seen anywhere in the file."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        date_obj = value.get("$date")
        if isinstance(date_obj, dict):
            ms = date_obj.get("$numberLong")
        else:
            ms = date_obj
        if ms is not None:
            try:
                return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                return None
    return None


def _normalize_sender(sender: str | None) -> str:
    s = (sender or "").strip().lower()
    if s == "human":
        return "user"
    if s == "assistant":
        return "assistant"
    return "unknown"


# xai:tool_usage_card XML blocks appear inline inside steps[].tagged_text
# and agent_thinking_traces[].thinking_trace as literal embedded markup,
# not as a separate structured field — confirmed in the real file. We
# keep this raw rather than attempting to parse the XML, since the
# structured `steps[].tool_usage_results` / `web_search_results` fields
# already carry the same information in JSON form where present.


def _response_blocks(resp: dict) -> list[dict]:
    blocks: list[dict] = []
    idx = 0

    text = resp.get("message") or ""
    if text:
        blocks.append(make_block(idx, "text", text, raw={"message": text}))
        idx += 1

    traces = resp.get("agent_thinking_traces") or []
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        trace_text = trace.get("thinking_trace") or ""
        if trace_text:
            blocks.append(make_block(idx, "thinking", trace_text, raw=trace))
            idx += 1

    # steps[].web_search_results is a per-step copy of the SAME results
    # already present in the top-level response.web_search_results rollup
    # — confirmed by direct comparison on real data (identical counts,
    # identical content, same conversation). Only the tagged_text (tool
    # call markup) is unique to `steps`; search results are read once,
    # below, from the top-level field, to avoid double-ingesting.
    steps = resp.get("steps") or []
    for step in steps:
        if not isinstance(step, dict):
            continue
        tagged = step.get("tagged_text") or {}
        for tag_name, tag_text in tagged.items():
            if not tag_text or tag_name == "header":
                continue
            block_type = "tool_use" if "tool" in tag_name else "text"
            blocks.append(make_block(idx, block_type, tag_text, raw=step, tool_name=tag_name if block_type == "tool_use" else None))
            idx += 1

    for result in resp.get("web_search_results") or []:
        if isinstance(result, dict):
            summary = f"{result.get('title', '')} — {result.get('url', '')}\n{result.get('preview', '')}"
            blocks.append(make_block(idx, "tool_result", summary, raw=result, tool_name="web_search"))
            idx += 1

    # file_attachments is a bare list of asset IDs with no filename or
    # mime type in-band (confirmed — checked the real field, it's just
    # UUID strings). Recorded as an attachment block with only the id
    # known; matching bytes live under prod-mc-asset-server/<id>/content
    # in the same export, but linking that up is a separate task, not
    # something to guess at here.
    for asset_id in resp.get("file_attachments") or []:
        blocks.append(
            make_block(
                idx,
                "attachment",
                "",
                raw={"asset_id": asset_id, "resolved": False},
            )
        )
        idx += 1

    if not blocks:
        blocks.append(make_block(0, "unknown", "", raw=resp))

    return blocks


def _longest_leaf_chain(responses: list[dict]) -> list[dict]:
    by_id = {r["response"]["_id"]: r["response"] for r in responses if isinstance(r, dict) and r.get("response", {}).get("_id")}
    has_children = {r.get("parent_response_id") for r in by_id.values() if r.get("parent_response_id")}
    leaves = [rid for rid in by_id if rid not in has_children]
    if not leaves:
        leaves = list(by_id.keys())

    def chain_from(leaf_id: str) -> list[str]:
        chain = []
        node_id = leaf_id
        seen: set[str] = set()
        while node_id is not None and node_id in by_id and node_id not in seen:
            seen.add(node_id)
            chain.append(node_id)
            node_id = by_id[node_id].get("parent_response_id")
        chain.reverse()
        return chain

    best: list[str] = []
    for leaf_id in leaves:
        candidate = chain_from(leaf_id)
        if len(candidate) > len(best):
            best = candidate
    return [by_id[nid] for nid in best]


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    conversations = data.get("conversations") if isinstance(data, dict) else None
    if not conversations:
        return

    for entry in conversations:
        if not isinstance(entry, dict):
            continue
        conv = entry.get("conversation") or {}
        responses = entry.get("responses") or []
        if not conv:
            continue

        ordered = _longest_leaf_chain(responses)

        messages = []
        idx = 0
        timestamps = []
        for resp in ordered:
            role = _normalize_sender(resp.get("sender"))
            blocks = _response_blocks(resp)
            if not blocks:
                continue
            created_at = _mongo_date_to_iso(resp.get("create_time"))
            if created_at:
                timestamps.append(created_at)
            model = resp.get("model") or None
            if role != "assistant":
                model = None
            messages.append(make_message(idx, role, model, created_at, blocks, raw=resp))
            idx += 1

        if not messages:
            continue

        yield make_conversation(
            platform=platform,
            native_id=conv.get("id"),
            title=conv.get("title"),
            # workspace_id is Grok's own grouping concept (verified present
            # on every conversation in the real file) — same conceptual
            # role as cwd for claude_code or workspace_uri for editors, so
            # it goes into `project` for consistency with every other
            # adapter, not left as raw-only.
            project=conv.get("workspace_id"),
            started_at=_mongo_date_to_iso(conv.get("create_time")) or (min(timestamps) if timestamps else None),
            ended_at=_mongo_date_to_iso(conv.get("modify_time")) or (max(timestamps) if timestamps else None),
            source_path=str(path),
            raw=conv,
            messages=messages,
        )


def sniff_projects(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    # Same file as sniff() (conversations + projects share prod-grok-backend.json)
    # — distinguished from sniff() only by which top-level key the loader asks
    # about, per the adapters.detect_projects() convention.
    return b'"leaf_response_id"' in prefix and b'"parent_response_id"' in prefix


def parse_projects(path: Path) -> Iterator[dict]:
    """Grok's `projects` array — 33 real entries in the export (e.g.
    "Assistant UI", "job-hunter", "Biology Tutor"). This is the backend
    name for what Grok's UI calls "Projects" — a purely in-platform
    grouping construct, no filesystem path, conceptually a sibling of
    Claude Desktop's projects, NOT of an editor's cwd/workspace_uri.
    `kind='grok_project'` reflects that distinction.

    IMPORTANT — verified, not assumed: `conversation.workspace_id` (the
    field that would link a conversation to one of these projects) is
    populated on only 1 of 599 conversations in this export. The other
    598 are legitimately un-grouped, OR this "30d"-named export (see the
    containing folder path) is a 30-day retention window that excludes
    older project-tagged conversations — genuinely unclear which, and not
    something to guess at. If a fuller Grok export becomes available,
    re-run this adapter against it before concluding most conversations
    have no project.

    Also verified: never merge a Grok project with a same-named local
    workspace or Claude Desktop project. All three are namespaced
    separately by (platform, native_id) — a Grok project called
    "job-hunter" and a local folder called "job-hunter" are unrelated
    unless a human says otherwise. See docs/20_FIX_project_identity.md §7.

    Safe to wire into ingest as of 2026-08-12: loader.load_project() now
    reads `kind` from this dict (defaulting to 'desktop' for callers that
    don't set it), so 'grok_project' lands correctly rather than being
    forced to 'desktop'. Verified end-to-end against the real export: 33
    rows land with kind='grok_project', zero with 'desktop'."""
    data = orjson.loads(path.read_bytes())
    projects = data.get("projects") if isinstance(data, dict) else None
    if not projects:
        return
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        yield {
            "platform": platform,
            "native_id": proj.get("workspace_id"),
            "name": proj.get("name"),
            "description": proj.get("custom_personality"),
            "instructions": None,
            "created_at": _mongo_date_to_iso(proj.get("create_time")) if isinstance(proj.get("create_time"), dict) else proj.get("create_time"),
            "raw": proj,
            "docs": [],
            "kind": "grok_project",  # honored once loader.load_project() supports it; see docstring
        }

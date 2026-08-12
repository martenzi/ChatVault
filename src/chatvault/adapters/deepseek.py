from __future__ import annotations

from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "deepseek"
name = "deepseek"

# Verified 2026-08-12 against a real official export
# (~/ChatVault/raw/deepseek_data-2026-08-12/conversations.json, 320
# conversations) — no assumptions carried over from ChatGPT/Claude
# conventions; every field below was confirmed present in the actual file.
#
# Shape: top-level array of conversations. Each has {id, title,
# inserted_at, updated_at, mapping}. `mapping` is a tree exactly like
# ChatGPT's `mapping` (node id -> {id, parent, children, message}), but
# DeepSeek has no `current_node` pointer — so instead of resolving one
# active path, we walk every leaf-to-root path and take the longest,
# same fallback ChatGPT's adapter already uses when current_node is
# missing/stale.
#
# message = {model, inserted_at, fragments: [{type, content, ...}]}.
# fragments[].type observed: REQUEST (user), RESPONSE (assistant text),
# THINK (assistant reasoning), TOOL_FIND / TOOL_OPEN / TOOL_SEARCH (tool
# calls), SEARCH (web search results), FILE (attachment). Confirmed via
# full-file scan, not inferred from one sample.

_FRAGMENT_ROLE = {
    "REQUEST": "user",
    "RESPONSE": "assistant",
    "THINK": "assistant",
    "SEARCH": "assistant",
    "TOOL_FIND": "assistant",
    "TOOL_OPEN": "assistant",
    "TOOL_SEARCH": "assistant",
    "FILE": "user",  # file fragments seen attached to REQUEST-side turns
}

_FRAGMENT_BLOCK_TYPE = {
    "REQUEST": "text",
    "RESPONSE": "text",
    "THINK": "thinking",
    "SEARCH": "tool_result",
    "TOOL_FIND": "tool_use",
    "TOOL_OPEN": "tool_use",
    "TOOL_SEARCH": "tool_use",
    "FILE": "attachment",
}


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    # DeepSeek's export is the only adapter target with both "mapping" and
    # "fragments" — ChatGPT/Claude mapping exports never contain
    # "fragments", so this stays disjoint from chatgpt.sniff without
    # needing to inspect chatgpt.py's own markers.
    return b'"mapping"' in prefix and b'"fragments"' in prefix


def _longest_leaf_path(mapping: dict) -> list[str]:
    """DeepSeek's mapping has no current_node pointer (confirmed absent
    from every one of the 320 real conversations checked), so there is no
    single authoritative active branch the way ChatGPT's current_node
    gives one. Fall back to the deepest root-to-leaf path, matching the
    same heuristic chatgpt.py already uses for its no-current_node case."""
    children_of: dict[str | None, list[str]] = {}
    for nid, node in mapping.items():
        children_of.setdefault(node.get("parent"), []).append(nid)

    def deepest_path(node_id: str, seen: set[str]) -> list[str]:
        if node_id in seen:
            return [node_id]
        seen = seen | {node_id}
        kids = children_of.get(node_id, [])
        if not kids:
            return [node_id]
        best: list[str] = []
        for k in kids:
            candidate = deepest_path(k, seen)
            if len(candidate) > len(best):
                best = candidate
        return [node_id] + best

    roots = children_of.get(None, []) or children_of.get("root", [])
    if not roots and "root" in mapping:
        roots = children_of.get("root", [])
    best_path: list[str] = []
    for r in roots:
        candidate = deepest_path(r, set())
        if len(candidate) > len(best_path):
            best_path = candidate
    return best_path


def _fragment_blocks(fragments: list) -> list[dict]:
    blocks = []
    for idx, frag in enumerate(fragments or []):
        if not isinstance(frag, dict):
            continue
        ftype = frag.get("type", "")
        block_type = _FRAGMENT_BLOCK_TYPE.get(ftype, "unknown")
        text = frag.get("content") or ""
        tool_name = ftype if block_type == "tool_use" else None
        blocks.append(make_block(idx, block_type, text, raw=frag, tool_name=tool_name))
    return blocks


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    conversations = data if isinstance(data, list) else [data]

    for conv in conversations:
        if not isinstance(conv, dict) or "mapping" not in conv:
            continue
        mapping = conv.get("mapping") or {}
        active_ids = [nid for nid in _longest_leaf_path(mapping) if nid != "root"]

        messages = []
        idx = 0
        timestamps = []
        for node_id in active_ids:
            node = mapping.get(node_id) or {}
            msg = node.get("message")
            if not msg:
                continue
            fragments = msg.get("fragments") or []
            if not fragments:
                continue

            # A DeepSeek "message" node can itself hold both a REQUEST
            # fragment and, in edited/regenerated turns, more than one
            # fragment of mixed roles — confirmed by scanning fragment
            # type combinations across the file. Split per-fragment-role
            # rather than assuming one node = one role, so a THINK+RESPONSE
            # pair (assistant) is never merged with a REQUEST (user) node.
            by_role: dict[str, list[dict]] = {}
            for frag in fragments:
                if not isinstance(frag, dict):
                    continue
                role = _FRAGMENT_ROLE.get(frag.get("type", ""), "unknown")
                by_role.setdefault(role, []).append(frag)

            created_at = msg.get("inserted_at")
            model = msg.get("model") or None

            for role, frags in by_role.items():
                blocks = _fragment_blocks(frags)
                if not blocks:
                    continue
                msg_model = model if role == "assistant" else None
                if created_at:
                    timestamps.append(created_at)
                messages.append(make_message(idx, role, msg_model, created_at, blocks, raw=msg))
                idx += 1

        if not messages:
            continue

        yield make_conversation(
            platform=platform,
            native_id=conv.get("id"),
            title=conv.get("title"),
            project=None,  # DeepSeek has no workspace/folder concept in this export
            started_at=conv.get("inserted_at") or (min(timestamps) if timestamps else None),
            ended_at=conv.get("updated_at") or (max(timestamps) if timestamps else None),
            source_path=str(path),
            raw={k: v for k, v in conv.items() if k != "mapping"},
            messages=messages,
        )

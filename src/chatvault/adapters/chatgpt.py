from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "chatgpt"
name = "chatgpt"

_HIDDEN_METADATA_KEYS = ("is_visually_hidden_from_conversation",)
_AUDIO_ATTACHMENT_TYPES = {"audio_asset_pointer", "real_time_user_audio_video_asset_pointer"}


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    # "mapping" alone is not exclusive to ChatGPT — DeepSeek's export is also
    # a node-map tree and satisfies this same prefix check (confirmed against
    # a real DeepSeek export, which has both "mapping" and "fragments"; see
    # deepseek.py). Reject explicitly rather than relying on registry order,
    # so this stays correct regardless of where either adapter sits in
    # adapters._REGISTRY.
    return b'"mapping"' in prefix and b'"fragments"' not in prefix


def _epoch_to_iso(value) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _blocks_from_message_content(content: dict) -> list[dict]:
    if not isinstance(content, dict):
        return [make_block(0, "unknown", "", raw={"value": content})]

    ct = content.get("content_type")
    parts = content.get("parts")

    if ct == "text":
        text = "\n".join(p for p in parts if isinstance(p, str)) if parts else (content.get("text") or "")
        return [make_block(0, "text", text, raw=content)] if text else []

    if ct == "code":
        text = content.get("text") or "\n".join(str(p) for p in (parts or []))
        return [make_block(0, "code", text, raw=content)]

    if ct == "execution_output":
        text = content.get("text") or "\n".join(str(p) for p in (parts or []))
        return [make_block(0, "tool_result", text, raw=content)]

    if ct == "multimodal_text":
        blocks = []
        for idx, p in enumerate(parts or []):
            if isinstance(p, str):
                blocks.append(make_block(idx, "text", p, raw={"text": p}))
            elif isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
                blocks.append(make_block(idx, "image", "", raw=p))
            elif isinstance(p, dict) and p.get("content_type") == "audio_transcription":
                blocks.append(make_block(idx, "text", p.get("text", ""), raw=p))
            elif isinstance(p, dict) and (
                p.get("content_type") in _AUDIO_ATTACHMENT_TYPES or "audio_asset_pointer" in p
            ):
                blocks.append(make_block(idx, "attachment", "", raw=p))
            elif isinstance(p, dict):
                blocks.append(make_block(idx, "unknown", "", raw=p))
        return blocks

    if ct == "thoughts":
        thoughts = content.get("thoughts") or []
        texts = []
        for t in thoughts:
            if isinstance(t, dict):
                texts.append(t.get("content") or t.get("summary") or "")
        text = "\n\n".join(t for t in texts if t)
        return [make_block(0, "thinking", text, raw=content)]

    if ct == "reasoning_recap":
        # e.g. {"content": "Thought for 12s", "content_type": "reasoning_recap"} -- a UI
        # summary of hidden reasoning time, not the reasoning itself. Real text, low value.
        text = content.get("content") or ""
        return [make_block(0, "thinking", text, raw=content)] if text else []

    return [make_block(0, "unknown", "", raw=content)]


def _active_path(mapping: dict, current_node: str | None) -> list[str]:
    if current_node is None or current_node not in mapping:
        # fall back to the deepest node by walking children from any root
        def depth(node_id: str, seen: set[str]) -> int:
            node = mapping.get(node_id)
            if not node or not node.get("children") or node_id in seen:
                return 0
            seen.add(node_id)
            return 1 + max((depth(c, seen) for c in node["children"]), default=0)

        roots = [nid for nid, n in mapping.items() if not n.get("parent")]
        best = None
        best_depth = -1
        for r in roots:
            d = depth(r, set())
            if d > best_depth:
                best_depth, best = d, r
        current_node = best

    path: list[str] = []
    node_id = current_node
    seen: set[str] = set()
    while node_id is not None and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        path.append(node_id)
        node_id = mapping[node_id].get("parent")
    path.reverse()
    return path


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    conversations = data if isinstance(data, list) else [data]

    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        mapping = conv.get("mapping") or {}
        active_ids = _active_path(mapping, conv.get("current_node"))
        active_set = set(active_ids)

        messages = []
        idx = 0
        timestamps = []
        for node_id in active_ids:
            node = mapping.get(node_id) or {}
            msg = node.get("message")
            if not msg:
                continue
            author = msg.get("author") or {}
            role = author.get("role") or "unknown"
            metadata = msg.get("metadata") or {}
            if any(metadata.get(k) for k in _HIDDEN_METADATA_KEYS):
                continue
            content = msg.get("content") or {}
            blocks = _blocks_from_message_content(content)
            if not blocks:
                continue
            for b_idx, b in enumerate(blocks):
                b["idx"] = b_idx
                if role == "tool" and not b.get("tool_name"):
                    b["tool_name"] = author.get("name")
            created_at = _epoch_to_iso(msg.get("create_time"))
            if created_at:
                timestamps.append(created_at)
            messages.append(
                make_message(idx, role, msg.get("metadata", {}).get("model_slug"), created_at, blocks, raw=msg)
            )
            idx += 1

        abandoned = [
            mapping[nid]["message"]
            for nid in mapping
            if nid not in active_set and mapping[nid].get("message")
        ]
        if abandoned:
            ab_blocks = [
                make_block(i, "unknown", "", raw=m) for i, m in enumerate(abandoned)
            ]
            ab_msg = make_message(idx, "system", None, None, ab_blocks, raw={"abandoned_branch": True})
            ab_msg["excluded"] = 1
            messages.append(ab_msg)
            idx += 1

        yield make_conversation(
            platform=platform,
            native_id=conv.get("conversation_id") or conv.get("id"),
            title=conv.get("title"),
            project=None,
            started_at=_epoch_to_iso(conv.get("create_time")) or (min(timestamps) if timestamps else None),
            ended_at=_epoch_to_iso(conv.get("update_time")) or (max(timestamps) if timestamps else None),
            source_path=str(path),
            raw={k: v for k, v in conv.items() if k != "mapping"},
            messages=messages,
        )

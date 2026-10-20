from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "openwebui"
name = "openwebui"

# Open WebUI per-chat JSON export, enumerated over all 28 real files
# (2026-10-20): 14 distinct chats, each present twice — `chat_exports/` has a
# top-level `id`, `chat_exports_simple2/` omits it; the two are otherwise
# byte-equivalent in `content` (identical history tree, identical
# `messages`). Shape:
#   {created_at "YYYY-MM-DD HH:MM:SS" (UTC), [id], title, content: {
#     id "" , title, models[], params, tags[], files[], timestamp (ms),
#     history: {messages: {<id>: node}, currentId},
#     messages: [node]   # a linearisation, see below
#   }}
# node = {id, parentId, childrenIds[], role user|assistant, content str,
#         timestamp (s), models[] (user) | model/modelName/modelIdx/usage/
#         done/lastSentence/userContext (assistant)}
#
# `history.messages` is the full regenerate/edit tree; `content.messages` is
# a linear copy of one path. In 12 of 14 chats the two agree. In 2 chats
# (several assistant alternatives under the final user turn) `content.messages`
# ends on a different sibling than `history.currentId`; the UI shows
# currentId, so that is the active path — the other siblings are kept in an
# excluded "abandoned branch" message exactly like the chatgpt adapter.
# native_id is the tree's root message id, not the top-level `id`, so the two
# copies of a chat share one uid; the top-level id stays in conversation raw.
# `tags` is empty in all 28 files, so nothing is carried into ChatVault tags.

_REASONING_RE = re.compile(r'\A<details type="reasoning"[^>]*>\n?(.*?)</details>\n?', re.S)
_SUMMARY_RE = re.compile(r"\A<summary>(.*?)</summary>\n?", re.S)


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        if path.stat().st_size > 50_000_000:
            return False
        data = orjson.loads(path.read_bytes())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not {"created_at", "title", "content"} <= set(data):
        return False
    content = data["content"]
    if not isinstance(content, dict):
        return False
    history = content.get("history")
    return isinstance(history, dict) and isinstance(history.get("messages"), dict) and "currentId" in history


def _epoch_to_iso(value, *, ms: bool = False) -> str | None:
    if value is None:
        return None
    try:
        v = float(value) / (1000 if ms else 1)
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _path_to(nodes: dict, current: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    while current and current in nodes and current not in seen:
        seen.add(current)
        out.append(current)
        current = nodes[current].get("parentId")
    out.reverse()
    return out


def _blocks(node: dict) -> list[dict]:
    content = node.get("content")
    if not isinstance(content, str):
        return [make_block(0, "unknown", "", raw={"content": content})]
    blocks = []
    m = _REASONING_RE.match(content) if node.get("role") == "assistant" else None
    if m:
        body = m.group(1)
        summary = _SUMMARY_RE.match(body)
        thought = body[summary.end():] if summary else body
        thought = "\n".join(line[2:] if line.startswith("> ") else line.lstrip(">") for line in thought.splitlines()).strip()
        blocks.append(make_block(0, "thinking", thought, raw={"details": m.group(0), "summary": summary.group(1) if summary else None}))
        content = content[m.end():]
    if content.strip() or not blocks:
        blocks.append(make_block(len(blocks), "text", content, raw={"content": content}))
    return blocks


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    content = data["content"]
    nodes = content["history"]["messages"]
    active = _path_to(nodes, content["history"].get("currentId"))
    active_set = set(active)
    roots = [nid for nid, n in nodes.items() if not n.get("parentId")]

    messages = []
    idx = 0
    stamps = []
    for nid in active:
        node = nodes[nid]
        role = node.get("role") or "unknown"
        created = _epoch_to_iso(node.get("timestamp"))
        if created:
            stamps.append(created)
        model = node.get("model") if role == "assistant" else None
        messages.append(make_message(idx, role, model, created, _blocks(node), raw=node))
        idx += 1

    abandoned = [n for nid, n in nodes.items() if nid not in active_set]
    if abandoned:
        ab_msg = make_message(
            idx, "system", None, None,
            [make_block(i, "unknown", "", raw=n) for i, n in enumerate(abandoned)],
            raw={"abandoned_branch": True},
        )
        ab_msg["excluded"] = 1
        messages.append(ab_msg)

    created_at = _epoch_to_iso(content.get("timestamp"), ms=True)
    if created_at is None and data.get("created_at"):
        try:
            created_at = datetime.strptime(data["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            created_at = None
    yield make_conversation(
        platform=platform,
        native_id=roots[0] if len(roots) == 1 else None,
        title=data.get("title") or content.get("title"),
        project=None,
        started_at=created_at or (min(stamps) if stamps else None),
        ended_at=max(stamps) if stamps else None,
        source_path=str(path),
        raw={k: v for k, v in data.items() if k != "content"} | {"content": {k: v for k, v in content.items() if k != "history"} | {"history_currentId": content["history"].get("currentId")}},
        messages=messages,
    )

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "chatgpt"
name = "chatgpt_legacy_recovered"

# Recovered chat history from a deleted ChatGPT account, captured by a
# DOM/API-scraping export tool (not OpenAI's official bulk export — that
# lands in chatgpt.py's mapping/current_node shape). Distinctive top-level
# keys: accountUserId, authUserId, id, isArchived, messages, title,
# updateTime. Messages are flat {id, text} — no role, no model, no
# per-message timestamp.
#
# Role must be inferred from the message id, which follows one of two
# schemes depending on which export-tool version captured it:
#   scheme A: "root" (empty placeholder) -> "root-nextPrompt" (user) ->
#             "<uuid>" (assistant) -> "<uuid>-nextPrompt" (next user) -> ...
#   scheme B: "aaa1<hex>" (empty placeholder) -> "aaa2<hex>" (user) ->
#             "<uuid>" (assistant) -> "aaa2<hex>" (next user) -> ...
# Verified against all 169 real conversations in this corpus: the two
# schemes never conflict within one file, but the inferred role sequence
# doesn't perfectly alternate for every conversation (~5% of turn
# transitions repeat the same role back to back) — almost certainly
# regenerated/edited turns that this flat exporter couldn't represent as a
# proper branch the way OpenAI's own mapping tree does. That ambiguity is
# inherent to the source data, not a parsing bug; raw_json keeps the
# original id on every row so it's always possible to go back and check.

_EMPTY_PLACEHOLDER_PREFIXES = ("aaa1",)
_USER_ID_PREFIXES = ("aaa2",)


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    return b'"accountUserId"' in prefix and b'"isArchived"' in prefix


def _infer_role(message_id: str) -> str | None:
    """Returns None for the empty placeholder entry (skip it)."""
    if message_id == "root" or message_id.startswith(_EMPTY_PLACEHOLDER_PREFIXES):
        return None
    if message_id.endswith("-nextPrompt") or message_id.startswith(_USER_ID_PREFIXES):
        return "user"
    return "assistant"


def _iso_from_epoch(value) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _parse_conversation(path: Path, conv: dict) -> dict | None:
    raw_messages = conv.get("messages") or []
    messages = []
    idx = 0
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        role = _infer_role(m.get("id", ""))
        if role is None:
            continue
        text = m.get("text") or ""
        block = make_block(0, "text", text, raw=m) if text else make_block(0, "unknown", "", raw=m)
        messages.append(make_message(idx, role, None, None, [block], raw=m))
        idx += 1

    if not messages:
        return None

    return make_conversation(
        platform=platform,
        native_id=conv.get("id"),
        title=conv.get("title"),
        project=None,
        started_at=None,
        ended_at=_iso_from_epoch(conv.get("updateTime")),
        source_path=str(path),
        raw={k: v for k, v in conv.items() if k != "messages"},
        messages=messages,
    )


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    conversations = data if isinstance(data, list) else [data]

    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        cir = _parse_conversation(path, conv)
        if cir is not None:
            yield cir

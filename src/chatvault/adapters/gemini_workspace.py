from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "gemini"
name = "gemini_workspace"

# Google Takeout "Gemini in Workspace/Conversation History/*.txt" — a
# Workspace (organisational) Gemini export. The .txt files are JSON:
#
#   {"conversation_turns": [
#      {"user_turn":   {"prompt": "…", "turn_index": 0, "turn_last_modified": "…+00:00"}},
#      {"system_turn": {"text": [{"data": "…markdown…"}], "turn_index": 1, ...}}
#    ],
#    "creation_time": "…", "last_modification_time": "…", "title": "…"}
#
# Enumerated 2026-09-21 from the single real file present in
# raw/AI_Provider_Exports/Takeout 16 sept not sure which account/ — see
# docs/reports/gemini_myactivity_html_2026-09-21.md §2. Roles observed:
# user_turn / system_turn only. No conversation id field exists, so
# native_id comes from the filename stem. If a future export shows other
# turn keys, they will land as unknown rather than be silently dropped.


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".txt":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(8192)
    except OSError:
        return False
    return b'"conversation_turns"' in prefix and (b'"user_turn"' in prefix or b'"system_turn"' in prefix)


def parse(path: Path) -> Iterator[dict]:
    data = json.loads(path.read_bytes())
    turns = data.get("conversation_turns") or []

    messages: list[dict] = []
    timestamps: list[str] = []
    for idx, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        if "user_turn" in turn:
            t = turn["user_turn"] or {}
            prompt = t.get("prompt") or ""
            ts = t.get("turn_last_modified")
            if ts:
                timestamps.append(ts)
            messages.append(
                make_message(idx, "user", None, ts, [make_block(0, "text", prompt, raw=t)], raw=t)
            )
        elif "system_turn" in turn:
            t = turn["system_turn"] or {}
            ts = t.get("turn_last_modified")
            if ts:
                timestamps.append(ts)
            parts = t.get("text") or []
            blocks = [
                make_block(i, "text", p.get("data") or "", raw=p)
                for i, p in enumerate(parts)
                if isinstance(p, dict)
            ]
            if not blocks:
                blocks = [make_block(0, "unknown", json.dumps(t), raw=t)]
            messages.append(make_message(idx, "assistant", None, ts, blocks, raw=t))
        else:
            # Unrecognised turn key — keep it, flag it.
            ts = None
            messages.append(
                make_message(idx, "system", None, None, [make_block(0, "unknown", json.dumps(turn), raw=turn)], raw=turn)
            )

    if not messages:
        return

    yield make_conversation(
        platform=platform,
        native_id=path.stem,
        title=(data.get("title") or "")[:200] or None,
        project=None,
        started_at=data.get("creation_time") or (min(timestamps) if timestamps else None),
        ended_at=data.get("last_modification_time") or (max(timestamps) if timestamps else None),
        source_path=str(path),
        raw={"export_product": "Gemini in Workspace", "turn_count": len(turns)},
        messages=messages,
    )

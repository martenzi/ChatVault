#!/usr/bin/env python3
"""Extract ~/.local/share/devin/cli/sessions.db into stable JSONL for the
`devin_cli` adapter.

Architecture (docs/54_TASK_devin_adapters.md §3, docs/reports/devin_cli_shapes_2026-09-09.md):
sessions.db is SQLite, and ChatVault's ingest pipeline is file-based
(.json/.jsonl only) — this script is the one place that talks to the live
database; adapters/devin_cli.py only ever reads the JSONL this script emits.

Read safety (task §3.1): sessions.db is in WAL mode and actively written to
(127 MB WAL observed at investigation time). This script uses SQLite's own
backup API against a `mode=ro` connection — atomic, WAL-aware, no manual
file/WAL/SHM copying. Never opens the live file read-write.

Output shape (one choice, stated here per task §3): **one JSONL file per
extraction run, one line per session** — mirrors the natural one-row-per-
session granularity of `sessions`, keeps the output small enough to inspect
by eye (94 sessions as of the investigation, not tens of thousands of tiny
files), and matches the existing `claude_code` adapter's precedent of one
file yielding many conversations via `parse()`.

Per-session record includes, in full (nothing decided here — the adapter
does all classification):
  - the full `sessions` row
  - every `message_nodes` row for that session_id, in node_id order, with
    `chat_message` parsed from its JSON-string column into a real object
    (inspectability — task §3) — the full node tree, not just the main
    chain (docs/reports/devin_cli_shapes_2026-09-09.md §5: every session has
    orphan nodes; nothing is dropped at extraction time, only at the
    adapter's `messages`-linearisation step, and even then only from the
    curated `messages` list — this raw JSONL keeps every node)
  - every `tool_call_state` row for that session_id (supplementary; see
    report §4.5 — largely mirrors `message_nodes` metadata, kept for
    completeness, not required by the adapter's block classification)
  - every `prompt_history` row for that session_id (report §1b)

Stability: output is byte-identical across runs against unchanged source
data — `json.dumps(..., sort_keys=True)` recursively sorts every dict's
keys, and no extraction-time timestamp is embedded in any record.

`transcripts/` and `summaries/` are deliberately not read here — report §6
confirms both duplicate `sessions.db` content and add nothing new.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

DEFAULT_SRC = Path("~/.local/share/devin/cli/sessions.db").expanduser()
SCHEMA_TAG = "devin_cli_session_v1"


def snapshot(src: Path) -> Path:
    """WAL-aware, read-only, atomic snapshot via SQLite's backup API.
    Returns the path to a private temp-file copy the caller must clean up."""
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlite", prefix="devin_cli_snapshot_")
    os.close(fd)
    tmp = Path(tmp_path)
    tmp.unlink()  # backup() wants to create the destination itself

    before_mtime, before_size = src.stat().st_mtime, src.stat().st_size
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(tmp)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()

    after_mtime, after_size = src.stat().st_mtime, src.stat().st_size
    if (before_mtime, before_size) != (after_mtime, after_size):
        raise RuntimeError(
            f"source file {src} changed during snapshot "
            f"(mtime/size {before_mtime}/{before_size} -> {after_mtime}/{after_size}) "
            "— this should be impossible with a mode=ro connection; aborting rather "
            "than risk a torn read."
        )
    return tmp


def _load_message_nodes(con: sqlite3.Connection, session_id: str) -> list[dict]:
    nodes = []
    for row in con.execute(
        "SELECT row_id, node_id, parent_node_id, chat_message, created_at, metadata "
        "FROM message_nodes WHERE session_id = ? ORDER BY node_id",
        (session_id,),
    ):
        row_id, node_id, parent_node_id, chat_message_raw, created_at, metadata_raw = row
        try:
            chat_message = json.loads(chat_message_raw)
        except (json.JSONDecodeError, TypeError):
            chat_message = None
            chat_message_parse_error = chat_message_raw
        else:
            chat_message_parse_error = None
        metadata = None
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
            except (json.JSONDecodeError, TypeError):
                metadata = metadata_raw
        nodes.append(
            {
                "row_id": row_id,
                "node_id": node_id,
                "parent_node_id": parent_node_id,
                "chat_message": chat_message,
                "chat_message_parse_error": chat_message_parse_error,
                "created_at": created_at,
                "metadata": metadata,
            }
        )
    return nodes


def _load_tool_call_state(con: sqlite3.Connection, session_id: str) -> list[dict]:
    out = []
    for tool_call_id, tool_call_json, tool_call_update_json in con.execute(
        "SELECT tool_call_id, tool_call_json, tool_call_update_json "
        "FROM tool_call_state WHERE session_id = ?",
        (session_id,),
    ):

        def _maybe_parse(s):
            if not s:
                return None
            try:
                return json.loads(s)
            except (json.JSONDecodeError, TypeError):
                return s

        out.append(
            {
                "tool_call_id": tool_call_id,
                "tool_call": _maybe_parse(tool_call_json),
                "tool_call_update": _maybe_parse(tool_call_update_json),
            }
        )
    return out


def _load_prompt_history(con: sqlite3.Connection, session_id: str) -> list[dict]:
    out = []
    for row_id, content, timestamp, is_shell in con.execute(
        "SELECT id, content, timestamp, is_shell FROM prompt_history WHERE session_id = ? ORDER BY id",
        (session_id,),
    ):
        out.append({"id": row_id, "content": content, "timestamp": timestamp, "is_shell": bool(is_shell)})
    return out


def extract(src: Path, out_path: Path) -> dict:
    snap = snapshot(src)
    stats = {"sessions": 0, "message_nodes": 0, "tool_call_state": 0, "prompt_history": 0}
    try:
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        sessions = con.execute(
            "SELECT id, working_directory, backend_type, model, agent_mode, created_at, "
            "last_activity_at, title, main_chain_id, shell_last_seen_index, cogs_json, "
            "workspace_dirs, hidden, metadata FROM sessions ORDER BY id"
        ).fetchall()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for s in sessions:
                session = dict(s)
                workspace_dirs = session.get("workspace_dirs")
                if workspace_dirs:
                    try:
                        session["workspace_dirs"] = json.loads(workspace_dirs)
                    except (json.JSONDecodeError, TypeError):
                        pass
                metadata = session.get("metadata")
                if metadata:
                    try:
                        session["metadata"] = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        pass

                message_nodes = _load_message_nodes(con, session["id"])
                tool_call_state = _load_tool_call_state(con, session["id"])
                prompt_history = _load_prompt_history(con, session["id"])

                record = {
                    "schema": SCHEMA_TAG,
                    "session": session,
                    "message_nodes": message_nodes,
                    "tool_call_state": tool_call_state,
                    "prompt_history": prompt_history,
                }
                fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
                fh.write("\n")

                stats["sessions"] += 1
                stats["message_nodes"] += len(message_nodes)
                stats["tool_call_state"] += len(tool_call_state)
                stats["prompt_history"] += len(prompt_history)

        con.close()
    finally:
        snap.unlink(missing_ok=True)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="Path to sessions.db")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "raw" / "00_inbox" / "devin_cli",
        help="raw/00_inbox/devin_cli root — a dated subdirectory is created under it",
    )
    args = parser.parse_args()

    if not args.src.exists():
        print(f"source not found: {args.src}", file=sys.stderr)
        return 1

    dated_dir = args.out_dir / date.today().isoformat()
    out_path = dated_dir / "sessions.jsonl"

    stats = extract(args.src, out_path)
    print(f"wrote {out_path}")
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

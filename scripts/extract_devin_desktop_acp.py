#!/usr/bin/env python3
"""Extract ~/Library/Application Support/Devin/User/acp-messages/*.db into
stable JSONL for the `devin_desktop_acp` adapter.

Architecture: same reasoning as extract_devin_cli.py -- one file per
conversation UUID, SQLite, WAL mode, actively written to (this script found
326 files at investigation time, up from 309 four days earlier). This
script is the only code that talks to those files directly; the adapter
only ever reads the JSONL this script emits.

Read safety: each .db snapshotted individually via SQLite's backup API
against a `mode=ro` connection (WAL-aware, atomic), source mtime/size
verified unchanged after every snapshot.

Output shape (task §3, stated here): **one JSONL file per extraction run,
one line per real conversation** (a file is skipped, not written, when
`SELECT COUNT(*) FROM messages` is 0 -- docs/reports/devin_desktop_acp_shapes_2026-09-09.md
§5: this is a clean, exhaustive predicate, no 1-11-message file exists
anywhere in the corpus to make it ambiguous). Skipped/empty files are not
silently lost -- they simply never produce a conversation, matching what
they are: aborted or never-started sessions, not content.

Per-conversation record includes, in full:
  - the source filename's UUID (the only stable identifier this store has --
    report §8.1: no session/conversation id exists anywhere inside the DB
    itself)
  - `meta.info` (parsed) -- title, configOptions (model/mode), availableCommands
  - `meta.schema_version`
  - every `messages` row, in `position` order, `payload` parsed from its
    JSON-string column into a real object

Stability: `json.dumps(..., sort_keys=True)`, no extraction-time timestamp
embedded in any record.
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

DEFAULT_SRC_DIR = Path("~/Library/Application Support/Devin/User/acp-messages").expanduser()
SCHEMA_TAG = "devin_desktop_acp_conversation_v1"


def _snapshot_one(src: Path) -> Path:
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlite", prefix="devin_acp_snapshot_")
    os.close(fd)
    tmp = Path(tmp_path)
    tmp.unlink()

    before = (src.stat().st_mtime, src.stat().st_size)
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(tmp)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()

    after = (src.stat().st_mtime, src.stat().st_size)
    if before != after:
        raise RuntimeError(
            f"source file {src} changed during snapshot ({before} -> {after}) "
            "— should be impossible with a mode=ro connection; aborting."
        )
    return tmp


def _maybe_parse(s):
    if not isinstance(s, str) or not s:
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def extract_one(db_path: Path) -> dict | None:
    uuid = db_path.stem
    snap = _snapshot_one(db_path)
    try:
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        message_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if message_count == 0:
            return None

        meta_rows = dict(con.execute("SELECT key, value FROM meta"))
        info = _maybe_parse(meta_rows.get("info"))

        messages = []
        for position, kind, payload in con.execute(
            "SELECT position, kind, payload FROM messages ORDER BY position"
        ):
            messages.append(
                {
                    "position": position,
                    "kind": kind,
                    "payload": _maybe_parse(payload),
                }
            )
        con.close()
    finally:
        snap.unlink(missing_ok=True)

    return {
        "schema": SCHEMA_TAG,
        "uuid": uuid,
        "meta": {
            "schema_version": meta_rows.get("schema_version"),
            "message_count": meta_rows.get("message_count"),
            "info": info,
        },
        "messages": messages,
        "source_mtime": None,  # filled by caller from the un-snapshotted original
    }


def extract(src_dir: Path, out_path: Path) -> dict:
    db_files = sorted(src_dir.glob("*.db"))
    stats = {"files_seen": len(db_files), "conversations_written": 0, "empty_skipped": 0, "messages_total": 0}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for f in db_files:
            record = extract_one(f)
            if record is None:
                stats["empty_skipped"] += 1
                continue
            # File mtime is the only source-side timestamp for a conversation
            # as a whole when no message-level timestamp survives (report
            # §7: per-message timestamps are sparse) -- captured post-hoc
            # from the original file (not the snapshot, which is a temp file
            # with its own mtime) so it's a real, stable fact about the source.
            record["source_mtime"] = f.stat().st_mtime
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
            fh.write("\n")
            stats["conversations_written"] += 1
            stats["messages_total"] += len(record["messages"])

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src-dir", type=Path, default=DEFAULT_SRC_DIR)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "raw" / "devin_desktop_acp",
    )
    args = parser.parse_args()

    if not args.src_dir.is_dir():
        print(f"source dir not found: {args.src_dir}", file=sys.stderr)
        return 1

    dated_dir = args.out_dir / date.today().isoformat()
    out_path = dated_dir / "conversations.jsonl"

    stats = extract(args.src_dir, out_path)
    print(f"wrote {out_path}")
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

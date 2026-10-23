#!/usr/bin/env python3
"""Extract Antigravity's modern conversation store into stable JSONL for the
`antigravity` adapter.

Findings this is built on: docs/reports/antigravity_shapes_2026-09-09.md.
Per explicit user instruction, scope is the *active* install only:

  - ~/.gemini/antigravity-ide/conversations/*.db  (Antigravity IDE.app v2.5.5)
  - ~/.gemini/antigravity-cli/conversations/*.db  (the `agy` CLI)

Both are per-conversation SQLite files (`trajectory_meta` + `steps` + a few
side-tables), always exactly one conversation per file, no branching. The
older `.pb` flat-file format and the older app variants' directories
(`~/.gemini/antigravity/`, `~/.gemini/antigravity-backup/`) are explicitly
out of scope this pass.

**No .proto schema ships with the app** (confirmed by grepping the entire
app bundle) -- `steps.step_payload` and friends are protobuf blobs this
project cannot fully decode field-by-name. Architecture decision, to keep
faith with this project's "raw is byte-faithful" rule (D3) despite that:
this script does NOT attempt to decode anything. It dumps every row of
every relevant table, blob columns base64-encoded, byte-for-byte. All
protobuf interpretation (the verified field paths in the report) happens
in the adapter at parse time, same separation of concerns as every other
adapter here -- the difference is only that decoding is schema-less
targeted-path protobuf reading instead of JSON key access.

Read safety: each .db snapshotted individually via SQLite's backup API
against a `mode=ro` connection; source mtime/size verified unchanged after.

Output shape: one JSONL file per extraction run, one line per real
conversation (`trajectory_meta` present AND `steps` non-empty -- report
SS3). `native_id` = the source filename's UUID (already globally unique).
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

DEFAULT_ROOTS = {
    "ide": Path("~/.gemini/antigravity-ide/conversations").expanduser(),
    "cli": Path("~/.gemini/antigravity-cli/conversations").expanduser(),
}
SCHEMA_TAG = "antigravity_conversation_v1"


def _snapshot(src: Path) -> Path:
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlite", prefix="antigravity_snapshot_")
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


def _b64(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        return base64.b64encode(v).decode("ascii")
    return v


def extract_one(db_path: Path, surface: str) -> dict | None:
    uuid = db_path.stem
    snap = _snapshot(db_path)
    try:
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)

        trajectory_meta = [
            {"trajectory_id": r[0], "cascade_id": r[1], "trajectory_type": r[2], "source": r[3]}
            for r in con.execute("SELECT trajectory_id, cascade_id, trajectory_type, source FROM trajectory_meta")
        ]

        steps = []
        for row in con.execute(
            "SELECT idx, step_type, status, has_subtrajectory, metadata, error_details, "
            "permissions, task_details, render_info, step_payload, step_format "
            "FROM steps ORDER BY idx"
        ):
            steps.append(
                {
                    "idx": row[0],
                    "step_type": row[1],
                    "status": row[2],
                    "has_subtrajectory": bool(row[3]),
                    "metadata": _b64(row[4]),
                    "error_details": _b64(row[5]),
                    "permissions": _b64(row[6]),
                    "task_details": _b64(row[7]),
                    "render_info": _b64(row[8]),
                    "step_payload": _b64(row[9]),
                    "step_format": row[10],
                }
            )

        if not steps:
            con.close()
            return None

        def dump_side_table(name, cols):
            out = []
            try:
                for row in con.execute(f"SELECT {', '.join(cols)} FROM {name}"):
                    out.append({c: _b64(v) if isinstance(v, bytes) else v for c, v in zip(cols, row)})
            except sqlite3.OperationalError:
                pass
            return out

        gen_metadata = dump_side_table("gen_metadata", ["idx", "data", "size"])
        executor_metadata = dump_side_table("executor_metadata", ["idx", "data"])
        parent_references = dump_side_table("parent_references", ["idx", "data"])
        trajectory_metadata_blob = dump_side_table("trajectory_metadata_blob", ["id", "data"])
        battle_mode_infos = dump_side_table("battle_mode_infos", ["idx", "data"])

        con.close()
    finally:
        snap.unlink(missing_ok=True)

    return {
        "schema": SCHEMA_TAG,
        "uuid": uuid,
        "surface": surface,  # "ide" | "cli" -- which store this came from
        "trajectory_meta": trajectory_meta,
        "steps": steps,
        "gen_metadata": gen_metadata,
        "executor_metadata": executor_metadata,
        "parent_references": parent_references,
        "trajectory_metadata_blob": trajectory_metadata_blob,
        "battle_mode_infos": battle_mode_infos,
        "source_mtime": db_path.stat().st_mtime,
    }


def extract(roots: dict[str, Path], out_path: Path) -> dict:
    stats = {"conversations_written": 0, "empty_skipped": 0, "by_surface": {}}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for surface, root in roots.items():
            if not root.is_dir():
                continue
            count = 0
            for f in sorted(root.glob("*.db")):
                record = extract_one(f, surface)
                if record is None:
                    stats["empty_skipped"] += 1
                    continue
                fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
                fh.write("\n")
                stats["conversations_written"] += 1
                count += 1
            stats["by_surface"][surface] = count
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ide-dir", type=Path, default=DEFAULT_ROOTS["ide"])
    parser.add_argument("--cli-dir", type=Path, default=DEFAULT_ROOTS["cli"])
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "raw" / "00_inbox" / "antigravity",
    )
    args = parser.parse_args()

    roots = {"ide": args.ide_dir, "cli": args.cli_dir}
    if not any(r.is_dir() for r in roots.values()):
        print(f"neither {args.ide_dir} nor {args.cli_dir} exist", file=sys.stderr)
        return 1

    dated_dir = args.out_dir / date.today().isoformat()
    out_path = dated_dir / "conversations.jsonl"

    stats = extract(roots, out_path)
    print(f"wrote {out_path}")
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

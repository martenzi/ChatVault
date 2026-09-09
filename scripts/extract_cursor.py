#!/usr/bin/env python3
"""Extract Cursor's current-version conversation stores into stable JSONL for
the `cursor` adapter.

Findings this is built on: docs/reports/cursor_shapes_2026-09-09.md. Four
stores were investigated; two are real sources, two are dead ends:

  - ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
    (composerHeaders + cursorDiskKV bubbles + composerData) -- the primary
    local conversation store. 257 MB, WAL, actively written to.
  - ~/Library/Application Support/Cursor/User/globalStorage/conversation-search.db
    -- a search index over the SAME local conversations. Its `source=
    'cloud-cache'` rows (8 total) were first thought to be cloud-only
    content (report's original §6); re-checked during this build and
    corrected (see the dated correction banner on the report): every one
    of the 8 has a matching LOCAL composer with real bubbles (the report's
    first check compared the full "bc-"-prefixed cloud id directly against
    bubble composerIds, which never carry that prefix, so it could only
    match by fluke). So this file contributes enrichment metadata
    (`cloud_agent_meta`, title fallback) to existing local conversations,
    never a standalone conversation. 3.6 MB, WAL.
  - ~/Library/Application Support/Cursor/User/workspaceStorage/*/state.vscdb
    (43 files) -- confirmed EMPTY of conversation content (report §2), not
    read here beyond nothing.
  - ~/Library/Application Support/Cursor/User/workspaceStorage/*/workspace.json
    (43 files, plain JSON, no SQLite concerns) -- read directly, to resolve
    composerHeaders.workspaceId -> a real project path.

Real-vs-empty predicate (report §5, verified exhaustively): a conversation
is real iff it has at least one `bubbleId:<composerId>:*` row -- NOT "has a
composerHeaders row" (134 of 162 composerHeaders rows are empty
placeholders; conversely 28 real composers have bubbles but no
composerHeaders row at all, having been pruned from that table while their
message data survived). Nothing here is a "temp cloud file" or transient
VM state: every conversation this script writes has real bubble content,
individually traceable back to actual message rows, not an index entry
taken on faith.

Read safety: every .db opened via SQLite's backup API against a `mode=ro`
connection (WAL-aware, atomic); source mtime/size verified unchanged after
each snapshot. `workspace.json` files are read directly (plain JSON, not a
live-write target during a running Cursor session in the same way the
SQLite stores are).

Output shape (one choice, stated here per the project's established
convention): **one JSONL file per extraction run, one line per real
conversation**. `native_id` = the local `composerId` (already globally
unique -- UUIDs; subagent composerIds are their own independent UUIDs,
report §9 -- no synthetic suffixing needed anywhere on this platform,
unlike claude_code's subagent handling).

Per-conversation record includes, in full:
  - `composer_header` (the composerHeaders row, parsed `value` JSON) if one
    exists, else null (report §5's 28 "extra" real composers)
  - `composer_data` (the composerData row, incl. `modelConfig`,
    `fullConversationHeadersOnly`, `subagentComposerIds`) if one exists
  - `bubbles`: every bubbleId:<composerId>:* row for this composer, parsed,
    in bubbleId order (a stable sort key -- see below) -- the FULL set,
    orphaned edit-history included (report §8); the adapter decides what
    subset becomes `messages`
  - `workspace_path`: resolved via composerHeaders.workspaceId -> that
    workspace's workspace.json, or null
  - `search_index`/`cloud_agent_meta`: present only when this composer is
    also tracked as a cloud Background Agent run (see above) -- enrichment,
    never the primary content source

Stability: `json.dumps(..., sort_keys=True)`; bubbles sorted by their own
`bubbleId` string (stable regardless of dict iteration order or re-run
timing) rather than by insertion order in cursorDiskKV, which is not itself
stable across VACUUM/compaction.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

DEFAULT_GLOBAL_STORAGE = Path("~/Library/Application Support/Cursor/User/globalStorage").expanduser()
DEFAULT_WORKSPACE_STORAGE = Path("~/Library/Application Support/Cursor/User/workspaceStorage").expanduser()
SCHEMA_TAG = "cursor_conversation_v1"


def _snapshot(src: Path) -> Path:
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlite", prefix="cursor_snapshot_")
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


def _maybe_parse(v):
    if v is None:
        return None
    s = v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
    if not isinstance(s, str) or not s:
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def _load_workspace_paths(workspace_storage_dir: Path) -> dict[str, str]:
    """workspaceId -> real project path, from each workspace's own
    workspace.json (report SS10). Not SQLite -- a plain read is safe."""
    out: dict[str, str] = {}
    for wd in sorted(workspace_storage_dir.glob("*")):
        wj = wd / "workspace.json"
        if not wj.exists():
            continue
        try:
            data = json.loads(wj.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        uri = data.get("workspace") or data.get("folder")
        if not uri:
            continue
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            out[wd.name] = unquote(parsed.path)
    return out


def _extract_local_conversations(global_storage: Path, workspace_paths: dict[str, str]) -> list[dict]:
    snap = _snapshot(global_storage / "state.vscdb")
    try:
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)

        headers_by_cid: dict[str, dict] = {}
        for row in con.execute(
            "SELECT composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, "
            "isSubagent, recency, checkpointAt, value, subagentTypeName FROM composerHeaders"
        ):
            cid, workspace_id, created_at, last_updated_at, is_archived, is_subagent, recency, checkpoint_at, value, subagent_type = row
            headers_by_cid[cid] = {
                "composerId": cid,
                "workspaceId": workspace_id,
                "createdAt": created_at,
                "lastUpdatedAt": last_updated_at,
                "isArchived": bool(is_archived),
                "isSubagent": bool(is_subagent),
                "recency": recency,
                "checkpointAt": checkpoint_at,
                "subagentTypeName": subagent_type,
                "value": _maybe_parse(value),
            }

        composer_data_by_cid: dict[str, dict] = {}
        for key, val in con.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
            obj = _maybe_parse(val)
            if isinstance(obj, dict) and obj.get("composerId"):
                composer_data_by_cid[obj["composerId"]] = obj

        bubbles_by_cid: dict[str, list[dict]] = {}
        for key, val in con.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
            parts = key.split(":", 2)
            if len(parts) != 3:
                continue
            _, cid, bubble_uuid = parts
            obj = _maybe_parse(val)
            if not isinstance(obj, dict):
                continue
            bubbles_by_cid.setdefault(cid, []).append(obj)

        con.close()
    finally:
        snap.unlink(missing_ok=True)

    records = []
    for cid, bubbles in bubbles_by_cid.items():
        bubbles.sort(key=lambda b: b.get("bubbleId") or "")
        header = headers_by_cid.get(cid)
        composer_data = composer_data_by_cid.get(cid)
        workspace_id = header.get("workspaceId") if header else None
        records.append(
            {
                "schema": SCHEMA_TAG,
                "source": "local",
                "composer_id": cid,
                "composer_header": header,
                "composer_data": composer_data,
                "bubbles": bubbles,
                "workspace_path": workspace_paths.get(workspace_id) if workspace_id else None,
                "search_index": None,
                "cloud_agent_meta": None,
            }
        )
    return records


def _load_cloud_agent_meta(global_storage: Path) -> dict[str, dict]:
    """ItemTable's cloudAgentRepository.agents* keys hold arrays of cloud
    Background Agent metadata (title/model/workspace binding/status),
    keyed by bcId -- report SS6."""
    snap = _snapshot(global_storage / "state.vscdb")
    out: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        for key, val in con.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'cloudAgentRepository.agents%'"):
            agents = _maybe_parse(val)
            if not isinstance(agents, list):
                continue
            for agent in agents:
                if isinstance(agent, dict) and agent.get("bcId"):
                    out[agent["bcId"]] = agent
        con.close()
    finally:
        snap.unlink(missing_ok=True)
    return out


def _extract_cloud_cache_conversations(global_storage: Path) -> list[dict]:
    snap = _snapshot(global_storage / "conversation-search.db")
    records_raw = []
    try:
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT conv.id, conv.fts_rowid, conv.title, conv.updated_at, conv.is_archived, "
            "conv.branches, c.c1 "
            "FROM conversations conv JOIN conversation_fts_content c ON c.id = conv.fts_rowid "
            "WHERE conv.source = 'cloud-cache'"
        ).fetchall()
        for cid, fts_rowid, title, updated_at, is_archived, branches, body in rows:
            if not body:
                continue  # report SS5/SS6: empty body = not a real conversation
            records_raw.append(
                {
                    "id": cid,
                    "title": title,
                    "updated_at": updated_at,
                    "is_archived": bool(is_archived),
                    "branches": branches,
                    "body": body,
                }
            )
        con.close()
    finally:
        snap.unlink(missing_ok=True)

    cloud_agent_meta = _load_cloud_agent_meta(global_storage)

    records = []
    for r in records_raw:
        records.append(
            {
                "schema": SCHEMA_TAG,
                "source": "cloud-cache",
                "composer_id": r["id"],
                "composer_header": None,
                "composer_data": None,
                "bubbles": [],
                "workspace_path": None,
                "search_index": {
                    "title": r["title"],
                    "updated_at": r["updated_at"],
                    "is_archived": r["is_archived"],
                    "branches": r["branches"],
                    "body": r["body"],
                },
                "cloud_agent_meta": cloud_agent_meta.get(r["id"]),
            }
        )
    return records


def extract(global_storage: Path, workspace_storage: Path, out_path: Path) -> dict:
    workspace_paths = _load_workspace_paths(workspace_storage)
    local = _extract_local_conversations(global_storage, workspace_paths)
    cloud = _extract_cloud_cache_conversations(global_storage)

    # Corrected during the build pass (a dated correction banner was added
    # to docs/reports/cursor_shapes_2026-09-09.md): the report's first pass
    # checked "bc-<uuid>" directly against bubble composerIds and concluded
    # only 1 of 8 cloud-cache entries had local bubble data. That check was
    # wrong -- bubbleId keys never carry the "bc-" prefix, so a direct
    # string match could only ever succeed by fluke. Stripping "bc-" and
    # re-checking shows **all 8** cloud-cache entries have a matching LOCAL
    # composer (bare uuid, real composerHeaders row, real bubbles) -- there
    # is no cloud-only content on this machine. So cloud-cache entries never
    # become their own conversation record; they only enrich an existing
    # local one with `cloud_agent_meta` (status/isKilled/modelDetails/
    # workspaceRootPath -- not otherwise available locally) and the search
    # index's title as a fallback. A cloud-cache id with NO local bubble
    # match (never observed here, 0/8) is intentionally dropped rather than
    # ingested from the flattened FTS body alone -- flagged here, not
    # silently handled, since a second lower-fidelity parse path with zero
    # real examples to validate against would be guessing, not evidence.
    local_by_cid = {r["composer_id"]: r for r in local}
    unmatched_cloud_only = []
    for c in cloud:
        bare_cid = c["composer_id"][3:] if c["composer_id"].startswith("bc-") else c["composer_id"]
        local_match = local_by_cid.get(bare_cid)
        if local_match:
            local_match["search_index"] = c["search_index"]
            local_match["cloud_agent_meta"] = c["cloud_agent_meta"]
        else:
            unmatched_cloud_only.append(c["composer_id"])

    all_records = list(local_by_cid.values())
    all_records.sort(key=lambda r: r["composer_id"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in all_records:
            fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False))
            fh.write("\n")

    return {
        "conversations_written": len(all_records),
        "enriched_with_cloud_agent_meta": sum(1 for r in all_records if r.get("cloud_agent_meta")),
        "unmatched_cloud_only_dropped": unmatched_cloud_only,
        "workspace_paths_resolved": len(workspace_paths),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--global-storage", type=Path, default=DEFAULT_GLOBAL_STORAGE)
    parser.add_argument("--workspace-storage", type=Path, default=DEFAULT_WORKSPACE_STORAGE)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "raw" / "cursor",
    )
    args = parser.parse_args()

    if not (args.global_storage / "state.vscdb").exists():
        print(f"state.vscdb not found under {args.global_storage}", file=sys.stderr)
        return 1

    dated_dir = args.out_dir / date.today().isoformat()
    out_path = dated_dir / "conversations.jsonl"

    stats = extract(args.global_storage, args.workspace_storage, out_path)
    print(f"wrote {out_path}")
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

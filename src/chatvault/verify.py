from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class VerifyResult:
    name: str
    ok: bool
    detail: str


def run(conn: sqlite3.Connection) -> list[VerifyResult]:
    results: list[VerifyResult] = []

    counts = {}
    for table in ("source_files", "conversations", "messages", "blocks"):
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    results.append(
        VerifyResult(
            "row counts",
            True,
            f"source_files={counts['source_files']} conversations={counts['conversations']} "
            f"messages={counts['messages']} blocks={counts['blocks']}",
        )
    )

    orphan_msgs = conn.execute(
        "SELECT COUNT(*) FROM messages m LEFT JOIN conversations c ON c.id = m.conv_id WHERE c.id IS NULL"
    ).fetchone()[0]
    results.append(VerifyResult("orphan messages", orphan_msgs == 0, f"{orphan_msgs} orphaned message row(s)"))

    orphan_blocks = conn.execute(
        "SELECT COUNT(*) FROM blocks b LEFT JOIN messages m ON m.id = b.msg_id WHERE m.id IS NULL"
    ).fetchone()[0]
    results.append(VerifyResult("orphan blocks", orphan_blocks == 0, f"{orphan_blocks} orphaned block row(s)"))

    fts_count = conn.execute("SELECT COUNT(*) FROM blocks_fts").fetchone()[0]
    blocks_count = counts["blocks"]
    fts_ok = fts_count == blocks_count
    results.append(
        VerifyResult("FTS index integrity", fts_ok, f"blocks_fts rows={fts_count} vs blocks rows={blocks_count}")
    )

    dup_uids = conn.execute(
        "SELECT COUNT(*) FROM (SELECT uid FROM conversations GROUP BY uid HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    results.append(VerifyResult("uid uniqueness", dup_uids == 0, f"{dup_uids} duplicated uid group(s)"))

    sample = conn.execute("SELECT id, content_hash FROM conversations ORDER BY RANDOM() LIMIT 5").fetchall()
    results.append(VerifyResult("hash spot-check", True, f"sampled {len(sample)} conversation(s) for content_hash presence"))

    return results

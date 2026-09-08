from __future__ import annotations

import sqlite3

# `brain ledger` (docs/51_SPEC_brain_parity_and_naming.md §2.1
# `ingest`/.../`ledger` row): "add `brain ledger` — a provenance table of
# where entries came from (chatvault copy, summarise, import markdown, jot,
# manual), which is the genuinely useful half [of ChatVault's ingestion
# ledger machinery]." Reads `items.origin_detail`
# (migrations/brain/005_brain_parity.sql), stamped by copy
# conversation/copy stashed (chatvault/brain/entries.py) and summarise
# conversation/summarise entry (chatvault/brain/distill.py). Items created
# before this column existed, or by a code path that doesn't stamp it yet
# (add entry, jot, import), report as "(unknown)" rather than being
# silently omitted from the count — nothing here is guessed.


def report(conn: sqlite3.Connection) -> list[dict]:
    """One row per distinct `origin_detail` value, with a count, ordered by
    count descending. `(unknown)` groups every item whose origin_detail is
    NULL."""
    rows = conn.execute(
        """
        SELECT COALESCE(origin_detail, '(unknown)') AS origin, COUNT(*) AS n
        FROM items
        GROUP BY origin
        ORDER BY n DESC, origin
        """
    ).fetchall()
    return [{"origin": r["origin"], "count": r["n"]} for r in rows]

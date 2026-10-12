from __future__ import annotations

import html
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _message_hashes(conn: sqlite3.Connection, conv_id: int) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT content_hash FROM messages WHERE conv_id = ? ORDER BY idx", (conv_id,)
        ).fetchall()
    ]


def _prefix_containment(conn: sqlite3.Connection, id_a: int, id_b: int) -> tuple[bool, int | None, int | None]:
    """Returns (verified, shorter_id, longer_id). verified is True iff the
    ordered message content_hash list of the shorter conversation is an
    exact prefix of the longer one's, per Patch 1's deterministic test."""
    hashes_a = _message_hashes(conn, id_a)
    hashes_b = _message_hashes(conn, id_b)
    if len(hashes_a) == len(hashes_b):
        return False, None, None
    shorter_id, shorter, longer_id, longer = (
        (id_a, hashes_a, id_b, hashes_b) if len(hashes_a) < len(hashes_b) else (id_b, hashes_b, id_a, hashes_a)
    )
    verified = longer[: len(shorter)] == shorter
    return verified, shorter_id, longer_id


def scan_candidates(conn: sqlite3.Connection) -> int:
    """Finds conversations sharing a head_hash across different native_ids
    or platforms (loader.py only ever resolves same-uid continuations —
    this is the cross-conversation case) and inserts dedupe_candidates rows.
    Idempotent: re-running never resets an applied/dismissed candidate."""
    groups = conn.execute(
        """
        SELECT head_hash FROM conversations
        WHERE superseded_by IS NULL AND head_hash IS NOT NULL
        GROUP BY head_hash HAVING COUNT(*) > 1
        """
    ).fetchall()

    inserted = 0
    now = _now()
    for (head_hash,) in groups:
        rows = conn.execute(
            """
            SELECT id, platform, native_id FROM conversations
            WHERE head_hash = ? AND superseded_by IS NULL ORDER BY id
            """,
            (head_hash,),
        ).fetchall()
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a["platform"] == b["platform"] and a["native_id"] == b["native_id"]:
                    continue  # same uid family — already handled by loader.py
                lo_id, hi_id = sorted((a["id"], b["id"]))
                exists = conn.execute(
                    "SELECT 1 FROM dedupe_candidates WHERE conv_id_a = ? AND conv_id_b = ?",
                    (lo_id, hi_id),
                ).fetchone()
                if exists:
                    continue
                verified, _, _ = _prefix_containment(conn, lo_id, hi_id)
                reason = "cross_source" if verified else "head_match_unverified"
                conn.execute(
                    """
                    INSERT INTO dedupe_candidates
                        (conv_id_a, conv_id_b, reason, confidence, detail_json, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        lo_id,
                        hi_id,
                        reason,
                        1.0 if verified else 0.5,
                        json.dumps({"head_hash": head_hash}),
                        now,
                    ),
                )
                inserted += 1
    conn.commit()
    return inserted


def apply_candidate(conn: sqlite3.Connection, candidate_id: int) -> tuple[bool, str]:
    """Re-verifies containment (never trusts a stale candidate row). On
    success, sets conversations.superseded_by ONLY — never touches blocks or
    excluded. Returns (applied, detail)."""
    cand = conn.execute("SELECT * FROM dedupe_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if cand is None:
        return False, f"no candidate with id={candidate_id}"
    if cand["status"] != "open":
        return False, f"candidate {candidate_id} is already {cand['status']}"

    verified, shorter_id, longer_id = _prefix_containment(conn, cand["conv_id_a"], cand["conv_id_b"])
    if not verified:
        return False, "containment not verified — left open"

    now = _now()
    conn.execute("UPDATE conversations SET superseded_by = ? WHERE id = ?", (longer_id, shorter_id))
    conn.execute(
        """
        INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at)
        VALUES ('conversation', ?, 'supersede', ?, ?, ?)
        """,
        (shorter_id, "dedupe --apply", f"candidate:{candidate_id}", now),
    )
    conn.execute(
        "UPDATE dedupe_candidates SET status = 'applied', resolved_at = ? WHERE id = ?",
        (now, candidate_id),
    )
    conn.commit()
    return True, f"conversation {shorter_id} superseded_by {longer_id}"


def apply_all(conn: sqlite3.Connection) -> list[tuple[int, bool, str]]:
    open_ids = [
        row[0] for row in conn.execute("SELECT id FROM dedupe_candidates WHERE status = 'open'").fetchall()
    ]
    results = []
    for cid in open_ids:
        applied, detail = apply_candidate(conn, cid)
        results.append((cid, applied, detail))
    return results


def supersede(conn: sqlite3.Connection, uid_a: str, uid_b: str) -> int:
    """Manual override: mark uid_a as superseded by uid_b, any pair, logged.
    Reuses apply_candidate's code path (via a synthetic candidate row) so
    --undo works uniformly for automatic and manual supersedes."""
    row_a = conn.execute("SELECT id FROM conversations WHERE uid = ?", (uid_a,)).fetchone()
    row_b = conn.execute("SELECT id FROM conversations WHERE uid = ?", (uid_b,)).fetchone()
    if row_a is None:
        raise ValueError(f"no conversation with uid {uid_a!r}")
    if row_b is None:
        raise ValueError(f"no conversation with uid {uid_b!r}")

    id_a, id_b = row_a["id"], row_b["id"]
    lo_id, hi_id = sorted((id_a, id_b))
    now = _now()
    conn.execute(
        """
        INSERT INTO dedupe_candidates (conv_id_a, conv_id_b, reason, confidence, detail_json, status, created_at)
        VALUES (?, ?, 'cross_source', 1.0, ?, 'open', ?)
        ON CONFLICT(conv_id_a, conv_id_b) DO UPDATE SET status = 'open' WHERE status = 'dismissed'
        """,
        (lo_id, hi_id, json.dumps({"manual_override": True}), now),
    )
    cand = conn.execute(
        "SELECT id FROM dedupe_candidates WHERE conv_id_a = ? AND conv_id_b = ?", (lo_id, hi_id)
    ).fetchone()
    candidate_id = cand["id"]

    conn.execute("UPDATE conversations SET superseded_by = ? WHERE id = ?", (id_b, id_a))
    conn.execute(
        """
        INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at)
        VALUES ('conversation', ?, 'supersede', ?, ?, ?)
        """,
        (id_a, "dedupe --supersede (manual)", f"candidate:{candidate_id}", now),
    )
    conn.execute(
        "UPDATE dedupe_candidates SET status = 'applied', resolved_at = ? WHERE id = ?",
        (now, candidate_id),
    )
    conn.commit()
    return candidate_id


def dismiss(conn: sqlite3.Connection, candidate_id: int, reason: str) -> None:
    cand = conn.execute("SELECT detail_json FROM dedupe_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if cand is None:
        raise ValueError(f"no candidate with id={candidate_id}")
    detail = json.loads(cand["detail_json"] or "{}")
    detail["dismiss_reason"] = reason
    conn.execute(
        "UPDATE dedupe_candidates SET status = 'dismissed', resolved_at = ?, detail_json = ? WHERE id = ?",
        (_now(), json.dumps(detail), candidate_id),
    )
    conn.commit()


def undo(conn: sqlite3.Connection, candidate_id: int) -> None:
    row = conn.execute(
        "SELECT target_id FROM exclusion_log WHERE action = 'supersede' AND rule = ? ORDER BY id DESC LIMIT 1",
        (f"candidate:{candidate_id}",),
    ).fetchone()
    if row is None:
        raise ValueError(f"no supersede action found for candidate {candidate_id}")
    now = _now()
    conn.execute("UPDATE conversations SET superseded_by = NULL WHERE id = ?", (row["target_id"],))
    conn.execute(
        """
        INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at)
        VALUES ('conversation', ?, 'unsupersede', 'dedupe --undo', ?, ?)
        """,
        (row["target_id"], f"candidate:{candidate_id}", now),
    )
    conn.execute(
        "UPDATE dedupe_candidates SET status = 'open', resolved_at = NULL WHERE id = ?",
        (candidate_id,),
    )
    conn.commit()


def backfill_snapshots(conn: sqlite3.Connection, apply: bool = False) -> dict:
    """Retroactive version of the loader's snapshot logic
    (docs/reports/snapshot_duplication_2026-10-12.md §5.4). Within each
    platform+native_id family of live rows: a byte-identical copy is superseded
    by the lowest id; a strict message-hash prefix is superseded by the longest
    live row containing it. Never deletes (D3) and never touches blocks.
    Families still holding more than one live row afterwards are divergent;
    their live rows get review/duplicate-ambiguous (apply only).
    Returns counts; with apply=False nothing is written."""
    from chatvault import tags as tags_mod

    result = {"families": 0, "identical": 0, "prefix": 0, "ambiguous_families": 0, "ambiguous_rows": 0}
    now = _now()
    fams = conn.execute(
        """
        SELECT platform, native_id FROM conversations
        WHERE superseded_by IS NULL AND native_id IS NOT NULL
        GROUP BY platform, native_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    for fam in fams:
        rows = conn.execute(
            "SELECT id, content_hash, message_count FROM conversations "
            "WHERE platform = ? AND native_id = ? AND superseded_by IS NULL ORDER BY id",
            (fam["platform"], fam["native_id"]),
        ).fetchall()
        result["families"] += 1
        live = {r["id"]: r for r in rows}
        moves: list[tuple[int, int, str]] = []
        seen_hash: dict[str, int] = {}
        for r in rows:
            keep = seen_hash.setdefault(r["content_hash"], r["id"])
            if keep != r["id"]:
                moves.append((r["id"], keep, "identical"))
                del live[r["id"]]
        hashes = {i: _message_hashes(conn, i) for i in live}
        for i in sorted(live, key=lambda i: len(hashes[i])):
            containers = [
                j for j in live
                if j != i and len(hashes[j]) > len(hashes[i]) and hashes[j][: len(hashes[i])] == hashes[i]
            ]
            if containers:
                moves.append((i, max(containers, key=lambda j: len(hashes[j])), "prefix"))
        moved = {m[0] for m in moves}
        remaining = [i for i in live if i not in moved]
        for target, by, kind in moves:
            result[kind] += 1
            if apply:
                conn.execute("UPDATE conversations SET superseded_by = ? WHERE id = ?", (by, target))
                conn.execute(
                    """
                    INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at)
                    VALUES ('conversation', ?, 'supersede', ?, ?, ?)
                    """,
                    (target, f"backfill: {kind} of conversation {by}", f"backfill:snapshot-{kind}", now),
                )
        if len(remaining) > 1:
            result["ambiguous_families"] += 1
            result["ambiguous_rows"] += len(remaining)
            if apply:
                tag_id = tags_mod.get_or_create_tag(conn, "review/duplicate-ambiguous")
                for i in remaining:
                    conn.execute(
                        """
                        INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
                        VALUES (?, 'conversation', ?, 'system', ?)
                        ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
                        """,
                        (tag_id, i, now),
                    )
    if apply:
        conn.commit()
    return result


_WS = re.compile(r"\s+")
_PUNCT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u00a0": " "})


def _body(conn: sqlite3.Connection, conv_id: int, tight: bool) -> str:
    """Block text in (msg.idx, block.idx) order, whitespace-collapsed. `tight`
    additionally decodes HTML entities, folds Unicode/punctuation variants and
    ignores `system` blocks (the hermes .jsonl copy leads with a
    "session_meta: N tool(s) available" block the .json copy lacks; verified
    on 9 real families). See docs/reports/snapshot_duplication_2026-10-12.md §8."""
    rows = conn.execute(
        "SELECT b.text FROM blocks b JOIN messages m ON m.id = b.msg_id "
        "WHERE m.conv_id = ? AND (? = 0 OR b.type != 'system') ORDER BY m.idx, b.idx",
        (conv_id, int(tight)),
    ).fetchall()
    text = "\n".join(r[0] or "" for r in rows)
    if tight:
        text = unicodedata.normalize("NFKC", html.unescape(text)).translate(_PUNCT)
    return _WS.sub(" ", text).strip()


def sweep_cross_format(conn: sqlite3.Connection, apply: bool = False) -> dict:
    """Second pass over families that backfill_snapshots left divergent
    (§8). For two-row families only: if one row's normalised body appears
    verbatim inside the other's, the shorter is a lossy/partial copy of the
    same conversation and is superseded by the longer. Tier 1 compares
    whitespace-collapsed text; tier 2 also entity-decodes and folds punctuation.
    Anything else is left live and tagged — disjoint or genuinely lossy copies
    need an adapter diagnosis or a human, not a heuristic. Never deletes."""
    out = {"families": 0, "tier1": 0, "tier2": 0, "remaining": [], "untagged": 0}
    now = _now()
    tag_id = conn.execute("SELECT id FROM tags WHERE name = 'review/duplicate-ambiguous'").fetchone()
    if tag_id is None:
        return out
    fams = conn.execute(
        """
        SELECT platform, native_id FROM conversations
        WHERE superseded_by IS NULL AND native_id IS NOT NULL
        GROUP BY platform, native_id HAVING COUNT(*) = 2
        """
    ).fetchall()
    for fam in fams:
        a, b = conn.execute(
            "SELECT id, uid FROM conversations WHERE platform = ? AND native_id = ? AND superseded_by IS NULL ORDER BY id",
            (fam["platform"], fam["native_id"]),
        ).fetchall()
        if not all(
            conn.execute(
                "SELECT 1 FROM tag_links WHERE tag_id = ? AND target_type = 'conversation' AND target_id = ?",
                (tag_id[0], r["id"]),
            ).fetchone()
            for r in (a, b)
        ):
            continue
        out["families"] += 1
        verdict = None
        for tier, tight in (("tier1", False), ("tier2", True)):
            ba, bb = _body(conn, a["id"], tight), _body(conn, b["id"], tight)
            (s_row, l_row), (s_txt, l_txt) = ((a, b), (ba, bb)) if len(ba) <= len(bb) else ((b, a), (bb, ba))
            if s_txt and s_txt in l_txt:
                verdict = (tier, s_row, l_row)
                break
        if verdict is None:
            out["remaining"].append((fam["platform"], fam["native_id"], a["id"], b["id"]))
            continue
        tier, s_row, l_row = verdict
        out[tier] += 1
        if apply:
            conn.execute("UPDATE conversations SET superseded_by = ? WHERE id = ?", (l_row["id"], s_row["id"]))
            conn.execute(
                """
                INSERT INTO exclusion_log (target_type, target_id, action, reason, rule, created_at)
                VALUES ('conversation', ?, 'supersede', ?, ?, ?)
                """,
                (s_row["id"], f"sweep: body contained in conversation {l_row['id']}", f"sweep:body-subset-{tier}", now),
            )
            # The surviving row is no longer ambiguous.
            conn.execute(
                "DELETE FROM tag_links WHERE tag_id = ? AND target_type = 'conversation' AND target_id = ?",
                (tag_id[0], l_row["id"]),
            )
            out["untagged"] += 1
    if apply:
        conn.commit()
    return out


def report(conn: sqlite3.Connection) -> dict:
    superseded = conn.execute(
        "SELECT id, uid, superseded_by FROM conversations WHERE superseded_by IS NOT NULL"
    ).fetchall()
    flagged = conn.execute("SELECT id, uid FROM conversations WHERE uid LIKE '%#dup-%'").fetchall()
    repeated_blocks = conn.execute(
        """
        SELECT content_hash, COUNT(*) AS n
        FROM blocks WHERE excluded = 0
        GROUP BY content_hash HAVING COUNT(*) > 20
        ORDER BY n DESC
        """
    ).fetchall()
    candidates = conn.execute(
        "SELECT status, reason, COUNT(*) AS n FROM dedupe_candidates GROUP BY status, reason"
    ).fetchall()
    return {
        "superseded": superseded,
        "flagged_duplicates": flagged,
        "repeated_blocks": repeated_blocks,
        "candidates": candidates,
    }

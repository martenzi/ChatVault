from __future__ import annotations

from chatvault import dedupe, ingest
from tests.conftest import FIXTURES


def test_cross_source_superset_creates_candidate_and_apply_supersedes(conn):
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_a_short.jsonl")
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_b_superset.jsonl")

    cands = conn.execute("SELECT * FROM dedupe_candidates").fetchall()
    assert len(cands) == 1
    assert cands[0]["reason"] == "cross_source"
    assert cands[0]["status"] == "open"

    results = dedupe.apply_all(conn)
    assert len(results) == 1
    cid, applied, _ = results[0]
    assert applied is True

    short = conn.execute("SELECT id, superseded_by FROM conversations WHERE native_id = 'dedupe-a'").fetchone()
    long = conn.execute("SELECT id FROM conversations WHERE native_id = 'dedupe-b'").fetchone()
    assert short["superseded_by"] == long["id"]

    visible = conn.execute("SELECT native_id FROM v_conversations").fetchall()
    assert len(visible) == 1
    assert visible[0]["native_id"] == "dedupe-b"

    dedupe.undo(conn, cid)
    visible_after_undo = conn.execute("SELECT native_id FROM v_conversations ORDER BY native_id").fetchall()
    assert [r["native_id"] for r in visible_after_undo] == ["dedupe-a", "dedupe-b"]


def test_divergent_head_match_stays_open_after_apply(conn):
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_a_short.jsonl")
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_c_divergent.jsonl")

    cand = conn.execute("SELECT * FROM dedupe_candidates").fetchone()
    assert cand["reason"] == "head_match_unverified"
    assert cand["status"] == "open"

    dedupe.apply_all(conn)

    cand_after = conn.execute("SELECT status FROM dedupe_candidates WHERE id = ?", (cand["id"],)).fetchone()
    assert cand_after["status"] == "open"

    rows = conn.execute("SELECT superseded_by FROM conversations").fetchall()
    assert all(r["superseded_by"] is None for r in rows)


def test_scan_is_idempotent(conn):
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_a_short.jsonl")
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_b_superset.jsonl")

    n1 = conn.execute("SELECT COUNT(*) FROM dedupe_candidates").fetchone()[0]
    dedupe.scan_candidates(conn)
    n2 = conn.execute("SELECT COUNT(*) FROM dedupe_candidates").fetchone()[0]
    assert n1 == n2 == 1

    dupes = conn.execute(
        "SELECT conv_id_a, conv_id_b, COUNT(*) AS n FROM dedupe_candidates GROUP BY conv_id_a, conv_id_b HAVING COUNT(*) > 1"
    ).fetchall()
    assert dupes == []


def test_manual_supersede_and_undo(conn):
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_a_short.jsonl")
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_c_divergent.jsonl")

    uid_a = conn.execute("SELECT uid FROM conversations WHERE native_id = 'dedupe-a'").fetchone()["uid"]
    uid_c = conn.execute("SELECT uid FROM conversations WHERE native_id = 'dedupe-c'").fetchone()["uid"]

    cid = dedupe.supersede(conn, uid_a, uid_c)
    row = conn.execute("SELECT superseded_by FROM conversations WHERE uid = ?", (uid_a,)).fetchone()
    assert row["superseded_by"] is not None

    dedupe.undo(conn, cid)
    row_after = conn.execute("SELECT superseded_by FROM conversations WHERE uid = ?", (uid_a,)).fetchone()
    assert row_after["superseded_by"] is None


def test_dismiss_stops_reflagging(conn):
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_a_short.jsonl")
    ingest.ingest_path(conn, FIXTURES / "dedupe" / "native_c_divergent.jsonl")

    cand = conn.execute("SELECT id FROM dedupe_candidates").fetchone()
    dedupe.dismiss(conn, cand["id"], "not actually a duplicate")

    dedupe.scan_candidates(conn)
    status = conn.execute("SELECT status FROM dedupe_candidates WHERE id = ?", (cand["id"],)).fetchone()["status"]
    assert status == "dismissed"

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chatvault import hashing


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _walk_root(root: Path) -> tuple[set[str], set[str]]:
    """Returns (sha256s, resolved paths) of every file under root. Empty sets
    if root no longer exists on disk — that's the whole point of reconcile."""
    shas: set[str] = set()
    paths: set[str] = set()
    if not root.is_dir():
        return shas, paths
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        paths.add(str(p.resolve()))
        shas.add(hashing.file_sha256(p))
    return shas, paths


@dataclass
class ReconcileReport:
    run_at: str
    roots: list[str] = field(default_factory=list)
    checked: int = 0
    still_present: int = 0
    newly_missing: int = 0
    reappeared: int = 0
    missing: list[dict] = field(default_factory=list)
    reappeared_detail: list[dict] = field(default_factory=list)


def _scoped_root_dirs(conn: sqlite3.Connection, roots: list[str] | None, kind: str) -> list[str]:
    where = ["presence != 'retired'", "root_dir IS NOT NULL"]
    params: list[str] = []
    if kind != "all":
        where.append("kind = ?")
        params.append(kind)
    if roots:
        placeholders = ",".join("?" for _ in roots)
        where.append(f"root_dir IN ({placeholders})")
        params.extend(roots)
    sql = f"SELECT DISTINCT root_dir FROM source_files WHERE {' AND '.join(where)}"  # noqa: S608
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def reconcile(
    conn: sqlite3.Connection,
    roots: list[str] | None = None,
    kind: str = "live",
    dry_run: bool = False,
) -> ReconcileReport:
    """Diff DB-tracked source_files against what's actually on disk. Default
    scope is kind='live' — frozen sources are archived on purpose and should
    never be flagged as missing. Never deletes a row or touches ingested
    content; this only updates presence metadata."""
    root_dirs = _scoped_root_dirs(conn, roots, kind)
    now = _now()
    report = ReconcileReport(run_at=now, roots=root_dirs)

    for root_dir in root_dirs:
        disk_shas, disk_paths = _walk_root(Path(root_dir))

        q = "SELECT id, path, sha256, presence FROM source_files WHERE root_dir = ? AND presence != 'retired'"
        qparams: list[str] = [root_dir]
        if kind != "all":
            q += " AND kind = ?"
            qparams.append(kind)
        rows = conn.execute(q, qparams).fetchall()

        for row in rows:
            report.checked += 1
            found = row["sha256"] in disk_shas or row["path"] in disk_paths

            if found:
                report.still_present += 1
                if row["presence"] == "missing":
                    report.reappeared += 1
                    report.reappeared_detail.append({"id": row["id"], "path": row["path"]})
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE source_files
                        SET presence = 'present', last_seen_at = ?, missing_since = NULL,
                            first_seen_at = COALESCE(first_seen_at, ingested_at)
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )
            else:
                if row["presence"] != "missing":
                    report.newly_missing += 1
                report.missing.append({"id": row["id"], "path": row["path"]})
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE source_files
                        SET presence = 'missing', missing_since = COALESCE(missing_since, ?)
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )

        if not dry_run:
            conn.commit()

    if not dry_run:
        persist(conn, report)

    return report


def retire(conn: sqlite3.Connection, path_or_root: str, reason: str) -> int:
    """Explicit "yes, I moved this on purpose" acknowledgement. Retired rows
    are skipped by future reconciles and never reported as missing."""
    cur = conn.execute(
        "UPDATE source_files SET presence = 'retired', presence_note = ?, missing_since = NULL "
        "WHERE root_dir = ? OR path = ?",
        (reason, path_or_root, path_or_root),
    )
    conn.commit()
    return cur.rowcount


def status(conn: sqlite3.Connection) -> dict:
    counts = {
        r["presence"]: r["n"]
        for r in conn.execute("SELECT presence, COUNT(*) AS n FROM source_files GROUP BY presence").fetchall()
    }
    last_run = conn.execute("SELECT * FROM reconcile_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"counts": counts, "last_run": dict(last_run) if last_run else None}


def to_dict(report: ReconcileReport) -> dict:
    return {
        "run_at": report.run_at,
        "roots": report.roots,
        "checked": report.checked,
        "still_present": report.still_present,
        "newly_missing": report.newly_missing,
        "reappeared": report.reappeared,
        "missing": report.missing,
        "reappeared_detail": report.reappeared_detail,
    }


def persist(conn: sqlite3.Connection, report: ReconcileReport) -> int:
    cur = conn.execute(
        """
        INSERT INTO reconcile_runs
            (run_at, roots_json, checked, still_present, newly_missing, reappeared, report_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report.run_at,
            json.dumps(report.roots),
            report.checked,
            report.still_present,
            report.newly_missing,
            report.reappeared,
            json.dumps(to_dict(report)),
        ),
    )
    conn.commit()
    return cur.lastrowid

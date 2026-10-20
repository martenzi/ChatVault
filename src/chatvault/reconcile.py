from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chatvault import hashing


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stat_index(root: Path) -> dict[int, list[Path]]:
    """size -> files under root, stat only (no hashing). Empty if root is gone."""
    out: dict[int, list[Path]] = {}
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        try:
            if p.is_file():
                out.setdefault(p.stat().st_size, []).append(p)
        except OSError:
            continue
    return out


@dataclass
class ReconcileReport:
    run_at: str
    roots: list[str] = field(default_factory=list)
    checked: int = 0
    still_present: int = 0
    newly_missing: int = 0
    reappeared: int = 0
    moved: int = 0
    changed: int = 0
    move_scan_seconds: float = 0.0
    move_scan: bool = True
    missing: list[dict] = field(default_factory=list)
    moved_detail: list[dict] = field(default_factory=list)
    changed_detail: list[dict] = field(default_factory=list)
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


def _composite(row) -> bool:
    """Artifact-bundle rows store a hash over a whole directory, not of the
    file at `path`, so file-level hash checks do not apply to them."""
    return str(row["adapter"]).endswith("_artifacts")


def _tracked_elsewhere(conn: sqlite3.Connection, sha: str, own_id: int) -> bool:
    """A grown file (new version ingested under another row) is not a 'change'."""
    return conn.execute(
        "SELECT 1 FROM source_files WHERE sha256 = ? AND id != ? LIMIT 1", (sha, own_id)
    ).fetchone() is not None


def reconcile(
    conn: sqlite3.Connection,
    roots: list[str] | None = None,
    kind: str = "live",
    dry_run: bool = False,
    move_scan: bool = True,
    search_dirs: list[Path] | None = None,
) -> ReconcileReport:
    """Diff DB-tracked source_files against what's actually on disk. Default
    scope is kind='live' — frozen sources are archived on purpose and should
    never be flagged as missing. Never deletes a row or touches ingested
    content; this only updates presence metadata and, for a file found
    elsewhere by sha256, its path.

    A tracked path that no longer exists is looked up by content: same bytes
    elsewhere under its root or under search_dirs -> 'moved' (path updated,
    presence stays 'present'); no copy anywhere -> 'missing'. A path that
    exists with different bytes is 'changed' (reported, left present) — only
    checked for non-live kinds, since live files legitimately grow."""
    import time

    root_dirs = _scoped_root_dirs(conn, roots, kind)
    now = _now()
    report = ReconcileReport(run_at=now, roots=root_dirs, move_scan=move_scan)
    extra = [Path(d) for d in (search_dirs or [])]
    sha_cache: dict[Path, str] = {}

    def _sha(p: Path) -> str:
        if p not in sha_cache:
            sha_cache[p] = hashing.file_sha256(p)
        return sha_cache[p]

    for root_dir in root_dirs:
        q = ("SELECT id, path, sha256, bytes, kind, adapter, presence FROM source_files "
             "WHERE root_dir = ? AND presence != 'retired'")
        qparams: list[str] = [root_dir]
        if kind != "all":
            q += " AND kind = ?"
            qparams.append(kind)
        rows = conn.execute(q, qparams).fetchall()

        gone = [r for r in rows if not Path(r["path"]).is_file()]
        indexes: list[tuple[Path, dict[int, list[Path]]]] = []
        if gone and move_scan:
            t0 = time.monotonic()
            for d in [Path(root_dir), *extra]:
                if all(d != seen for seen, _ in indexes):
                    indexes.append((d, _stat_index(d)))
            report.move_scan_seconds += time.monotonic() - t0

        for row in rows:
            report.checked += 1
            path = Path(row["path"])
            if path.is_file():
                if row["kind"] != "live" and row["bytes"] is not None and not _composite(row):
                    same = path.stat().st_size == row["bytes"] and _sha(path) == row["sha256"]
                    if not same and not _tracked_elsewhere(conn, _sha(path), row["id"]):
                        report.changed += 1
                        report.changed_detail.append({"id": row["id"], "path": row["path"]})
                _mark_present(conn, report, row, now, dry_run)
                continue

            new_path = None
            found_in = None
            if move_scan:
                t0 = time.monotonic()
                for d, idx in indexes:
                    for cand in idx.get(row["bytes"], []) if row["bytes"] is not None else []:
                        if _composite(row):
                            ok = cand.parts[-3:] == path.parts[-3:]
                        else:
                            ok = _sha(cand) == row["sha256"]
                        if ok:
                            new_path, found_in = cand, d
                            break
                    if new_path:
                        break
                report.move_scan_seconds += time.monotonic() - t0

            if new_path is not None:
                report.moved += 1
                report.moved_detail.append({"id": row["id"], "old": row["path"], "new": str(new_path)})
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE source_files
                        SET path = ?, root_dir = ?, presence = 'present', last_seen_at = ?,
                            missing_since = NULL, presence_note = ?,
                            first_seen_at = COALESCE(first_seen_at, ingested_at)
                        WHERE id = ?
                        """,
                        (str(new_path), str(found_in), now, f"moved from {row['path']} ({now[:10]})", row["id"]),
                    )
                report.still_present += 1
                continue

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


def _mark_present(conn, report, row, now, dry_run) -> None:
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
        "moved": report.moved,
        "changed": report.changed,
        "move_scan": report.move_scan,
        "move_scan_seconds": round(report.move_scan_seconds, 2),
        "moved_detail": report.moved_detail,
        "changed_detail": report.changed_detail,
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

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from chatvault.config import Config

IGNORE_EXACT = {".DS_Store", "_manifest.jsonl"}
IGNORE_SUFFIXES = {".zip"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ignored(path: Path) -> bool:
    return path.name in IGNORE_EXACT or path.suffix.lower() in IGNORE_SUFFIXES


def walk_raw(directory: Path, ignore_dir: Path | None = None) -> list[Path]:
    """Every non-ignored file under directory. ignore_dir (typically
    raw_exclusions/) is skipped even if it happens to be a descendant of
    directory — the normal layout has it as a sibling of raw/, so this only
    matters for the --raw-dir=$CHATVAULT_ROOT defensive case."""
    ignore_resolved = ignore_dir.resolve() if ignore_dir else None
    out = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or _ignored(path):
            continue
        if ignore_resolved is not None:
            try:
                path.resolve().relative_to(ignore_resolved)
                continue
            except ValueError:
                pass
        out.append(path)
    return out


def unclaimed(conn: sqlite3.Connection, directory: Path, ignore_dir: Path | None = None) -> list[Path]:
    from chatvault import hashing

    known = {row[0] for row in conn.execute("SELECT sha256 FROM source_files").fetchall()}
    return [p for p in walk_raw(directory, ignore_dir) if hashing.file_sha256(p) not in known]


def guard_raw_exclusions(config: Config, path: Path, force: bool) -> None:
    exclusions_dir = (config.root / "raw_exclusions").resolve()
    target = path.resolve()
    is_exclusions_itself = target == exclusions_dir
    is_inside_exclusions = False
    if not is_exclusions_itself:
        try:
            target.relative_to(exclusions_dir)
            is_inside_exclusions = True
        except ValueError:
            pass
    if (is_exclusions_itself or is_inside_exclusions) and not force:
        raise ValueError(
            f"{path} is inside raw_exclusions/ — these files were seen and deliberately "
            "not ingested (see raw_exclusions/_why.md). Use --force to override."
        )


def mark_kind(conn: sqlite3.Connection, path_or_root: str, kind: str) -> int:
    if kind not in ("live", "frozen"):
        raise ValueError(f"kind must be 'live' or 'frozen', got {kind!r}")
    cur = conn.execute(
        "UPDATE source_files SET kind = ? WHERE root_dir = ? OR path = ?",
        (kind, path_or_root, path_or_root),
    )
    conn.commit()
    return cur.rowcount


def list_sources(conn: sqlite3.Connection, kind: str | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT root_dir, kind, COUNT(*) AS files, MAX(last_seen_at) AS last_seen_at
        FROM source_files
        {where}
        GROUP BY root_dir, kind
        ORDER BY root_dir
    """.format(where="WHERE kind = ?" if kind else "")
    params = (kind,) if kind else ()
    return conn.execute(sql, params).fetchall()  # noqa: S608


_WHY_ROW_RE = re.compile(r"^\|\s*(?P<path>[^|]+?)\s*\|\s*(?P<reason>[^|]+?)\s*\|\s*$")


def parse_why_md(path: Path) -> list[dict]:
    """Parses a two-column markdown table: | Path | Reason |. Tolerant of a
    header/separator row (skipped by ignoring anything that doesn't have at
    least one non-dash, non-space, non-pipe character in both columns)."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        m = _WHY_ROW_RE.match(line.strip())
        if not m:
            continue
        p, reason = m.group("path"), m.group("reason")
        if set(p) <= set("-: ") or set(reason) <= set("-: "):
            continue  # separator row (|---|---|) or header
        if p.lower() == "path" and reason.lower() == "reason":
            continue  # header row
        entries.append({"path": p, "reason": reason})
    return entries


def undocumented_subdirs(raw_exclusions_dir: Path, why_entries: list[dict]) -> list[Path]:
    if not raw_exclusions_dir.is_dir():
        return []
    documented = {e["path"].rstrip("/") for e in why_entries}
    out = []
    for child in sorted(raw_exclusions_dir.iterdir()):
        if not child.is_dir():
            continue
        rel = child.name
        if rel not in documented and f"raw_exclusions/{rel}" not in documented:
            out.append(child)
    return out


@dataclass
class ManifestSection:
    title: str
    rows: list[dict]


def export_manifest(conn: sqlite3.Connection, config: Config, out: Path | None = None) -> Path:
    out = out or (config.root / "MANIFEST.md")

    total_convs = conn.execute("SELECT COUNT(*) FROM conversations WHERE superseded_by IS NULL AND excluded = 0").fetchone()[0]
    total_msgs = conn.execute("SELECT COUNT(*) FROM v_messages").fetchone()[0]
    n_sources = conn.execute("SELECT COUNT(DISTINCT root_dir) FROM source_files WHERE root_dir IS NOT NULL").fetchone()[0]

    lines = [
        "# ChatVault Source Manifest",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{n_sources} source(s) · {total_convs} conversations · {total_msgs} messages",
        "",
        "## Frozen (ingest once, archive)",
        "| Source | Files | Conversations | Date range | SHA (short) | Ingested |",
        "|---|---|---|---|---|---|",
    ]
    frozen = conn.execute(
        """
        SELECT sf.root_dir AS source, COUNT(DISTINCT sf.id) AS files,
               COUNT(DISTINCT c.id) AS conversations,
               MIN(c.started_at) AS date_start, MAX(c.started_at) AS date_end,
               substr(MIN(sf.sha256), 1, 8) AS sha_short,
               MAX(sf.ingested_at) AS ingested
        FROM source_files sf
        LEFT JOIN conversations c ON c.source_file_id = sf.id AND c.superseded_by IS NULL AND c.excluded = 0
        WHERE sf.kind = 'frozen'
        GROUP BY sf.root_dir
        ORDER BY sf.root_dir
        """
    ).fetchall()
    for r in frozen:
        date_range = f"{r['date_start'] or '?'} → {r['date_end'] or '?'}"
        lines.append(
            f"| {r['source']} | {r['files']} | {r['conversations']} | {date_range} | {r['sha_short']} | {r['ingested']} |"
        )

    lines += [
        "",
        "## Live (re-ingest periodically)",
        "| Source | Files | Conversations | Last ingest | Days since |",
        "|---|---|---|---|---|",
    ]
    live = conn.execute(
        """
        SELECT sf.root_dir AS source, COUNT(DISTINCT sf.id) AS files,
               COUNT(DISTINCT c.id) AS conversations,
               MAX(sf.last_seen_at) AS last_seen,
               CAST(julianday('now') - julianday(MAX(sf.last_seen_at)) AS INTEGER) AS days_since
        FROM source_files sf
        LEFT JOIN conversations c ON c.source_file_id = sf.id AND c.superseded_by IS NULL AND c.excluded = 0
        WHERE sf.kind = 'live'
        GROUP BY sf.root_dir
        ORDER BY sf.root_dir
        """
    ).fetchall()
    for r in live:
        lines.append(f"| {r['source']} | {r['files']} | {r['conversations']} | {r['last_seen'] or '?'} | {r['days_since'] if r['days_since'] is not None else '?'} |")

    lines += [
        "",
        "## Rejected — seen and deliberately not ingested",
        "| Path | Reason |",
        "|---|---|",
    ]
    why_path = config.root / "raw_exclusions" / "_why.md"
    why_entries = parse_why_md(why_path)
    if not why_entries and not why_path.exists():
        lines.append("<!-- generate entries by editing raw_exclusions/_why.md -->")
    else:
        for e in why_entries:
            lines.append(f"| {e['path']} | {e['reason']} |")

    for subdir in undocumented_subdirs(config.root / "raw_exclusions", why_entries):
        lines.append(f"| {subdir.name} | ⚠ undocumented |")

    out.write_text("\n".join(lines) + "\n")
    return out

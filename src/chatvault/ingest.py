from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chatvault import adapters, hashing, loader
from chatvault.config import Config

KNOWN_EXTENSIONS = {".json", ".jsonl"}

# Some sources write archived/backup copies with a compound extension —
# OpenClaw's pre-reset session backups are named "<uuid>.jsonl.reset" or
# "<uuid>.jsonl.reset.<ISO8601Z timestamp>". Path.suffix on those returns
# ".reset"/".<fragment>Z", not ".jsonl", so the strict KNOWN_EXTENSIONS check
# above would silently drop real session content. Recognise them by glob-ish
# pattern on the filename instead of requiring the source folder to rename
# anything (which raw/ append-only + "never modify source" both rule out).
_KNOWN_NAME_RE = re.compile(r"\.(json|jsonl)(\.|$)", re.IGNORECASE)


def _is_known_candidate_name(name: str) -> bool:
    return bool(_KNOWN_NAME_RE.search(name))


@dataclass
class FileIngestResult:
    path: Path
    status: str  # ok | skipped_dup | skipped_no_adapter | failed
    adapter: str | None = None
    inserted: int = 0
    noop: int = 0
    superseded: int = 0
    flagged: int = 0
    error: str | None = None


@dataclass
class IngestSummary:
    files: list[FileIngestResult] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        totals = {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
        for f in self.files:
            totals["inserted"] += f.inserted
            totals["noop"] += f.noop
            totals["superseded"] += f.superseded
            totals["flagged"] += f.flagged
        return totals


def _iter_candidate_files(path: Path, exclude_dir: Path | None = None) -> list[Path]:
    exclude_resolved = exclude_dir.resolve() if exclude_dir else None

    def _is_excluded(p: Path) -> bool:
        if exclude_resolved is None:
            return False
        try:
            p.resolve().relative_to(exclude_resolved)
            return True
        except ValueError:
            return False

    if path.is_file():
        return [] if _is_excluded(path) else [path]
    return sorted(
        p
        for p in path.rglob("*")
        if p.is_file() and _is_known_candidate_name(p.name) and not _is_excluded(p)
    )


def ingest_path(
    conn: sqlite3.Connection,
    path: Path,
    adapter_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    config: Config | None = None,
) -> IngestSummary:
    exclude_dir = None
    if config is not None:
        from chatvault import sources

        sources.guard_raw_exclusions(config, path, force)
        candidate_exclude_dir = config.root / "raw_exclusions"
        # Only filter raw_exclusions/ out of a broader walk (e.g. path is an
        # ancestor like CHATVAULT_ROOT itself). If path targets raw_exclusions/
        # directly, the guard above already required --force to get this far,
        # so honor that and don't then silently filter every file back out.
        try:
            path.resolve().relative_to(candidate_exclude_dir.resolve())
            targets_exclusions_dir = True
        except ValueError:
            targets_exclusions_dir = path.resolve() == candidate_exclude_dir.resolve()
        if not targets_exclusions_dir:
            exclude_dir = candidate_exclude_dir

    root_dir = str((path if path.is_dir() else path.parent).resolve())
    summary = IngestSummary()

    for file_path in _iter_candidate_files(path, exclude_dir):
        mod = adapters.get(adapter_name) if adapter_name else adapters.detect(file_path)
        if mod is None:
            summary.files.append(FileIngestResult(file_path, "skipped_no_adapter"))
            continue

        sha = hashing.file_sha256(file_path)
        existing = conn.execute(
            "SELECT id, status FROM source_files WHERE sha256 = ?", (sha,)
        ).fetchone()
        if existing is not None and existing["status"] == "ok":
            if not dry_run:
                conn.execute(
                    "UPDATE source_files SET last_seen_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), existing["id"]),
                )
                conn.commit()
            summary.files.append(FileIngestResult(file_path, "skipped_dup", adapter=mod.name))
            continue

        if dry_run:
            try:
                convs = list(mod.parse(file_path))
                result = FileIngestResult(file_path, "ok", adapter=mod.name, inserted=len(convs))
            except Exception as exc:  # noqa: BLE001
                result = FileIngestResult(file_path, "failed", adapter=mod.name, error=str(exc))
            summary.files.append(result)
            continue

        now = datetime.now(timezone.utc).isoformat()
        try:
            convs = list(mod.parse(file_path))
        except Exception as exc:  # noqa: BLE001
            conn.execute(
                """
                INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, error, kind, last_seen_at, root_dir, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, 'frozen', ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    status='failed', error=excluded.error, ingested_at=excluded.ingested_at,
                    last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
                    first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
                """,
                (str(file_path), sha, mod.platform, mod.name, file_path.stat().st_size, now, str(exc), now, root_dir, now),
            )
            conn.commit()
            summary.files.append(FileIngestResult(file_path, "failed", adapter=mod.name, error=str(exc)))
            continue

        cur = conn.execute(
            """
            INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, kind, last_seen_at, root_dir, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, 'ok', 'frozen', ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                status='ok', error=NULL, ingested_at=excluded.ingested_at,
                last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
                first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
            """,
            (str(file_path), sha, mod.platform, mod.name, file_path.stat().st_size, now, now, root_dir, now),
        )
        source_file_id = cur.lastrowid or conn.execute(
            "SELECT id FROM source_files WHERE sha256 = ?", (sha,)
        ).fetchone()["id"]

        result = FileIngestResult(file_path, "ok", adapter=mod.name)
        for cir in convs:
            load_result = loader.load_conversation(conn, cir, source_file_id)
            if load_result.action == "inserted":
                result.inserted += 1
            elif load_result.action == "noop":
                result.noop += 1
            elif load_result.action == "superseded":
                result.superseded += 1
            elif load_result.action == "flagged_duplicate":
                result.flagged += 1
        conn.commit()
        summary.files.append(result)

    if not dry_run:
        from chatvault import dedupe

        dedupe.scan_candidates(conn)

        # Self-maintaining presence tracking: if this root has ever been
        # marked 'live', a fresh ingest is also the natural moment to notice
        # that some previously-tracked file has disappeared (or reappeared).
        is_live = conn.execute(
            "SELECT 1 FROM source_files WHERE root_dir = ? AND kind = 'live' LIMIT 1", (root_dir,)
        ).fetchone()
        if is_live:
            from chatvault import reconcile as reconcile_mod

            reconcile_mod.reconcile(conn, roots=[root_dir], kind="live")

    return summary

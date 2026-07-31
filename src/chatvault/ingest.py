from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chatvault import adapters, hashing, loader

KNOWN_EXTENSIONS = {".json", ".jsonl"}


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


def _iter_candidate_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in KNOWN_EXTENSIONS)


def ingest_path(
    conn: sqlite3.Connection,
    path: Path,
    adapter_name: str | None = None,
    dry_run: bool = False,
) -> IngestSummary:
    summary = IngestSummary()

    for file_path in _iter_candidate_files(path):
        mod = adapters.get(adapter_name) if adapter_name else adapters.detect(file_path)
        if mod is None:
            summary.files.append(FileIngestResult(file_path, "skipped_no_adapter"))
            continue

        sha = hashing.file_sha256(file_path)
        existing = conn.execute(
            "SELECT id, status FROM source_files WHERE sha256 = ?", (sha,)
        ).fetchone()
        if existing is not None and existing["status"] == "ok":
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
                INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, error)
                VALUES (?, ?, ?, ?, ?, ?, 'failed', ?)
                ON CONFLICT(sha256) DO UPDATE SET status='failed', error=excluded.error, ingested_at=excluded.ingested_at
                """,
                (str(file_path), sha, mod.platform, mod.name, file_path.stat().st_size, now, str(exc)),
            )
            conn.commit()
            summary.files.append(FileIngestResult(file_path, "failed", adapter=mod.name, error=str(exc)))
            continue

        cur = conn.execute(
            """
            INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'ok')
            ON CONFLICT(sha256) DO UPDATE SET status='ok', error=NULL, ingested_at=excluded.ingested_at
            """,
            (str(file_path), sha, mod.platform, mod.name, file_path.stat().st_size, now),
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

    return summary

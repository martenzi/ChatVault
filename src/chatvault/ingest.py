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
    embedding: object = None  # chatvault.embeddings.IndexResult | None

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


def _ingest_projects_file(
    conn: sqlite3.Connection, file_path: Path, mod, root_dir: str, dry_run: bool
) -> FileIngestResult:
    """Projects files (e.g. Claude's projects.json) are structurally distinct
    from conversation exports and go through loader.load_project instead of
    load_conversation. `inserted` = new project rows, `noop` = existing
    project rows refreshed — mirrors the conversation counters' meaning
    closely enough for the shared ingest summary table."""
    adapter_label = f"{mod.name} (projects)"
    projects_adapter = f"{mod.name}_projects"
    now = datetime.now(timezone.utc).isoformat()
    sha = hashing.file_sha256(file_path)

    # Scoped by (sha256, adapter), not sha256 alone: a file that is BOTH a
    # projects export and a conversations export (Grok's real export is)
    # gets one source_files row per purpose, so this dedup check must not
    # see the conversations-half row and mistake it for "already done."
    existing = conn.execute(
        "SELECT id, status FROM source_files WHERE sha256 = ? AND adapter = ?", (sha, projects_adapter)
    ).fetchone()
    if existing is not None and existing["status"] == "ok":
        if not dry_run:
            conn.execute("UPDATE source_files SET last_seen_at = ? WHERE id = ?", (now, existing["id"]))
            conn.commit()
        return FileIngestResult(file_path, "skipped_dup", adapter=adapter_label)

    if dry_run:
        try:
            projects = list(mod.parse_projects(file_path))
            return FileIngestResult(file_path, "ok", adapter=adapter_label, inserted=len(projects))
        except Exception as exc:  # noqa: BLE001
            return FileIngestResult(file_path, "failed", adapter=adapter_label, error=str(exc))

    try:
        projects = list(mod.parse_projects(file_path))
    except Exception as exc:  # noqa: BLE001
        conn.execute(
            """
            INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, error, kind, last_seen_at, root_dir, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, 'frozen', ?, ?, ?)
            ON CONFLICT(sha256, adapter) DO UPDATE SET
                status='failed', error=excluded.error, ingested_at=excluded.ingested_at,
                last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
                first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
            """,
            (str(file_path), sha, mod.platform, projects_adapter, file_path.stat().st_size, now, str(exc), now, root_dir, now),
        )
        conn.commit()
        return FileIngestResult(file_path, "failed", adapter=adapter_label, error=str(exc))

    cur = conn.execute(
        """
        INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, kind, last_seen_at, root_dir, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, 'ok', 'frozen', ?, ?, ?)
        ON CONFLICT(sha256, adapter) DO UPDATE SET
            status='ok', error=NULL, ingested_at=excluded.ingested_at,
            last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
            first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
        """,
        (str(file_path), sha, mod.platform, projects_adapter, file_path.stat().st_size, now, now, root_dir, now),
    )
    source_file_id = cur.lastrowid or conn.execute(
        "SELECT id FROM source_files WHERE sha256 = ? AND adapter = ?", (sha, projects_adapter)
    ).fetchone()["id"]

    result = FileIngestResult(file_path, "ok", adapter=adapter_label)
    for project in projects:
        load_result = loader.load_project(conn, project, source_file_id)
        if load_result["action"] == "inserted":
            result.inserted += 1
        else:
            result.noop += 1
    conn.commit()
    return result


def ingest_path(
    conn: sqlite3.Connection,
    path: Path,
    adapter_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    config: Config | None = None,
    reprocess: bool = False,
    embed: bool = False,
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
    touched_conv_ids: list[int] = []

    for file_path in _iter_candidate_files(path, exclude_dir):
        # A file's project data and its conversation data are independent
        # questions, not an either/or branch. Most sources keep them in
        # separate files (Claude's projects.json vs its conversations
        # export), but Grok's real export carries both "conversations" and
        # "projects" in the SAME file — a file that used to short-circuit
        # here via `continue` the moment detect_projects() matched, silently
        # dropping all 599 of its conversations. Check both independently
        # and ingest whichever are present.
        matched_projects = False
        if adapter_name is None:
            projects_mod = adapters.detect_projects(file_path)
            if projects_mod is not None:
                summary.files.append(
                    _ingest_projects_file(conn, file_path, projects_mod, root_dir, dry_run=dry_run)
                )
                matched_projects = True

        mod = adapters.get(adapter_name) if adapter_name else adapters.detect(file_path)
        if mod is None:
            # Only a "no adapter" result if the file also had no project
            # data — a pure projects file (mod is legitimately None here)
            # already got its result appended above.
            if not matched_projects:
                summary.files.append(FileIngestResult(file_path, "skipped_no_adapter"))
            continue

        sha = hashing.file_sha256(file_path)
        # Scoped by (sha256, adapter): see the matching comment in
        # _ingest_projects_file — a dual-purpose file's projects-half row
        # must not shadow this conversations-half check.
        existing = conn.execute(
            "SELECT id, status FROM source_files WHERE sha256 = ? AND adapter = ?", (sha, mod.name)
        ).fetchone()
        if existing is not None and existing["status"] == "ok" and not reprocess:
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
                ON CONFLICT(sha256, adapter) DO UPDATE SET
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
            ON CONFLICT(sha256, adapter) DO UPDATE SET
                status='ok', error=NULL, ingested_at=excluded.ingested_at,
                last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
                first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
            """,
            (str(file_path), sha, mod.platform, mod.name, file_path.stat().st_size, now, now, root_dir, now),
        )
        source_file_id = cur.lastrowid or conn.execute(
            "SELECT id FROM source_files WHERE sha256 = ? AND adapter = ?", (sha, mod.name)
        ).fetchone()["id"]

        result = FileIngestResult(file_path, "ok", adapter=mod.name)
        for cir in convs:
            load_result = loader.load_conversation(conn, cir, source_file_id)
            if load_result.action == "inserted":
                result.inserted += 1
                touched_conv_ids.append(load_result.conv_id)
            elif load_result.action == "noop":
                result.noop += 1
            elif load_result.action == "superseded":
                result.superseded += 1
                touched_conv_ids.append(load_result.conv_id)
            elif load_result.action == "flagged_duplicate":
                result.flagged += 1
        conn.commit()
        summary.files.append(result)

    if not dry_run:
        from chatvault import dedupe

        dedupe.scan_candidates(conn)

        if touched_conv_ids:
            from chatvault import tags as tags_mod

            tags_mod.apply_rules(conn, conv_ids=touched_conv_ids)
            tags_mod.flag_model_unknown(conn, touched_conv_ids)

            if embed:
                from chatvault import embeddings as embeddings_mod

                # Best-effort: an unreachable Ollama server must not fail an
                # otherwise-successful ingest. index_conversations() already
                # stops cleanly (IndexResult.unavailable=True) rather than
                # raising, so no try/except is needed here — just surface it.
                summary.embedding = embeddings_mod.index_conversations(conn, conv_ids=touched_conv_ids)

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

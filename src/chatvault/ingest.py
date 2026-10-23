from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chatvault import adapters, hashing, ledger, loader, paths
from chatvault.config import Config

KNOWN_EXTENSIONS = {".json", ".jsonl"}


def _kind_for_platform(platform: str) -> str:
    """New source_files rows for a known server-side platform (Claude
    Desktop, ChatGPT, Grok, Gemini, DeepSeek — see ledger.SERVER_SIDE_PLATFORMS
    / migration 015) start life as 'server_side', not 'frozen': these sources
    go stale the moment they're exported, unlike a genuine one-time capture.
    Everything else defaults to 'frozen' as before; `sources mark --kind live`
    remains the explicit opt-in for a locally-growing directory, and re-ingest
    never overwrites a kind a human already set (see the ON CONFLICT clauses
    below, which don't touch kind)."""
    return "server_side" if platform in ledger.SERVER_SIDE_PLATFORMS else "frozen"

# Some sources write archived/backup copies with a compound extension —
# OpenClaw's pre-reset session backups are named "<uuid>.jsonl.reset" or
# "<uuid>.jsonl.reset.<ISO8601Z timestamp>". Path.suffix on those returns
# ".reset"/".<fragment>Z", not ".jsonl", so the strict KNOWN_EXTENSIONS check
# above would silently drop real session content. Recognise them by glob-ish
# pattern on the filename instead of requiring the source folder to rename
# anything (which raw/ append-only + "never modify source" both rule out).
# .html/.txt added 2026-09-21: Google Takeout switched "My Activity" exports
# to HTML, and "Gemini in Workspace/Conversation History" exports JSON inside
# .txt files. Files matching no adapter land as skipped_no_adapter rows only —
# nothing is written, so widening this list is audit-positive, not risky.
_KNOWN_NAME_RE = re.compile(r"\.(json|jsonl|html?|txt)(\.|$)", re.IGNORECASE)


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
    reprocessed: int = 0
    flagged: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestSummary:
    files: list[FileIngestResult] = field(default_factory=list)
    embedding: object = None  # chatvault.embeddings.IndexResult | None
    artifacts: dict | None = None  # loader.link_artifacts result

    @property
    def totals(self) -> dict[str, int]:
        totals = {"inserted": 0, "noop": 0, "superseded": 0, "flagged": 0}
        for f in self.files:
            totals["inserted"] += f.inserted
            totals["noop"] += f.noop
            totals["superseded"] += f.superseded
            totals["flagged"] += f.flagged
        return totals


# Directory name for already-ingested archives under raw/. Walks that start
# above this folder skip it; an explicit `chatvault ingest raw/_ingested/...`
# still processes that tree (relative parts then have no leading `_ingested`).
_INGESTED_ARCHIVE_DIR = "_ingested"
# raw/ lifecycle folders (chatvault.intake): a walk of raw/ must not re-read
# files that were already filed, excluded or archived.
_LIFECYCLE_SKIP_DIRS = {"_ingested", "10_ingested", "20_needs_adapter", "30_excluded", "40_archive"}


def _adapter_parse_warnings(mod) -> list[str]:
    """Harvest an adapter's module-level `parse_stats` dict, if it exposes
    one, into human-readable warning strings for the ingest report.

    Adapters that tolerate per-record degradation (rather than failing the
    file) count what they degraded here — e.g. gemini_takeout counts
    timestamps it could not parse (those messages land created_at=None and
    their conversations get review/timestamp-unparsed) and timezone
    abbreviations it treated as UTC. Loud, never fatal: a warning in the
    report plus a review tag, not a failed file and not a silent wrong
    value."""
    stats = getattr(mod, "parse_stats", None)
    if not stats:
        return []
    warnings: list[str] = []
    unparsed = stats.get("timestamps_unparsed") or 0
    if unparsed:
        warnings.append(
            f"{unparsed} timestamp(s) unparseable — affected messages have created_at=NULL; "
            "conversations tagged review/timestamp-unparsed"
        )
    unknown_tz = stats.get("unknown_timezones") or {}
    if unknown_tz:
        detail = ", ".join(f"{abbr}×{n}" for abbr, n in sorted(unknown_tz.items()))
        warnings.append(f"unknown timezone abbreviation(s) treated as UTC: {detail}")
    return warnings


def _under_ingested_archive(p: Path, walk_root: Path) -> bool:
    """True when p sits under a `_ingested/` component relative to walk_root."""
    try:
        rel = p.resolve().relative_to(walk_root.resolve())
    except ValueError:
        return False
    return bool(_LIFECYCLE_SKIP_DIRS & set(rel.parts))


def _iter_candidate_files(path: Path, exclude_dir: Path | None = None) -> list[Path]:
    exclude_resolved = exclude_dir.resolve() if exclude_dir else None
    walk_root = path if path.is_dir() else path.parent

    def _is_excluded(p: Path) -> bool:
        if exclude_resolved is not None:
            try:
                p.resolve().relative_to(exclude_resolved)
                return True
            except ValueError:
                pass
        if path.is_dir() and _under_ingested_archive(p, walk_root):
            return True
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
            VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?)
            ON CONFLICT(sha256, adapter) DO UPDATE SET
                status='failed', error=excluded.error, ingested_at=excluded.ingested_at,
                last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
                first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
            """,
            (str(file_path), sha, mod.platform, projects_adapter, file_path.stat().st_size, now, str(exc), _kind_for_platform(mod.platform), now, root_dir, now),
        )
        conn.commit()
        return FileIngestResult(file_path, "failed", adapter=adapter_label, error=str(exc))

    cur = conn.execute(
        """
        INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, kind, last_seen_at, root_dir, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)
        ON CONFLICT(sha256, adapter) DO UPDATE SET
            status='ok', error=NULL, ingested_at=excluded.ingested_at,
            last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
            first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
        """,
        (str(file_path), sha, mod.platform, projects_adapter, file_path.stat().st_size, now, _kind_for_platform(mod.platform), now, root_dir, now),
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


def _ingest_artifacts_file(
    conn: sqlite3.Connection, file_path: Path, mod, root_dir: str, dry_run: bool
) -> FileIngestResult:
    """`artifacts/<id>/artifact.json` -> attachments via loader.load_artifact.
    `inserted` counts newly created attachment rows, `noop` ones that already
    existed. Same source_files bookkeeping as projects, adapter `<name>_artifacts`."""
    adapter_label = f"{mod.name} (artifacts)"
    artifacts_adapter = f"{mod.name}_artifacts"
    now = datetime.now(timezone.utc).isoformat()
    # Hash the artifact.json together with its version files' bytes: the file
    # alone does not change when a version file is edited.
    import hashlib

    h = hashlib.sha256()
    for p in sorted(x for x in file_path.parent.rglob("*") if x.is_file() and x.name != ".DS_Store"):
        h.update(str(p.relative_to(file_path.parent)).encode())
        h.update(hashing.file_sha256(p).encode())
    sha = h.hexdigest()

    existing = conn.execute(
        "SELECT id, status FROM source_files WHERE sha256 = ? AND adapter = ?", (sha, artifacts_adapter)
    ).fetchone()
    if existing is not None and existing["status"] == "ok":
        if not dry_run:
            conn.execute("UPDATE source_files SET last_seen_at = ? WHERE id = ?", (now, existing["id"]))
            conn.commit()
        return FileIngestResult(file_path, "skipped_dup", adapter=adapter_label)
    try:
        artifacts = list(mod.parse_artifacts(file_path))
    except Exception as exc:  # noqa: BLE001
        return FileIngestResult(file_path, "failed", adapter=adapter_label, error=str(exc))
    if dry_run:
        return FileIngestResult(file_path, "ok", adapter=adapter_label, inserted=sum(len(a["attachments"]) for a in artifacts))

    conn.execute(
        """
        INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, kind, last_seen_at, root_dir, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)
        ON CONFLICT(sha256, adapter) DO UPDATE SET
            status='ok', error=NULL, ingested_at=excluded.ingested_at,
            last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
            first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
        """,
        (str(file_path), sha, mod.platform, artifacts_adapter, file_path.stat().st_size, now, _kind_for_platform(mod.platform), now, root_dir, now),
    )
    source_file_id = conn.execute(
        "SELECT id FROM source_files WHERE sha256 = ? AND adapter = ?", (sha, artifacts_adapter)
    ).fetchone()["id"]
    result = FileIngestResult(file_path, "ok", adapter=adapter_label)
    for art in artifacts:
        r = loader.load_artifact(conn, art, source_file_id)
        result.inserted += r["created"]
        result.noop += r["attachments"] - r["created"]
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

    path = Path(paths.canonical(path))
    root_dir = str(path if path.is_dir() else path.parent)
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
            artifacts_mod = adapters.detect_artifacts(file_path)
            if artifacts_mod is not None:
                summary.files.append(
                    _ingest_artifacts_file(conn, file_path, artifacts_mod, root_dir, dry_run=dry_run)
                )
                continue
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
                result = FileIngestResult(
                    file_path, "ok", adapter=mod.name, inserted=len(convs),
                    warnings=_adapter_parse_warnings(mod),
                )
            except Exception as exc:  # noqa: BLE001
                result = FileIngestResult(file_path, "failed", adapter=mod.name, error=str(exc))
            summary.files.append(result)
            continue

        now = datetime.now(timezone.utc).isoformat()
        parse_warnings: list[str] = []
        try:
            convs = list(mod.parse(file_path))
            parse_warnings = _adapter_parse_warnings(mod)
        except Exception as exc:  # noqa: BLE001
            conn.execute(
                """
                INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, error, kind, last_seen_at, root_dir, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?)
                ON CONFLICT(sha256, adapter) DO UPDATE SET
                    status='failed', error=excluded.error, ingested_at=excluded.ingested_at,
                    last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
                    first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
                """,
                (str(file_path), sha, mod.platform, mod.name, file_path.stat().st_size, now, str(exc), _kind_for_platform(mod.platform), now, root_dir, now),
            )
            conn.commit()
            summary.files.append(FileIngestResult(file_path, "failed", adapter=mod.name, error=str(exc)))
            continue

        conn.execute(
            """
            INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, kind, last_seen_at, root_dir, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)
            ON CONFLICT(sha256, adapter) DO UPDATE SET
                status='ok', error=NULL, ingested_at=excluded.ingested_at,
                last_seen_at=excluded.last_seen_at, root_dir=excluded.root_dir,
                first_seen_at=COALESCE(source_files.first_seen_at, excluded.first_seen_at)
            """,
            (str(file_path), sha, mod.platform, mod.name, file_path.stat().st_size, now, _kind_for_platform(mod.platform), now, root_dir, now),
        )
        source_file_id = conn.execute(
            "SELECT id FROM source_files WHERE sha256 = ? AND adapter = ?", (sha, mod.name)
        ).fetchone()["id"]

        result = FileIngestResult(file_path, "ok", adapter=mod.name, warnings=parse_warnings)
        original_rows = conn.execute(
            "SELECT native_id FROM conversations WHERE source_file_id = ? ORDER BY id", (source_file_id,)
        ).fetchall()
        original_count = len(original_rows)
        original_native_ids = {row["native_id"] for row in original_rows if row["native_id"] is not None}
        for position, cir in enumerate(convs):
            load_result = (
                loader.reprocess_conversation(conn, cir, source_file_id, position)
                if reprocess and existing is not None
                else loader.load_conversation(conn, cir, source_file_id)
            )
            if load_result.action == "inserted":
                result.inserted += 1
                touched_conv_ids.append(load_result.conv_id)
            elif load_result.action == "noop":
                result.noop += 1
            elif load_result.action == "superseded":
                result.superseded += 1
                touched_conv_ids.append(load_result.conv_id)
            elif load_result.action == "snapshot":
                result.superseded += 1
            elif load_result.action == "reprocessed":
                result.reprocessed += 1
                touched_conv_ids.append(load_result.conv_id)
            elif load_result.action == "flagged_duplicate":
                result.flagged += 1
        parsed_native_ids = {cir.get("native_id") for cir in convs if cir.get("native_id") is not None}
        missing_native_ids = original_native_ids - parsed_native_ids
        if reprocess and existing is not None and (len(convs) < original_count or missing_native_ids):
            raise ValueError(
                f"reprocess dropped existing conversations from source file {source_file_id}: "
                f"count {original_count} -> {len(convs)}, missing native ids {sorted(missing_native_ids)}"
            )
        conn.commit()
        summary.files.append(result)

    if not dry_run:
        from chatvault import dedupe

        dedupe.scan_candidates(conn)
        # Artifacts and the conversations that reference them may arrive in
        # either order (or different runs); linking reads stored rows only.
        summary.artifacts = loader.link_artifacts(conn)
        conn.commit()

        if touched_conv_ids:
            from chatvault import dates as dates_mod
            from chatvault import tags as tags_mod

            tags_mod.apply_rules(conn, conv_ids=touched_conv_ids)
            tags_mod.flag_model_unknown(conn, touched_conv_ids)
            tags_mod.flag_shape_unclear(conn, touched_conv_ids)
            tags_mod.flag_timestamp_unparsed(conn, touched_conv_ids)
            dates_mod.flag(conn, touched_conv_ids)

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

        # Checkpoint WAL so large ingest transaction logs do not accumulate on disk
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass

    return summary

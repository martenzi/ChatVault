"""Terminal table presentation helpers — keep Rich tables scannable.

Full values stay in the DB / ingest log; the terminal is for glance review.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from chatvault.ingest import FileIngestResult, IngestSummary

TITLE_MAX = 72
PROJECT_MAX = 28
INGEST_DETAIL_CAP = 40

# Detail rows always shown (actionable or newly processed). skipped_dup is
# collapsed to a count unless verbose — re-ingesting a full raw/ tree can
# produce thousands of them.
_ALWAYS_SHOW_STATUSES = frozenset({"ok", "failed", "skipped_no_adapter"})
_STATUS_SORT = {
    "failed": 0,
    "skipped_no_adapter": 1,
    "ok": 2,
    "skipped_dup": 3,
}


def truncate_display(text: str | None, max_chars: int = TITLE_MAX) -> str:
    """Single-line ellipsis for table cells. Collapses newlines so a Gemini
    canvas-event title (full HTML, 100k+ chars) cannot blow the terminal."""
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    if max_chars <= 1:
        return "…"
    return flat[: max_chars - 1] + "…"


def relative_display_path(path: Path, root: Path | None = None) -> str:
    """Prefer a path relative to the ingest root so the filename is visible."""
    if root is None:
        return str(path)
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def status_counts(files: list[FileIngestResult]) -> dict[str, int]:
    return dict(Counter(f.status for f in files))


def select_ingest_detail_rows(
    files: list[FileIngestResult],
    *,
    verbose: bool = False,
    cap: int = INGEST_DETAIL_CAP,
) -> tuple[list[FileIngestResult], int]:
    """Return (rows for the terminal table, number of rows omitted).

    Default: show ok / failed / skipped_no_adapter, hide skipped_dup.
    Verbose: show every status. Cap applies either way.
    """
    if verbose:
        candidates = list(files)
    else:
        candidates = [f for f in files if f.status in _ALWAYS_SHOW_STATUSES]
    candidates.sort(key=lambda f: (_STATUS_SORT.get(f.status, 9), str(f.path)))
    if len(candidates) <= cap:
        return candidates, 0
    return candidates[:cap], len(candidates) - cap


def write_ingest_log(
    summary: IngestSummary,
    log_path: Path,
    *,
    ingest_path: Path,
    dry_run: bool = False,
    root: Path | None = None,
) -> Path:
    """Write the full per-file ingest report. Returns the path written."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    counts = status_counts(summary.files)
    totals = summary.totals
    reprocessed = sum(f.reprocessed for f in summary.files)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"chatvault ingest{' (dry-run)' if dry_run else ''}",
        f"started: {now}",
        f"path: {ingest_path}",
        f"files: {len(summary.files)}",
        f"status counts: {counts}",
        (
            f"totals: inserted={totals['inserted']} noop={totals['noop']} "
            f"superseded={totals['superseded']} reprocessed={reprocessed} "
            f"flagged={totals['flagged']}"
        ),
        "",
        "\t".join(
            [
                "status",
                "adapter",
                "inserted",
                "noop",
                "superseded",
                "reprocessed",
                "flagged",
                "path",
                "error",
                "warnings",
            ]
        ),
    ]
    for f in sorted(summary.files, key=lambda r: (_STATUS_SORT.get(r.status, 9), str(r.path))):
        lines.append(
            "\t".join(
                [
                    f.status,
                    f.adapter or "-",
                    str(f.inserted),
                    str(f.noop),
                    str(f.superseded),
                    str(f.reprocessed),
                    str(f.flagged),
                    relative_display_path(f.path, root or ingest_path),
                    (f.error or "").replace("\t", " ").replace("\n", " "),
                    ("; ".join(f.warnings)).replace("\t", " ").replace("\n", " "),
                ]
            )
        )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def ingest_log_path(logs_dir: Path, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return logs_dir / f"ingest-{stamp}.txt"

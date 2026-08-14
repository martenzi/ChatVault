from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Sources that live on a vendor's server and go stale the moment they're
# exported — see migration 015 / docs/05_INGESTION_LEDGER.md §5. Kept here
# (not just in the migration) so callers that haven't re-ingested a newly
# supported platform yet (e.g. gemini/deepseek before their first ingest)
# still see it listed correctly by --stale once source_files rows exist.
SERVER_SIDE_PLATFORMS = {"claude_desktop", "chatgpt", "grok", "gemini", "deepseek"}

_SECTION_1_RE = re.compile(
    r"## 1\. What's ingested\n.*?(?=\n## 2\.)", re.DOTALL
)


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT sf.platform,
               sf.kind,
               MIN(COALESCE(sf.first_seen_at, sf.ingested_at)) AS first_seen,
               MAX(COALESCE(sf.last_seen_at, sf.ingested_at))  AS last_seen,
               COUNT(DISTINCT sf.id)  AS files,
               COUNT(DISTINCT vc.id)  AS convs,
               GROUP_CONCAT(DISTINCT sf.root_dir) AS sources_raw
        FROM source_files sf
        LEFT JOIN v_conversations vc ON vc.source_file_id = sf.id
        WHERE sf.status = 'ok'
        GROUP BY sf.platform, sf.kind
        ORDER BY sf.platform, sf.kind
        """
    ).fetchall()


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per (platform, kind) with file/conversation counts and the
    date range files of that shape were ingested. Convs is scoped through
    v_conversations (excluded/superseded rows never counted) — the same
    scoping bug class already fixed once in projects.backfill() and
    tags.tag_tree(), avoided here from the start."""
    return _rows(conn)


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "?"
    return iso[:10]


def _fmt_ingested(first: str | None, last: str | None) -> str:
    f, l = _fmt_date(first), _fmt_date(last)
    return f if f == l else f"{f} → {l}"


def render_table_rows(rows_: list[sqlite3.Row]) -> list[tuple[str, str, str, str, str, str]]:
    """Formats raw rows into the display tuples shared by the CLI table and
    the markdown table, so the two can never drift in what they show."""
    out = []
    for r in rows_:
        out.append(
            (
                _fmt_ingested(r["first_seen"], r["last_seen"]),
                r["platform"],
                r["kind"],
                str(r["files"]),
                str(r["convs"]),
                (r["sources_raw"] or "").replace(",", "; ") or "-",
            )
        )
    return out


def render_markdown_table(rows_: list[sqlite3.Row]) -> str:
    formatted = render_table_rows(rows_)
    lines = [
        "| Ingested | Platform | Kind | Files | Convs | Source |",
        "|---|---|---|---|---|---|",
    ]
    for ingested, platform, kind, files, convs, source in formatted:
        lines.append(f"| {ingested} | `{platform}` | {kind} | {files} | {convs} | {source} |")
    return "\n".join(lines)


def _totals(rows_: list[sqlite3.Row]) -> tuple[int, int]:
    total_convs = sum(r["convs"] for r in rows_)
    platforms = {r["platform"] for r in rows_}
    return total_convs, len(platforms)


def render_markdown_section(conn: sqlite3.Connection) -> str:
    rows_ = _rows(conn)
    table = render_markdown_table(rows_)
    total_convs, n_platforms = _totals(rows_)
    today = datetime.now(timezone.utc).date().isoformat()
    return (
        f"## 1. What's ingested\n\n"
        f"_Regenerated {today} by `chatvault ledger --markdown` — see §5. Edit source_files, not this table._\n\n"
        f"{table}\n\n"
        f"**Total: {total_convs:,} live conversations across {n_platforms} platform(s).**\n"
    )


def regenerate_markdown(conn: sqlite3.Connection, doc_path: str | Path) -> str:
    """Replaces the '## 1. What's ingested' section of doc_path in place,
    leaving every other section (including the §5 spec this command
    implements) untouched. Raises ValueError if the doc doesn't have the
    expected '## 1.' / '## 2.' section markers to anchor the replacement —
    refuses to guess at a different structure rather than corrupting the
    file."""
    doc_path = Path(doc_path)
    text = doc_path.read_text()
    if not _SECTION_1_RE.search(text):
        raise ValueError(
            f"{doc_path}: couldn't find a '## 1. What's ingested' ... '## 2.' section to replace"
        )
    new_section = render_markdown_section(conn).rstrip("\n") + "\n\n---\n"
    new_text = _SECTION_1_RE.sub(lambda _m: new_section, text, count=1)
    doc_path.write_text(new_text)
    return new_text


@dataclass
class StaleSource:
    platform: str
    kind: str
    last_seen: str | None
    days_stale: int | None


def stale(conn: sqlite3.Connection, days: int = 30) -> list[StaleSource]:
    """server_side sources (see SERVER_SIDE_PLATFORMS / migration 015) whose
    most recent ingest is older than `days`. A platform with kind rows but
    no last_seen_at at all (shouldn't happen post-ingest, but don't crash)
    is reported with days_stale=None rather than silently skipped."""
    now = datetime.now(timezone.utc)
    out: list[StaleSource] = []
    for r in _rows(conn):
        if r["kind"] != "server_side":
            continue
        last_seen = r["last_seen"]
        if not last_seen:
            out.append(StaleSource(r["platform"], r["kind"], None, None))
            continue
        try:
            last_dt = datetime.fromisoformat(last_seen)
        except ValueError:
            out.append(StaleSource(r["platform"], r["kind"], last_seen, None))
            continue
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age_days = (now - last_dt).days
        if age_days >= days:
            out.append(StaleSource(r["platform"], r["kind"], last_seen, age_days))
    return sorted(out, key=lambda s: (s.days_stale is None, -(s.days_stale or 0)))

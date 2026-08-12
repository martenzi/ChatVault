from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chatvault import sources
from chatvault.config import Config

_STATUS_RANK = {"pass": 0, "warn": 1, "fail": 2}

# Platforms whose export genuinely never carries a model field — a fact
# already recorded in messages.model (see v_model_coverage), not a defect
# to chase. Excluded from the "assistant messages with model" pass/warn/fail
# threshold so they don't register as a failure every run; still counted
# and shown elsewhere (v_model_coverage, per-platform audit runs).
KNOWN_NULL_MODEL_PLATFORMS = {"gemini", "claude_desktop"}


@dataclass
class CheckResult:
    group: str  # A..F
    name: str
    status: str  # pass | warn | fail
    detail: str
    data: dict | None = None


@dataclass
class AuditReport:
    results: list[CheckResult] = field(default_factory=list)
    sample: list[dict] | None = None
    run_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pct(numerator: int, denominator: int) -> float:
    return (100.0 * numerator / denominator) if denominator else 0.0


# ─────────────── A. ingest completeness ───────────────


def _check_ingest_completeness(conn: sqlite3.Connection, config: Config, raw_dir: Path) -> list[CheckResult]:
    results = []

    failed_n = conn.execute("SELECT COUNT(*) FROM source_files WHERE status = 'failed'").fetchone()[0]
    results.append(
        CheckResult(
            "A", "source_files all ok",
            "pass" if failed_n == 0 else "fail",
            f"{failed_n} file(s) with status='failed'",
        )
    )

    unclaimed_paths = sources.unclaimed(conn, raw_dir, ignore_dir=config.root / "raw_exclusions")
    results.append(
        CheckResult(
            "A", "no unclaimed files",
            "pass" if not unclaimed_paths else "fail",
            f"{len(unclaimed_paths)} file(s) on disk under {raw_dir} not in source_files"
            + (f": {', '.join(str(p) for p in unclaimed_paths[:5])}" + (" ..." if len(unclaimed_paths) > 5 else "") if unclaimed_paths else ""),
            data={"paths": [str(p) for p in unclaimed_paths]},
        )
    )

    missing_rows = conn.execute(
        "SELECT path, missing_since FROM source_files WHERE presence = 'missing' ORDER BY missing_since"
    ).fetchall()
    results.append(
        CheckResult(
            "A", "no missing tracked files",
            "pass" if not missing_rows else "warn",
            f"{len(missing_rows)} file(s) tracked but not found on disk (run `chatvault reconcile`)"
            + (": " + ", ".join(f"{r['path']} (missing since {r['missing_since']})" for r in missing_rows[:5])
               + (" ..." if len(missing_rows) > 5 else "") if missing_rows else ""),
            data={"missing": [{"path": r["path"], "missing_since": r["missing_since"]} for r in missing_rows]},
        )
    )
    return results


# ─────────────── B. structural integrity ───────────────


def _check_structural_integrity(conn: sqlite3.Connection) -> list[CheckResult]:
    results = []

    orphan_msgs = conn.execute(
        "SELECT COUNT(*) FROM messages m LEFT JOIN conversations c ON c.id = m.conv_id WHERE c.id IS NULL"
    ).fetchone()[0]
    results.append(CheckResult("B", "no orphan messages", "pass" if orphan_msgs == 0 else "fail", f"{orphan_msgs} orphan message row(s)"))

    orphan_blocks = conn.execute(
        "SELECT COUNT(*) FROM blocks b LEFT JOIN messages m ON m.id = b.msg_id WHERE m.id IS NULL"
    ).fetchone()[0]
    results.append(CheckResult("B", "no orphan blocks", "pass" if orphan_blocks == 0 else "fail", f"{orphan_blocks} orphan block row(s)"))

    zero_msg_convs = conn.execute("SELECT COUNT(*) FROM conversations WHERE message_count = 0").fetchone()[0]
    results.append(CheckResult("B", "no zero-message conversations", "pass" if zero_msg_convs == 0 else "fail", f"{zero_msg_convs} conversation(s) with message_count=0"))

    zero_block_msgs = conn.execute(
        "SELECT COUNT(*) FROM messages m LEFT JOIN blocks b ON b.msg_id = m.id WHERE b.id IS NULL"
    ).fetchone()[0]
    results.append(CheckResult("B", "no zero-block messages", "pass" if zero_block_msgs == 0 else "warn", f"{zero_block_msgs} message(s) with zero blocks"))

    idx_gaps = conn.execute(
        """
        SELECT conv_id FROM (
          SELECT conv_id, COUNT(*) n, MIN(idx) mn, MAX(idx) mx, SUM(idx) s
          FROM messages GROUP BY conv_id
        ) WHERE NOT (mn = 0 AND mx = n - 1 AND s = n * (n - 1) / 2)
        """
    ).fetchall()
    results.append(CheckResult("B", "message idx contiguous per conversation", "pass" if not idx_gaps else "fail", f"{len(idx_gaps)} conversation(s) with idx gaps/dupes"))

    # deliberate base-table count: FTS index integrity, not a user-facing
    # figure, and it must span every row (including excluded/superseded) to
    # actually verify blocks_fts is in sync with blocks.
    fts_n = conn.execute("SELECT COUNT(*) FROM blocks_fts").fetchone()[0]
    blocks_n = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    results.append(CheckResult("B", "FTS index in sync", "pass" if fts_n == blocks_n else "fail", f"blocks_fts={fts_n} vs blocks={blocks_n}"))

    return results


# ─────────────── C. metadata coverage ───────────────


def _check_metadata_coverage(conn: sqlite3.Connection, platform: str | None) -> list[CheckResult]:
    results = []
    platform_clause = "AND c.platform = ?" if platform else ""
    params = (platform,) if platform else ()

    if platform in KNOWN_NULL_MODEL_PLATFORMS:
        # Scoped to a single known-null-model platform: the model field is
        # genuinely absent from this export, not a coverage gap — report the
        # real numbers but don't fail on them.
        total_assistant = conn.execute(
            f"SELECT COUNT(*) FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id WHERE m.role = 'assistant' {platform_clause}",  # noqa: S608
            params,
        ).fetchone()[0]
        null_model = conn.execute(
            f"SELECT COUNT(*) FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id WHERE m.role = 'assistant' AND m.model IS NULL {platform_clause}",  # noqa: S608
            params,
        ).fetchone()[0]
        pct = _pct(null_model, total_assistant)
        results.append(CheckResult("C", "assistant messages with model", "pass", f"{pct:.1f}% NULL model ({null_model}/{total_assistant}) — known absent from this export, see v_model_coverage"))
    else:
        # Excludes known-null-model platforms from the aggregate so their
        # documented, unavoidable nulls don't drag every whole-DB audit run
        # into warn/fail.
        allowlist_clause = " AND c.platform NOT IN ({})".format(",".join("?" * len(KNOWN_NULL_MODEL_PLATFORMS))) if not platform else ""
        allowlist_params = tuple(KNOWN_NULL_MODEL_PLATFORMS) if not platform else ()
        total_assistant = conn.execute(
            f"SELECT COUNT(*) FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id WHERE m.role = 'assistant' {platform_clause}{allowlist_clause}",  # noqa: S608
            params + allowlist_params,
        ).fetchone()[0]
        null_model = conn.execute(
            f"SELECT COUNT(*) FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id WHERE m.role = 'assistant' AND m.model IS NULL {platform_clause}{allowlist_clause}",  # noqa: S608
            params + allowlist_params,
        ).fetchone()[0]
        pct = _pct(null_model, total_assistant)
        status = "fail" if pct > 20 else ("warn" if pct > 5 else "pass")
        results.append(CheckResult("C", "assistant messages with model", status, f"{pct:.1f}% NULL model ({null_model}/{total_assistant}), excluding known-null platforms {sorted(KNOWN_NULL_MODEL_PLATFORMS)}"))

    total_msgs = conn.execute(f"SELECT COUNT(*) FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id WHERE 1=1 {platform_clause}", params).fetchone()[0]  # noqa: S608
    null_created = conn.execute(
        f"SELECT COUNT(*) FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id WHERE m.created_at IS NULL {platform_clause}",  # noqa: S608
        params,
    ).fetchone()[0]
    pct = _pct(null_created, total_msgs)
    results.append(CheckResult("C", "messages with created_at", "warn" if pct > 5 else "pass", f"{pct:.1f}% NULL created_at ({null_created}/{total_msgs})"))

    total_convs = conn.execute(f"SELECT COUNT(*) FROM v_conversations c WHERE 1=1 {platform_clause}", params).fetchone()[0]  # noqa: S608
    null_title = conn.execute(f"SELECT COUNT(*) FROM v_conversations c WHERE c.title IS NULL {platform_clause}", params).fetchone()[0]  # noqa: S608
    pct = _pct(null_title, total_convs)
    results.append(CheckResult("C", "conversations with title", "warn" if pct > 10 else "pass", f"{pct:.1f}% NULL title ({null_title}/{total_convs})"))

    null_started = conn.execute(f"SELECT COUNT(*) FROM v_conversations c WHERE c.started_at IS NULL {platform_clause}", params).fetchone()[0]  # noqa: S608
    pct = _pct(null_started, total_convs)
    results.append(CheckResult("C", "conversations with started_at", "warn" if pct > 5 else "pass", f"{pct:.1f}% NULL started_at ({null_started}/{total_convs})"))

    # DATASETTE_AND_VIEWS_GUIDE.md §3: six different started_at formats exist
    # across adapters; the harmless variance is 'T' vs ' ' and Z vs
    # '+00:00' (both sort fine). A missing timezone marker entirely is
    # genuinely ambiguous, so it's flagged here rather than silently sorted
    # alongside timestamps that do carry one. The stored value itself is
    # never touched — this only ever reports.
    no_tz = conn.execute(
        f"""
        SELECT uid, title, started_at FROM v_conversations c
        WHERE c.started_at IS NOT NULL AND c.started_at NOT LIKE '%Z' AND c.started_at NOT LIKE '%+%'
        {platform_clause}
        """,  # noqa: S608
        params,
    ).fetchall()
    pct = _pct(len(no_tz), total_convs)
    results.append(
        CheckResult(
            "C", "started_at carries a timezone marker", "warn" if no_tz else "pass",
            f"{len(no_tz)} conversation(s) ({pct:.1f}%) with started_at missing a timezone marker (no 'Z' or '+offset')"
            + (": " + ", ".join(r["uid"] for r in no_tz[:5]) + (" ..." if len(no_tz) > 5 else "") if no_tz else ""),
            data={"uids": [r["uid"] for r in no_tz]},
        )
    )

    return results


# ─────────────── D. content coverage ───────────────


def _percentiles(values: list[int]) -> dict[str, int]:
    if not values:
        return {"min": 0, "p25": 0, "median": 0, "p95": 0, "max": 0}
    s = sorted(values)
    n = len(s)

    def at(p: float) -> int:
        return s[min(n - 1, int(p * n))]

    return {"min": s[0], "p25": at(0.25), "median": at(0.5), "p95": at(0.95), "max": s[-1]}


def _check_content_coverage(conn: sqlite3.Connection, platform: str | None) -> list[CheckResult]:
    results = []
    platform_clause = "AND c.platform = ?" if platform else ""
    params = (platform,) if platform else ()

    total_blocks = conn.execute(f"SELECT COUNT(*) FROM v_blocks b JOIN v_conversations c ON c.id = b.conv_id WHERE 1=1 {platform_clause}", params).fetchone()[0]  # noqa: S608
    unknown_n = conn.execute(f"SELECT COUNT(*) FROM v_blocks b JOIN v_conversations c ON c.id = b.conv_id WHERE b.type = 'unknown' {platform_clause}", params).fetchone()[0]  # noqa: S608
    pct = _pct(unknown_n, total_blocks)
    status = "fail" if pct > 5 else ("warn" if pct > 1 else "pass")
    results.append(CheckResult("D", "unknown block rate", status, f"{pct:.1f}% unknown blocks ({unknown_n}/{total_blocks})"))

    empty_text_n = conn.execute(
        f"SELECT COUNT(*) FROM v_blocks b JOIN v_conversations c ON c.id = b.conv_id WHERE b.type = 'text' AND b.text = '' {platform_clause}",  # noqa: S608
        params,
    ).fetchone()[0]
    pct = _pct(empty_text_n, total_blocks)
    results.append(CheckResult("D", "empty text blocks", "warn" if pct > 1 else "pass", f"{pct:.1f}% empty text-type blocks ({empty_text_n}/{total_blocks})"))

    char_counts = [
        row[0]
        for row in conn.execute(
            f"SELECT c.char_count FROM v_conversations c WHERE 1=1 {platform_clause}", params  # noqa: S608
        ).fetchall()
    ]
    pctl = _percentiles(char_counts)
    results.append(
        CheckResult(
            "D", "char_count distribution", "pass",
            f"min={pctl['min']} p25={pctl['p25']} median={pctl['median']} p95={pctl['p95']} max={pctl['max']}",
        )
    )

    if char_counts:
        cutoff_idx = max(1, len(char_counts) // 100)
        bottom = conn.execute(
            f"""
            SELECT uid, title, char_count FROM v_conversations c WHERE 1=1 {platform_clause}
            ORDER BY char_count ASC LIMIT ?
            """,  # noqa: S608
            (*params, cutoff_idx),
        ).fetchall()
        results.append(
            CheckResult(
                "D", "bottom 1% by char_count (likely truncation)", "pass" if not bottom else "warn",
                f"{len(bottom)} conversation(s): " + ", ".join(f"{r['uid']} ({r['char_count']} chars)" for r in bottom[:10]),
                data={"uids": [r["uid"] for r in bottom]},
            )
        )

    return results


# ─────────────── E. dedupe health ───────────────


def _check_dedupe_health(conn: sqlite3.Connection) -> list[CheckResult]:
    superseded_n = conn.execute("SELECT COUNT(*) FROM conversations WHERE superseded_by IS NOT NULL").fetchone()[0]
    flagged_n = conn.execute("SELECT COUNT(*) FROM conversations WHERE uid LIKE '%#dup-%'").fetchall()
    repeated = conn.execute(
        """
        SELECT content_hash, COUNT(*) AS n, MIN(text) AS preview
        FROM blocks WHERE excluded = 0
        GROUP BY content_hash HAVING COUNT(*) > 20
        ORDER BY n DESC LIMIT 20
        """
    ).fetchall()
    # deliberate base-table count: dedupe_candidates is a workflow queue, not
    # scoped to live conversations — a candidate referencing an already-
    # superseded conversation is still a real open/applied/dismissed item.
    candidates = conn.execute("SELECT status, COUNT(*) AS n FROM dedupe_candidates GROUP BY status").fetchall()

    results = [
        CheckResult("E", "superseded conversations", "pass", f"{superseded_n} conversation(s) superseded"),
        CheckResult("E", "flagged-for-review duplicates", "pass" if not flagged_n else "warn", f"{len(flagged_n)} conversation(s) flagged ambiguous"),
        CheckResult(
            "E", "open dedupe candidates", "pass",
            ", ".join(f"{r['status']}={r['n']}" for r in candidates) or "none",
        ),
        CheckResult(
            "E", "top repeated block clusters", "pass",
            f"{len(repeated)} cluster(s) with >20 occurrences (see report for detail)",
            data={"clusters": [{"content_hash": r["content_hash"], "n": r["n"], "preview": (r["preview"] or "")[:80]} for r in repeated]},
        ),
    ]
    return results


# ─────────────── F. sample manifest ───────────────


def _sample_manifest(conn: sqlite3.Connection, platform: str | None) -> list[dict]:
    platforms = [platform] if platform else [r[0] for r in conn.execute("SELECT DISTINCT platform FROM v_conversations ORDER BY platform").fetchall()]

    samples = []
    for plat in platforms:
        picked: set[int] = set()

        def pick(sql: str, params: tuple = ()) -> sqlite3.Row | None:
            for row in conn.execute(sql, params).fetchall():  # noqa: B023
                if row["id"] not in picked:  # noqa: B023
                    return row
            return None

        slots = [
            ("longest by char_count", "SELECT id, uid, title FROM v_conversations WHERE platform = ? ORDER BY char_count DESC", (plat,)),
            (
                "most tool_use blocks",
                """SELECT c.id, c.uid, c.title FROM v_conversations c
                   JOIN (SELECT conv_id, COUNT(*) n FROM v_blocks WHERE type='tool_use' GROUP BY conv_id) b ON b.conv_id = c.id
                   WHERE c.platform = ? ORDER BY b.n DESC""",
                (plat,),
            ),
            (
                "most unknown blocks",
                """SELECT c.id, c.uid, c.title FROM v_conversations c
                   JOIN (SELECT conv_id, COUNT(*) n FROM v_blocks WHERE type='unknown' GROUP BY conv_id) b ON b.conv_id = c.id
                   WHERE c.platform = ? ORDER BY b.n DESC""",
                (plat,),
            ),
            (
                "has image/attachment (else most blocks)",
                """SELECT DISTINCT c.id, c.uid, c.title FROM v_conversations c
                   JOIN v_blocks b ON b.conv_id = c.id
                   WHERE c.platform = ? AND b.type IN ('image','attachment')""",
                (plat,),
            ),
            ("random control", "SELECT id, uid, title FROM v_conversations WHERE platform = ? ORDER BY RANDOM()", (plat,)),
        ]

        for label, sql, params in slots:
            row = pick(sql, params)
            if row is None and "image/attachment" in label:
                row = pick("SELECT c.id, c.uid, c.title FROM v_conversations c JOIN (SELECT conv_id, COUNT(*) n FROM v_blocks GROUP BY conv_id) b ON b.conv_id=c.id WHERE c.platform=? ORDER BY b.n DESC", (plat,))
            if row is None:
                continue
            picked.add(row["id"])
            samples.append(
                {
                    "platform": plat,
                    "slot": label,
                    "uid": row["uid"],
                    "title": row["title"],
                    "show_cmd": f"chatvault show {row['uid']}",
                    "export_cmd": f"chatvault export --uid {row['uid']}",
                }
            )
    return samples


# ─────────────── orchestration ───────────────


def run(
    conn: sqlite3.Connection,
    config: Config,
    raw_dir: Path | None = None,
    platform: str | None = None,
    sample: bool = False,
) -> AuditReport:
    raw_dir = raw_dir or config.raw_dir
    results: list[CheckResult] = []
    results += _check_ingest_completeness(conn, config, raw_dir)
    results += _check_structural_integrity(conn)
    results += _check_metadata_coverage(conn, platform)
    results += _check_content_coverage(conn, platform)
    results += _check_dedupe_health(conn)

    sample_data = _sample_manifest(conn, platform) if sample else None
    return AuditReport(results=results, sample=sample_data, run_at=_now())


def verdict(report: AuditReport) -> str:
    worst = max((_STATUS_RANK[r.status] for r in report.results), default=0)
    return {0: "pass", 1: "warn", 2: "fail"}[worst]


def exit_code(report: AuditReport, strict: bool = False) -> int:
    v = verdict(report)
    if v == "fail":
        return 2
    if v == "warn":
        return 2 if strict else 1
    return 0


def to_dict(report: AuditReport) -> dict:
    return {
        "run_at": report.run_at,
        "verdict": verdict(report),
        "results": [
            {"group": r.group, "name": r.name, "status": r.status, "detail": r.detail, "data": r.data}
            for r in report.results
        ],
        "sample": report.sample,
    }


def render_markdown(report: AuditReport) -> str:
    lines = [f"# chatvault audit — {report.run_at}", "", f"**Verdict:** {verdict(report).upper()}", ""]
    for group in "ABCDEF":
        group_results = [r for r in report.results if r.group == group]
        if not group_results:
            continue
        lines.append(f"## Group {group}")
        lines.append("| check | status | detail |")
        lines.append("|---|---|---|")
        for r in group_results:
            lines.append(f"| {r.name} | {r.status.upper()} | {r.detail} |")
        lines.append("")
    if report.sample:
        lines.append("## Sample manifest")
        for s in report.sample:
            lines.append(f"- **{s['platform']} / {s['slot']}** — {s['uid']} — `{s['show_cmd']}`")
        lines.append("")
    safe = "YES" if verdict(report) != "fail" else "NO"
    lines.append(f"**Safe to archive sources:** {safe}")
    return "\n".join(lines)


def persist(conn: sqlite3.Connection, report: AuditReport) -> int:
    cur = conn.execute(
        "INSERT INTO audit_runs (run_at, verdict, report_json) VALUES (?, ?, ?)",
        (report.run_at, verdict(report), json.dumps(to_dict(report))),
    )
    conn.commit()
    return cur.lastrowid

"""Conversation-date health: flags for the three ways a timestamp can be missing
or untrustworthy, and the capture-delay measure `audit` reports for live sources.

Nothing here corrects or invents a date (D7): a gap is made queryable through a
`review/*` tag, and the stored value is left exactly as the source gave it.

- `review/no-end-date`      ended_at is NULL (the source carries no last-message time)
- `review/naive-timestamp`  started_at / ended_at has no Z or UTC offset, so its zone is unknown
- `review/implausible-date` a date before 2020 or in the future (epoch-zero, unit errors)
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from chatvault import tags as tags_mod

NO_END_DATE = "review/no-end-date"
NAIVE_TIMESTAMP = "review/naive-timestamp"
IMPLAUSIBLE_DATE = "review/implausible-date"
_FLAGS = (NO_END_DATE, NAIVE_TIMESTAMP, IMPLAUSIBLE_DATE)

PLAUSIBLE_FROM = datetime(2020, 1, 1, tzinfo=timezone.utc)
# Clocks differ and exports are stamped in local time: a day of slack before "future".
_FUTURE_SLACK = timedelta(days=1)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def is_naive(ts: str | None) -> bool:
    dt = _parse(ts)
    return dt is not None and dt.tzinfo is None


def is_implausible(ts: str | None, now: datetime | None = None) -> bool:
    """Before 2020 or in the future. An unparseable value is not judged here."""
    dt = _parse(ts)
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < PLAUSIBLE_FROM or dt > (now or datetime.now(timezone.utc)) + _FUTURE_SLACK


def classify(conn: sqlite3.Connection, conv_ids: list[int] | None = None) -> dict[str, list[int]]:
    """Live conversations (or just `conv_ids`) grouped by which flag applies."""
    sql = "SELECT id, started_at, ended_at FROM v_conversations"
    params: list = []
    if conv_ids is not None:
        if not conv_ids:
            return {f: [] for f in _FLAGS}
        sql += f" WHERE id IN ({','.join('?' * len(conv_ids))})"  # noqa: S608
        params = list(conv_ids)
    out: dict[str, list[int]] = {f: [] for f in _FLAGS}
    for r in conn.execute(sql, params):
        if r["ended_at"] is None:
            out[NO_END_DATE].append(r["id"])
        if is_naive(r["started_at"]) or is_naive(r["ended_at"]):
            out[NAIVE_TIMESTAMP].append(r["id"])
        if is_implausible(r["started_at"]) or is_implausible(r["ended_at"]):
            out[IMPLAUSIBLE_DATE].append(r["id"])
    return out


def flag(conn: sqlite3.Connection, conv_ids: list[int] | None = None, apply: bool = True) -> dict[str, int]:
    """Tag every flagged conversation (idempotent). With apply=False only counts.
    Returns {tag: number of conversations that carry it after the call}."""
    found = classify(conn, conv_ids)
    if apply:
        now = datetime.now(timezone.utc).isoformat()
        for tag, ids in found.items():
            if not ids:
                continue
            tag_id = tags_mod.get_or_create_tag(conn, tag)
            for cid in ids:
                conn.execute(
                    """
                    INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
                    VALUES (?, 'conversation', ?, 'rule', ?)
                    ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
                    """,
                    (tag_id, cid, now),
                )
        conn.commit()
    return {tag: len(ids) for tag, ids in found.items()}


def platform_breakdown(conn: sqlite3.Connection, ids: list[int]) -> dict[str, int]:
    if not ids:
        return {}
    out: dict[str, int] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(
            f"SELECT platform, COUNT(*) n FROM v_conversations WHERE id IN ({','.join('?' * len(chunk))}) GROUP BY platform",  # noqa: S608
            chunk,
        ):
            out[r["platform"]] = out.get(r["platform"], 0) + r["n"]
    return dict(sorted(out.items()))


def capture_delay(conn: sqlite3.Connection) -> list[dict]:
    """Per live source: for conversations whose last message is AFTER that source's
    first ingest, how many days passed before ChatVault captured them.

    Forward-looking on purpose. Conversations that ended before the source was
    first ingested measure "when the archive started", not staleness, and are left
    out. Conversations with a naive `ended_at` are left out too (the zone is
    unknown, so a lag in hours would be a guess)."""
    live = [r["platform"] for r in conn.execute("SELECT DISTINCT platform FROM source_files WHERE kind = 'live' ORDER BY 1")]
    out = []
    for platform in live:
        first = conn.execute("SELECT MIN(ingested_at) FROM conversations WHERE platform = ?", (platform,)).fetchone()[0]
        first_dt = _parse(first)
        if first_dt is None:
            continue
        lags = []
        recent = []  # lags of conversations captured within the last 30 days
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for r in conn.execute(
            "SELECT ended_at, ingested_at FROM v_conversations WHERE platform = ? AND ended_at IS NOT NULL", (platform,)
        ):
            ended, ingested = _parse(r["ended_at"]), _parse(r["ingested_at"])
            if ended is None or ingested is None or ended.tzinfo is None or ingested.tzinfo is None:
                continue
            if ended <= first_dt or is_implausible(r["ended_at"]):
                continue
            lag = max(0.0, (ingested - ended).total_seconds() / 86400)
            lags.append(lag)
            if ingested >= cutoff:
                recent.append(lag)
        if lags:
            lags.sort()
            out.append({
                "platform": platform,
                "n": len(lags),
                "median_days": statistics.median(lags),
                "p95_days": lags[min(len(lags) - 1, int(0.95 * len(lags)))],
                "max_days": lags[-1],
                "recent_n": len(recent),
                "recent_max_days": max(recent) if recent else None,
            })
    return out

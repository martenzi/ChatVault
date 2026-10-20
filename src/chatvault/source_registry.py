"""`chatvault sources status`: one row per *source*, not per file.

Derived from source_files + v_conversations, plus docs/source_registry.md for
the only things the database cannot know: sources that exist but are not (or
only partly) ingested, and deliberate exclusions with their reason.

state is a closed set. `out of scope`, `blocked`, `partial` and `not ingested`
all need a recorded reason; a chat-shaped file under raw/ that no source_files
row and no registry row accounts for makes `sources status` exit non-zero.
"""
from __future__ import annotations

import fnmatch
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from chatvault import adapters, intake
from chatvault.config import Config

REGISTRY_FILE = "docs/source_registry.md"
STATES = ("ingested", "partial", "not ingested", "out of scope", "blocked")
NEEDS_REASON = {"partial", "not ingested", "out of scope", "blocked"}


@dataclass
class RegistryRow:
    source: str
    platform: str
    state: str
    match: str
    reason: str
    files: str = ""


@dataclass
class StatusRow:
    source: str
    platform: str
    adapter: str
    files: str
    convs: str
    newest: str
    state: str
    note: str = ""
    problem: bool = False


def load_registry(config: Config) -> list[RegistryRow]:
    path = config.root / REGISTRY_FILE
    rows: list[RegistryRow] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or cells[0] in ("source", "---") or set(cells[0]) <= {"-"}:
            continue
        rows.append(RegistryRow(cells[0], cells[1], cells[2], cells[3].strip("`"), "|".join(cells[5:]).strip(), cells[4]))
    return rows


def _adapter_platforms() -> set[str]:
    adapters.register_late_adapters()
    return {getattr(m, "platform", None) for m in adapters._REGISTRY.values()} - {None}


def discover_unexplained(conn: sqlite3.Connection, config: Config, registry: list[RegistryRow]) -> dict[str, list[str]]:
    """Chat-shaped files under raw/ that no source_files row, no lifecycle
    folder (excluded/archive/ingested) and no registry row accounts for, grouped
    by top-level folder."""
    from chatvault import sources

    raw = config.raw_dir
    found: dict[str, list[str]] = {}
    covering = [r for r in registry if r.state in ("not ingested", "out of scope", "blocked") and r.match]
    for p in sources.unclaimed(conn, raw, ignore_dir=config.root / "raw_exclusions"):
        rel = str(p.relative_to(raw))
        if not intake.looks_like_chat(p):
            continue
        if intake.content_in_vault(conn, p)[0]:
            continue
        if any(fnmatch.fnmatch(rel, r.match) for r in covering):
            continue
        top = Path(rel).parts[0] if len(Path(rel).parts) > 1 else "(raw root)"
        found.setdefault(top, []).append(rel)
    return found


def build_status(conn: sqlite3.Connection, config: Config) -> list[StatusRow]:
    registry = load_registry(config)
    has_adapter = _adapter_platforms()
    out: list[StatusRow] = []

    for r in conn.execute(
        """
        SELECT sf.platform AS platform, COUNT(*) AS files,
               (SELECT COUNT(*) FROM v_conversations c WHERE c.platform = sf.platform) AS convs,
               (SELECT MAX(substr(c.started_at, 1, 10)) FROM v_conversations c WHERE c.platform = sf.platform) AS newest
        FROM source_files sf GROUP BY sf.platform ORDER BY sf.platform
        """
    ).fetchall():
        out.append(StatusRow(
            r["platform"], r["platform"], "yes" if r["platform"] in has_adapter else "no",
            str(r["files"]), str(r["convs"]), r["newest"] or "—", "ingested",
        ))

    for reg in registry:
        problem = reg.state not in STATES or (reg.state in NEEDS_REASON and not reg.reason)
        out.append(StatusRow(reg.source, reg.platform or "—", "yes" if reg.platform in has_adapter else "no",
                             reg.files or "—", "—", "—", reg.state, reg.reason, problem))

    for top, files in sorted(discover_unexplained(conn, config, registry).items()):
        out.append(StatusRow(top, "—", "no", str(len(files)), "—", "—", "not ingested",
                             "chat-shaped, no source_files row and no recorded reason — add a registry row or an adapter", True))
    return out

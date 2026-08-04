from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from chatvault.config import Config


@dataclass
class CheckResult:
    name: str
    ok: bool
    fatal: bool
    detail: str


def check_sqlite_version() -> CheckResult:
    version = sqlite3.sqlite_version_info
    ok = version >= (3, 35, 0)
    return CheckResult(
        name="sqlite3 version",
        ok=ok,
        fatal=True,
        detail=(
            f"{sqlite3.sqlite_version} (OK, >= 3.35)"
            if ok
            else f"{sqlite3.sqlite_version} is too old — need >= 3.35. "
            "Install a newer sqlite3, e.g. `brew install sqlite` and rebuild "
            "Python against it, or use a Python 3.12+ from a recent brew formula."
        ),
    )


def check_fts5() -> CheckResult:
    conn = sqlite3.connect(":memory:")
    try:
        options = {row[0] for row in conn.execute("PRAGMA compile_options")}
        ok = any("ENABLE_FTS5" in opt for opt in options)
        if not ok:
            # Some builds don't report compile_options for stdlib reasons; fall back
            # to trying to actually create an FTS5 table.
            try:
                conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
                ok = True
            except sqlite3.OperationalError:
                ok = False
        return CheckResult(
            name="FTS5 support",
            ok=ok,
            fatal=True,
            detail="available" if ok else (
                "not compiled into this Python's sqlite3. Install Python via a "
                "recent brew formula (brew install python@3.12) which links "
                "against a modern sqlite3 with FTS5 enabled."
            ),
        )
    finally:
        conn.close()


def check_neo4j(config: Config) -> CheckResult:
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return CheckResult(
            name="neo4j reachability",
            ok=False,
            fatal=False,
            detail="neo4j driver not installed (warn only, not fatal)",
        )
    try:
        driver = GraphDatabase.driver(
            config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password)
        )
        driver.verify_connectivity()
        driver.close()
        return CheckResult(name="neo4j reachability", ok=True, fatal=False, detail=f"reachable at {config.neo4j_uri}")
    except Exception as exc:  # noqa: BLE001 - warn only, any failure is fine to surface
        return CheckResult(
            name="neo4j reachability",
            ok=False,
            fatal=False,
            detail=f"not reachable at {config.neo4j_uri} ({exc.__class__.__name__}) — warn only",
        )


def check_ollama() -> CheckResult:
    from chatvault import embeddings

    try:
        embeddings.embed_text("doctor preflight check")
        return CheckResult(
            name="ollama / semantic search",
            ok=True,
            fatal=False,
            detail=f"reachable, model {embeddings.MODEL!r} responds",
        )
    except embeddings.EmbeddingUnavailable as exc:
        return CheckResult(
            name="ollama / semantic search",
            ok=False,
            fatal=False,
            detail=f"{exc} — warn only, everything else still works",
        )


def run_all(config: Config) -> list[CheckResult]:
    return [check_sqlite_version(), check_fts5(), check_neo4j(config), check_ollama()]

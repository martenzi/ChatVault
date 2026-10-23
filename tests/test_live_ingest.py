from __future__ import annotations

import json
import os
import sqlite3
import sys
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatvault import live_roots, paths
from chatvault.cli import app

FIXTURES = Path(__file__).parent / "fixtures"


def _add(conn, n, path, root, kind="live", presence="present", platform="x"):
    conn.execute(
        "INSERT INTO source_files (path, sha256, platform, adapter, ingested_at, kind, root_dir, presence, last_seen_at) "
        "VALUES (?, ?, ?, 'x', '2026-01-01T00:00:00+00:00', ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (path, f"sha{n}", platform, kind, root, presence),
    )
    conn.commit()


def _direct(conn, source, path):
    conn.execute("INSERT INTO live_roots (source, path, created_at) VALUES (?, ?, '2026-01-01')", (source, str(path)))
    conn.commit()


def _extracted(conn, source, store, script, last_extracted=None):
    conn.execute(
        "INSERT INTO live_roots (source, live_store, extract_cmd, stage_dir, last_extracted_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-01-01')",
        (source, json.dumps([str(store)]), str(script), f"raw/00_inbox/{source}", last_extracted),
    )
    conn.commit()


# ── the registry, not source_files, decides what --live reads ──


def test_phantom_provenance_roots_are_not_live_roots(vault, conn, tmp_path):
    """Defects B and C: 70 nested/typo'd root_dirs, one of them marked live, used to become 70 scans."""
    real = tmp_path / "tree"
    real.mkdir()
    for i, d in enumerate(["tree", "tree/a", "tree/a/b", "elsewhere"]):
        _add(conn, i, f"/{d}/x.jsonl", f"/{d}")
    _direct(conn, "claude_code", real)
    assert [s.source for s in live_roots.statuses(conn)] == ["claude_code"]
    assert len(live_roots.active(conn)) == 1


def test_direct_source_missing_store_is_loud_and_names_the_retire_command(vault, conn, monkeypatch):
    _direct(conn, "ghost", "/nonexistent/tree")
    conn.close()
    monkeypatch.setenv("CHATVAULT_ROOT", str(vault.root))
    result = CliRunner().invoke(app, ["ingest", "--live"])
    assert result.exit_code == 1
    assert "MISSING" in result.output and "/nonexistent/tree" in result.output
    assert "live-roots disable" in result.output


def test_empty_registry_points_at_init(vault, conn, monkeypatch):
    conn.close()
    monkeypatch.setenv("CHATVAULT_ROOT", str(vault.root))
    result = CliRunner().invoke(app, ["ingest", "--live"])
    assert result.exit_code == 1 and "live-roots init" in result.output


def test_disabled_source_is_skipped_not_deleted(vault, conn, tmp_path):
    _direct(conn, "old", tmp_path)
    assert live_roots.deactivate(conn, "old", "retired on purpose") == 1
    assert live_roots.statuses(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM live_roots").fetchone()[0] == 1


def test_defaults_are_idempotent_and_one_extractor_per_store(vault, conn):
    first = live_roots.seed_defaults(conn)
    assert first == len(live_roots.DEFAULTS)
    assert live_roots.seed_defaults(conn) == 0
    sources = {r["source"] for r in live_roots.active(conn)}
    assert {"claude_code", "openclaw", "hermes", "devin_cli", "devin_desktop_acp", "cursor", "antigravity"} == sources
    # a staging directory is never a registered scan path
    assert all(not (r["path"] or "").endswith(("raw/devin_cli", "raw/devin_desktop_acp")) for r in live_roots.active(conn))


# ── staleness is measured against the live store ──


def test_extracted_source_is_stale_when_store_changed_after_last_extraction(vault, conn, tmp_path):
    store = tmp_path / "sessions.db"
    store.write_text("x")
    old = time.time() - 5 * 86400
    _extracted(conn, "devin_cli", store, "scripts/extract_devin_cli.py", last_extracted=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(old)))
    (st,) = live_roots.statuses(conn)
    assert st.state == "STALE" and st.extracted and st.data_age is not None


def test_wal_sibling_counts_as_a_store_change(vault, conn, tmp_path):
    store = tmp_path / "sessions.db"
    store.write_text("x")
    os.utime(store, (time.time() - 9 * 86400,) * 2)
    wal = tmp_path / "sessions.db-wal"
    wal.write_text("w")  # recent write sits in the WAL first
    _extracted(conn, "devin_cli", store, "x.py", last_extracted="2026-01-01T00:00:00+00:00")
    row = live_roots.active(conn)[0]
    assert live_roots.newest_mtime(row) >= wal.stat().st_mtime - 1


def test_extracted_source_ok_when_extracted_after_last_store_change(vault, conn, tmp_path):
    store = tmp_path / "sessions.db"
    store.write_text("x")
    os.utime(store, (time.time() - 3600,) * 2)
    _extracted(conn, "devin_cli", store, "x.py", last_extracted=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    assert live_roots.statuses(conn)[0].state == "ok"


def test_extracted_source_missing_store(vault, conn, tmp_path):
    _extracted(conn, "devin_cli", tmp_path / "gone.db", "x.py")
    assert live_roots.statuses(conn)[0].state == "MISSING"


# ── --live extracts, ingests and files the snapshot ──


def _fake_extractor(tmp_path: Path) -> Path:
    script = tmp_path / "fake_extract.py"
    script.write_text(textwrap.dedent(f"""
        import argparse, shutil
        from pathlib import Path
        ap = argparse.ArgumentParser(); ap.add_argument("--out-dir", type=Path)
        out = ap.parse_args().out_dir / "2026-10-23"; out.mkdir(parents=True, exist_ok=True)
        shutil.copy({str(FIXTURES / "devin_cli" / "real_session.jsonl")!r}, out / "sessions.jsonl")
        print("wrote", out / "sessions.jsonl")
    """))
    return script


def test_ingest_live_extracts_ingests_and_files_the_snapshot(vault, conn, tmp_path, monkeypatch):
    store = tmp_path / "sessions.db"
    store.write_text("x")
    _extracted(conn, "devin_cli", store, _fake_extractor(tmp_path))
    conn.close()
    monkeypatch.setenv("CHATVAULT_ROOT", str(vault.root))
    result = CliRunner().invoke(app, ["ingest", "--live"])
    assert result.exit_code == 0, result.output
    c = sqlite3.connect(vault.db_path)
    assert c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] >= 1
    (path, kind, root) = c.execute("SELECT path, kind, root_dir FROM source_files").fetchone()
    # filed by intake, not left in the inbox, and a snapshot is frozen — not a live root
    assert "10_ingested/devin_cli" in path
    assert kind == "frozen"
    assert not list((vault.raw_dir / "00_inbox").rglob("*.jsonl"))
    assert c.execute("SELECT last_extracted_at FROM live_roots").fetchone()[0] is not None


def test_ingest_live_dry_run_extracts_nothing(vault, conn, tmp_path, monkeypatch):
    store = tmp_path / "sessions.db"
    store.write_text("x")
    _extracted(conn, "devin_cli", store, _fake_extractor(tmp_path))
    conn.close()
    monkeypatch.setenv("CHATVAULT_ROOT", str(vault.root))
    result = CliRunner().invoke(app, ["ingest", "--live", "--dry-run"])
    assert result.exit_code == 0 and "would extract" in result.output
    assert not list(vault.raw_dir.rglob("*.jsonl"))


def test_failed_extraction_stops_before_ingesting(vault, conn, tmp_path, monkeypatch):
    store = tmp_path / "sessions.db"
    store.write_text("x")
    bad = tmp_path / "bad.py"
    bad.write_text("import sys; print('boom', file=sys.stderr); sys.exit(2)")
    _extracted(conn, "devin_cli", store, bad)
    conn.close()
    monkeypatch.setenv("CHATVAULT_ROOT", str(vault.root))
    result = CliRunner().invoke(app, ["ingest", "--live"])
    assert result.exit_code == 1 and "extraction failed" in result.output


# ── one spelling per directory ──

needs_case_insensitive_fs = pytest.mark.skipif(
    sys.platform != "darwin", reason="letter-case aliasing is an APFS/HFS+ behaviour"
)


@needs_case_insensitive_fs
def test_canonical_restores_on_disk_letter_case(tmp_path):
    real = tmp_path / "ChatVault" / "raw"
    real.mkdir(parents=True)
    wrong = tmp_path / "Chatvault" / "raw"
    if not wrong.exists():
        pytest.skip("filesystem is case-sensitive")
    assert paths.canonical(wrong) == str(real.resolve())
    assert paths.is_canonical(real.resolve())
    assert not paths.is_canonical(wrong)


@needs_case_insensitive_fs
def test_canonical_keeps_a_vanished_tail_but_fixes_its_directory(tmp_path):
    (tmp_path / "ChatVault").mkdir()
    got = paths.canonical(tmp_path / "Chatvault" / "raw" / "gone.jsonl")
    assert got == str((tmp_path / "ChatVault").resolve() / "raw" / "gone.jsonl")


@needs_case_insensitive_fs
def test_load_config_canonicalises_a_wrong_case_root(tmp_path, monkeypatch):
    (tmp_path / "ChatVault").mkdir()
    monkeypatch.setenv("CHATVAULT_ROOT", str(tmp_path / "Chatvault"))
    monkeypatch.chdir(tmp_path)
    from chatvault.config import load_config
    assert load_config().root == (tmp_path / "ChatVault").resolve()


def test_a_root_dir_differing_only_by_case_cannot_register_a_second_root(vault, conn):
    _add(conn, 1, "/Users/x/ChatVault/raw/a.jsonl", "/Users/x/ChatVault/raw")
    with pytest.raises(sqlite3.IntegrityError, match="differs only by letter case"):
        _add(conn, 2, "/Users/x/Chatvault/raw/b.jsonl", "/Users/x/Chatvault/raw")
    # the same spelling is of course fine
    _add(conn, 3, "/Users/x/ChatVault/raw/c.jsonl", "/Users/x/ChatVault/raw")


def test_case_guard_also_covers_updates(vault, conn):
    _add(conn, 1, "/a/ChatVault/x", "/a/ChatVault")
    _add(conn, 2, "/a/other/y", "/a/other")
    with pytest.raises(sqlite3.IntegrityError, match="differs only by letter case"):
        conn.execute("UPDATE source_files SET root_dir = '/a/Chatvault' WHERE sha256 = 'sha2'")


@needs_case_insensitive_fs
def test_normalise_collapses_duplicate_roots_and_freezes_snapshots(vault, conn, tmp_path):
    real = tmp_path / "ChatVault" / "raw"
    real.mkdir(parents=True)
    wrong = str(tmp_path / "Chatvault" / "raw")
    if not Path(wrong).exists():
        pytest.skip("filesystem is case-sensitive")
    good = str(real.resolve())
    # build the historical duplicate by lifting the guards, as the old code could
    guards = conn.execute("SELECT sql FROM sqlite_master WHERE name LIKE 'trg_source_files_root_case_%'").fetchall()
    for t in ("trg_source_files_root_case_insert", "trg_source_files_root_case_update"):
        conn.execute(f"DROP TRIGGER {t}")
    _add(conn, 1, f"{good}/a.jsonl", good, kind="frozen", platform="openclaw")
    _add(conn, 2, f"{wrong}/b.jsonl", wrong, kind="live", platform="devin_cli")
    _add(conn, 3, f"{wrong}/c.jsonl", wrong, kind="live", platform="claude_code")
    for (sql,) in guards:  # production has the guards; they do not validate rows already stored
        conn.execute(sql)

    dry = live_roots.normalise(conn, apply=False)
    assert dry["roots_before"] == 2 and dry["roots_after"] == 1 and dry["rows_rewritten"] == 2
    assert conn.execute("SELECT COUNT(DISTINCT root_dir) FROM source_files").fetchone()[0] == 2  # dry run wrote nothing

    rep = live_roots.normalise(conn, apply=True)
    assert rep["rows_rewritten"] == 2 and rep["snapshots_to_frozen"] == 1
    assert conn.execute("SELECT COUNT(DISTINCT root_dir) FROM source_files").fetchone()[0] == 1
    kinds = dict(conn.execute("SELECT platform, kind FROM source_files"))
    assert kinds["devin_cli"] == "frozen" and kinds["claude_code"] == "live"  # a real live source is untouched
    assert conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 3  # rewritten, never deleted
    assert rep["registry_added"] == len(live_roots.DEFAULTS)
    # the guards are back
    with pytest.raises(sqlite3.IntegrityError):
        _add(conn, 9, f"{wrong}/z.jsonl", wrong)

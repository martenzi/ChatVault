from __future__ import annotations

import shutil

from chatvault import audit, ingest, reconcile, sources
from tests.conftest import FIXTURES


def _stage_live_source(vault, conn):
    """Copies the claude_code fixture into raw/ (so it's a real, deletable
    file rather than the read-only fixture) and ingests + marks it live."""
    staged_dir = vault.raw_dir / "claude_code_live"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged = staged_dir / "session1.jsonl"
    shutil.copy2(FIXTURES / "claude_code" / "session1.jsonl", staged)

    ingest.ingest_path(conn, staged, config=vault)
    root_dir = conn.execute("SELECT root_dir FROM source_files LIMIT 1").fetchone()["root_dir"]
    sources.mark_kind(conn, root_dir, "live")
    return staged, root_dir


def test_reconcile_reports_present_file_as_still_present(vault, conn):
    _stage_live_source(vault, conn)
    report = reconcile.reconcile(conn, kind="live")
    assert report.checked == 1
    assert report.still_present == 1
    assert report.newly_missing == 0
    row = conn.execute("SELECT presence FROM source_files LIMIT 1").fetchone()
    assert row["presence"] == "present"


def test_reconcile_flags_deleted_file_as_missing(vault, conn):
    staged, _ = _stage_live_source(vault, conn)
    staged.unlink()

    report = reconcile.reconcile(conn, kind="live")
    assert report.newly_missing == 1
    row = conn.execute("SELECT presence, missing_since FROM source_files LIMIT 1").fetchone()
    assert row["presence"] == "missing"
    assert row["missing_since"] is not None


def test_reconcile_does_not_overwrite_missing_since_on_repeat_runs(vault, conn):
    staged, _ = _stage_live_source(vault, conn)
    staged.unlink()

    reconcile.reconcile(conn, kind="live")
    first = conn.execute("SELECT missing_since FROM source_files LIMIT 1").fetchone()["missing_since"]

    reconcile.reconcile(conn, kind="live")
    second = conn.execute("SELECT missing_since FROM source_files LIMIT 1").fetchone()["missing_since"]
    assert first == second


def test_reconcile_reappeared_when_file_returns(vault, conn):
    staged, _ = _stage_live_source(vault, conn)
    original = staged.read_text()
    staged.unlink()
    reconcile.reconcile(conn, kind="live")

    staged.write_text(original)
    report = reconcile.reconcile(conn, kind="live")
    assert report.reappeared == 1
    row = conn.execute("SELECT presence, missing_since FROM source_files LIMIT 1").fetchone()
    assert row["presence"] == "present"
    assert row["missing_since"] is None


def test_reconcile_default_kind_live_ignores_frozen_sources(vault, conn):
    # Default kind stays 'frozen' unless explicitly marked live.
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl", config=vault)
    # session1.jsonl in fixtures/ always exists, so "missing" here would only
    # ever trigger from scope leaking into kind='frozen' — assert it doesn't.
    report = reconcile.reconcile(conn, kind="live")
    assert report.checked == 0


def test_reconcile_dry_run_does_not_write(vault, conn):
    staged, _ = _stage_live_source(vault, conn)
    staged.unlink()

    report = reconcile.reconcile(conn, kind="live", dry_run=True)
    assert report.newly_missing == 1
    row = conn.execute("SELECT presence, missing_since FROM source_files LIMIT 1").fetchone()
    assert row["presence"] == "present"
    assert row["missing_since"] is None
    assert conn.execute("SELECT COUNT(*) FROM reconcile_runs").fetchone()[0] == 0


def test_retire_marks_row_and_future_reconciles_skip_it(vault, conn):
    staged, root_dir = _stage_live_source(vault, conn)
    staged.unlink()

    n = reconcile.retire(conn, root_dir, "moved to /Volumes/Archive")
    assert n == 1
    row = conn.execute("SELECT presence, presence_note FROM source_files LIMIT 1").fetchone()
    assert row["presence"] == "retired"
    assert row["presence_note"] == "moved to /Volumes/Archive"

    report = reconcile.reconcile(conn, kind="live")
    assert report.checked == 0  # retired rows are never reported as missing


def test_reconcile_persists_run(vault, conn):
    _stage_live_source(vault, conn)
    reconcile.reconcile(conn, kind="live")
    run_row = conn.execute("SELECT * FROM reconcile_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run_row is not None
    assert run_row["checked"] == 1


def test_ingest_on_live_root_auto_reconciles(vault, conn):
    staged_dir = vault.raw_dir / "claude_code_live"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged1 = staged_dir / "session1.jsonl"
    shutil.copy2(FIXTURES / "claude_code" / "session1.jsonl", staged1)
    extra_fixture = FIXTURES / "generic" / "toolsession.jsonl"
    staged2 = staged_dir / "session2.jsonl"
    shutil.copy2(extra_fixture, staged2)

    ingest.ingest_path(conn, staged_dir, config=vault)
    root_dir = conn.execute("SELECT DISTINCT root_dir FROM source_files").fetchone()["root_dir"]
    sources.mark_kind(conn, root_dir, "live")

    # Delete one of the two tracked files, then re-ingest the directory —
    # per spec A.4, ingest on a live root must auto-run reconcile afterwards.
    staged2.unlink()
    ingest.ingest_path(conn, staged_dir, config=vault)

    presences = {r["presence"] for r in conn.execute("SELECT presence FROM source_files").fetchall()}
    assert "missing" in presences
    assert conn.execute("SELECT COUNT(*) FROM reconcile_runs").fetchone()[0] >= 1


def test_audit_warns_on_missing_tracked_file(vault, conn):
    staged, _ = _stage_live_source(vault, conn)
    staged.unlink()
    reconcile.reconcile(conn, kind="live")

    report = audit.run(conn, vault)
    check = [r for r in report.results if r.name == "no missing tracked files"][0]
    assert check.status == "warn"
    assert "1 file(s)" in check.detail


def test_export_manifest_lists_missing_and_retired(vault, conn):
    staged, root_dir = _stage_live_source(vault, conn)
    staged.unlink()
    reconcile.reconcile(conn, kind="live")

    out = sources.export_manifest(conn, vault, out=vault.root / "MANIFEST.md")
    content = out.read_text()
    assert "## Missing — tracked but not found on disk" in content
    assert str(staged) in content

    reconcile.retire(conn, root_dir, "archived deliberately")
    out = sources.export_manifest(conn, vault, out=vault.root / "MANIFEST.md")
    content = out.read_text()
    assert "## Retired — deliberately archived, expected absence" in content
    assert "archived deliberately" in content


def test_reconcile_reports_moved_file_and_updates_path(vault, conn):
    staged, _ = _stage_live_source(vault, conn)
    dest_dir = vault.raw_dir / "10_ingested" / "claude_code"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / staged.name
    staged.rename(dest)

    report = reconcile.reconcile(conn, kind="live", search_dirs=[vault.raw_dir])
    assert report.moved == 1
    assert report.newly_missing == 0
    row = conn.execute("SELECT path, presence, presence_note FROM source_files LIMIT 1").fetchone()
    assert row["path"] == str(dest)
    assert row["presence"] == "present"
    assert "moved from" in row["presence_note"]

    again = reconcile.reconcile(conn, kind="live", search_dirs=[vault.raw_dir])
    assert again.moved == 0 and again.newly_missing == 0


def test_reconcile_move_scan_off_reads_move_as_missing(vault, conn):
    staged, _ = _stage_live_source(vault, conn)
    staged.rename(vault.raw_dir / "renamed.jsonl")
    report = reconcile.reconcile(conn, kind="live", move_scan=False, search_dirs=[vault.raw_dir])
    assert report.moved == 0 and report.newly_missing == 1


def test_reconcile_missing_only_when_bytes_gone(vault, conn):
    staged, _ = _stage_live_source(vault, conn)
    # same name elsewhere but different bytes is not a move
    other = vault.raw_dir / "elsewhere.jsonl"
    other.write_text(staged.read_text() + "\n")
    staged.unlink()
    report = reconcile.reconcile(conn, kind="live", search_dirs=[vault.raw_dir])
    assert report.moved == 0 and report.newly_missing == 1


def test_reconcile_flags_changed_frozen_file(vault, conn):
    staged, root_dir = _stage_live_source(vault, conn)
    conn.execute("UPDATE source_files SET kind = 'frozen'")
    conn.commit()
    staged.write_text("{}\n")
    report = reconcile.reconcile(conn, kind="frozen")
    assert report.changed == 1 and report.newly_missing == 0

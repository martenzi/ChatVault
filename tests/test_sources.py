from __future__ import annotations

from chatvault import ingest, sources
from tests.conftest import FIXTURES


def test_unclaimed_reports_file_dropped_post_ingest(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    assert sources.unclaimed(conn, vault.raw_dir) == []

    extra = vault.raw_dir / "extra.jsonl"
    extra.write_text((FIXTURES / "generic" / "toolsession.jsonl").read_text())

    unclaimed = sources.unclaimed(conn, vault.raw_dir)
    assert len(unclaimed) == 1
    assert unclaimed[0].name == "extra.jsonl"


def test_unclaimed_ignores_raw_exclusions(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")

    exclusions_dir = vault.root / "raw_exclusions" / "index-dumps"
    exclusions_dir.mkdir(parents=True)
    (exclusions_dir / "junk.jsonl").write_text('{"not": "ingestable"}\n')

    unclaimed = sources.unclaimed(conn, vault.raw_dir, ignore_dir=vault.root / "raw_exclusions")
    assert unclaimed == []


def test_guard_refuses_direct_ingest_of_raw_exclusions_without_force(vault, conn):
    exclusions_dir = vault.root / "raw_exclusions"
    exclusions_dir.mkdir(parents=True, exist_ok=True)
    (exclusions_dir / "junk.jsonl").write_text((FIXTURES / "generic" / "toolsession.jsonl").read_text())

    try:
        ingest.ingest_path(conn, exclusions_dir, config=vault)
        raised = False
    except ValueError:
        raised = True
    assert raised

    # with force, it proceeds (even though there's nothing meaningfully
    # different about the content — the point is the guard doesn't fire)
    summary = ingest.ingest_path(conn, exclusions_dir, config=vault, force=True)
    assert summary.totals["inserted"] == 1


def test_export_manifest_reads_why_md_and_flags_undocumented_subdir(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")

    exclusions_dir = vault.root / "raw_exclusions"
    (exclusions_dir / "documented").mkdir(parents=True)
    (exclusions_dir / "undocumented").mkdir(parents=True)
    (exclusions_dir / "_why.md").write_text(
        "| Path | Reason |\n|---|---|\n| documented | index files, not conversations |\n"
    )

    out = sources.export_manifest(conn, vault, out=vault.root / "MANIFEST.md")
    content = out.read_text()
    assert "documented | index files, not conversations" in content
    assert "undocumented | ⚠ undocumented" in content


def test_export_manifest_stub_when_why_md_missing(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    out = sources.export_manifest(conn, vault, out=vault.root / "MANIFEST.md")
    content = out.read_text()
    assert "generate entries by editing raw_exclusions/_why.md" in content


def test_mark_kind_and_manifest_live_vs_frozen_sections(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    row = conn.execute("SELECT root_dir FROM source_files LIMIT 1").fetchone()
    n = sources.mark_kind(conn, row["root_dir"], "live")
    assert n == 1

    kind = conn.execute("SELECT kind FROM source_files WHERE root_dir = ?", (row["root_dir"],)).fetchone()["kind"]
    assert kind == "live"

    out = sources.export_manifest(conn, vault, out=vault.root / "MANIFEST.md")
    content = out.read_text()
    assert "## Live (re-ingest periodically)" in content
    assert "## Frozen (ingest once, archive)" in content

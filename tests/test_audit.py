from __future__ import annotations

from chatvault import audit, ingest
from tests.conftest import FIXTURES


def test_audit_passes_on_healthy_vault(vault, conn):
    # claude_code's fixture has full model coverage (unlike claude_desktop, which
    # genuinely lacks model data in its source — audit.KNOWN_NULL_MODEL_PLATFORMS
    # excludes it from the model-coverage check for exactly that reason, so
    # it would pass anyway, but claude_code stays the cleaner "nothing's
    # wrong" baseline).
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl", config=vault)
    report = audit.run(conn, vault)
    assert audit.verdict(report) in ("pass", "warn")  # tiny fixtures can legitimately warn on char_count percentile
    fails = [r for r in report.results if r.status == "fail"]
    assert fails == []


def test_audit_catches_unclaimed_file(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl", config=vault)
    extra = vault.raw_dir / "dropped.jsonl"
    extra.write_text((FIXTURES / "generic" / "toolsession.jsonl").read_text())

    report = audit.run(conn, vault)
    group_a = [r for r in report.results if r.group == "A" and r.name == "no unclaimed files"][0]
    assert group_a.status == "fail"
    assert "dropped.jsonl" in group_a.detail


def test_audit_catches_deleted_block(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl", config=vault)
    block_id = conn.execute("SELECT id FROM blocks LIMIT 1").fetchone()[0]
    conn.execute("DELETE FROM blocks WHERE id = ?", (block_id,))
    conn.commit()

    report = audit.run(conn, vault)
    zero_block_check = [r for r in report.results if r.name == "no zero-block messages"][0]
    assert zero_block_check.status == "warn"


def test_audit_catches_null_model(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "windsurf_legacy" / "full_schema_sample.jsonl", config=vault)
    conn.execute("UPDATE messages SET model = NULL WHERE role = 'assistant'")
    conn.commit()

    report = audit.run(conn, vault)
    model_check = [r for r in report.results if r.name == "assistant messages with model"][0]
    assert model_check.status == "fail"


def test_audit_known_null_model_platform_does_not_fail(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json", config=vault)

    report = audit.run(conn, vault, platform="claude_desktop")
    model_check = [r for r in report.results if r.name == "assistant messages with model"][0]
    assert model_check.status == "pass"

    # Whole-DB run excludes claude_desktop from the aggregate too, so its
    # known-absent model data doesn't drag the overall check into fail.
    report_all = audit.run(conn, vault)
    model_check_all = [r for r in report_all.results if r.name == "assistant messages with model"][0]
    assert model_check_all.status != "fail"


def test_audit_strict_promotes_warn_to_fail_exit_code(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl", config=vault)
    # force a WARN deterministically: null out started_at (>5% WARN threshold)
    conn.execute("UPDATE conversations SET started_at = NULL")
    conn.commit()
    report = audit.run(conn, vault)
    assert audit.verdict(report) == "warn"
    assert audit.exit_code(report, strict=False) == 1
    assert audit.exit_code(report, strict=True) == 2


def test_audit_sample_emits_pasteable_commands(vault, conn):
    ingest.ingest_path(conn, FIXTURES, config=vault)
    report = audit.run(conn, vault, sample=True)
    assert report.sample
    for s in report.sample:
        assert s["show_cmd"].startswith("chatvault show ")
        assert s["export_cmd"].startswith("chatvault export --uid ")


def test_audit_ignores_raw_exclusions(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl", config=vault)
    exclusions_dir = vault.root / "raw_exclusions"
    exclusions_dir.mkdir(parents=True)
    (exclusions_dir / "junk.jsonl").write_text((FIXTURES / "generic" / "toolsession.jsonl").read_text())

    report = audit.run(conn, vault)
    group_a = [r for r in report.results if r.name == "no unclaimed files"][0]
    assert group_a.status == "pass"


def test_audit_persists_run(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "conversations.json", config=vault)
    report = audit.run(conn, vault)
    audit.persist(conn, report)

    row = conn.execute("SELECT verdict FROM audit_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["verdict"] == audit.verdict(report)

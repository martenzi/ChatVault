from datetime import datetime, timezone
from pathlib import Path

from chatvault import display
from chatvault.ingest import FileIngestResult, IngestSummary


def test_truncate_display_collapses_newlines_and_ellipsis():
    assert display.truncate_display(None) == ""
    assert display.truncate_display("short") == "short"
    assert display.truncate_display("a\nb\nc", max_chars=4) == "a b…"
    huge = "Created Gemini Canvas titled\n" + ("x" * 500)
    out = display.truncate_display(huge, max_chars=40)
    assert "\n" not in out
    assert len(out) == 40
    assert out.endswith("…")


def test_relative_display_path():
    root = Path("/vault/raw")
    assert display.relative_display_path(Path("/vault/raw/grok/export.json"), root) == "grok/export.json"
    assert display.relative_display_path(Path("/elsewhere/file.json"), root) == "/elsewhere/file.json"


def test_select_ingest_detail_rows_collapses_dups_by_default():
    files = [
        FileIngestResult(Path("a.json"), "skipped_dup", adapter="grok"),
        FileIngestResult(Path("b.json"), "skipped_no_adapter"),
        FileIngestResult(Path("c.json"), "ok", adapter="chatgpt", inserted=1),
        FileIngestResult(Path("d.json"), "failed", adapter="x", error="boom"),
        FileIngestResult(Path("e.json"), "skipped_dup", adapter="vscode_legacy"),
    ]
    rows, omitted = display.select_ingest_detail_rows(files, verbose=False)
    assert [r.status for r in rows] == ["failed", "skipped_no_adapter", "ok"]
    assert omitted == 0

    rows_v, omitted_v = display.select_ingest_detail_rows(files, verbose=True)
    assert len(rows_v) == 5
    assert omitted_v == 0
    assert rows_v[0].status == "failed"
    assert rows_v[-1].status == "skipped_dup"


def test_select_ingest_detail_rows_respects_cap():
    files = [
        FileIngestResult(Path(f"f{i}.json"), "skipped_no_adapter") for i in range(10)
    ]
    rows, omitted = display.select_ingest_detail_rows(files, verbose=False, cap=3)
    assert len(rows) == 3
    assert omitted == 7


def test_write_ingest_log(tmp_path: Path):
    summary = IngestSummary(
        files=[
            FileIngestResult(tmp_path / "raw" / "ok.json", "ok", adapter="grok", inserted=2),
            FileIngestResult(tmp_path / "raw" / "dup.json", "skipped_dup", adapter="grok"),
        ]
    )
    log = display.write_ingest_log(
        summary,
        tmp_path / "logs" / "ingest-test.txt",
        ingest_path=tmp_path / "raw",
        root=tmp_path / "raw",
    )
    text = log.read_text()
    assert "status counts:" in text
    assert "skipped_dup" in text
    assert "ok\tgrok\t2\t0\t0\t0\t0\tok.json\t" in text
    assert "skipped_dup\tgrok\t0\t0\t0\t0\t0\tdup.json\t" in text


def test_ingest_log_path_stamp():
    when = datetime(2026, 10, 7, 12, 34, 56, tzinfo=timezone.utc)
    assert display.ingest_log_path(Path("/vault/logs"), when) == Path(
        "/vault/logs/ingest-20261007-123456.txt"
    )

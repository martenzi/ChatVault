from __future__ import annotations

import shutil
from datetime import datetime

from chatvault import ingest, intake, reconcile
from tests.conftest import FIXTURES


def _drop(vault, name="session1.jsonl", sub=""):
    inbox = vault.raw_dir / intake.INBOX / sub
    inbox.mkdir(parents=True, exist_ok=True)
    dst = inbox / name
    shutil.copy2(FIXTURES / "claude_code" / "session1.jsonl", dst)
    return dst


def _run(vault, conn, apply, ingest_fn=None):
    fn = ingest_fn or (lambda p: ingest.ingest_path(conn, p, config=vault))
    return intake.run_intake(conn, vault, apply=apply, ingest_fn=fn)


def test_dry_run_moves_nothing(vault, conn):
    f = _drop(vault)
    (item,) = _run(vault, conn, apply=False)
    assert item.action == "would_move" and f.is_file()
    assert conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0


def test_apply_files_the_ingest_and_keeps_paths_in_sync(vault, conn):
    f = _drop(vault)
    (item,) = _run(vault, conn, apply=True)
    today = datetime.now().strftime("%Y-%m-%d")
    assert item.action == "moved" and not f.exists()
    dest = vault.raw_dir / intake.INGESTED / "claude_code" / today / "session1.jsonl"
    assert dest.is_file()
    row = conn.execute("SELECT path, presence FROM source_files").fetchone()
    assert row["path"].endswith(f"{intake.INGESTED}/claude_code/{today}/session1.jsonl")
    # a reconcile right after must find nothing moved and nothing missing
    rep = reconcile.reconcile(conn, kind="all", search_dirs=[vault.raw_dir])
    assert rep.moved == 0 and rep.newly_missing == 0 and rep.missing == []
    assert (vault.raw_dir / intake.STATUS_FILE).is_file()


def test_failed_ingest_stays_in_inbox(vault, conn):
    f = _drop(vault)

    def boom(p):
        raise RuntimeError("parse exploded")

    (item,) = _run(vault, conn, apply=True, ingest_fn=boom)
    assert item.action == "failed" and f.is_file()


def test_unclaimed_chat_shaped_goes_to_needs_adapter_and_other_stays(vault, conn):
    inbox = vault.raw_dir / intake.INBOX
    inbox.mkdir(parents=True)
    chat = inbox / "mystery.json"
    chat.write_text('[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]')
    other = inbox / "notes.txt"
    other.write_text("just a note")
    items = {i.path.name: i for i in _run(vault, conn, apply=True)}
    assert items["mystery.json"].action == "needs_adapter"
    assert (vault.raw_dir / intake.NEEDS_ADAPTER / "mystery.json").is_file()
    assert items["notes.txt"].action == "unclaimed_nonchat" and other.is_file()


def test_rerun_with_duplicate_file_is_filed_not_reingested(vault, conn):
    _drop(vault)
    _run(vault, conn, apply=True)
    _drop(vault)  # same bytes dropped again
    (item,) = _run(vault, conn, apply=True)
    assert item.action == "dup_moved"
    assert conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 1


def test_classify_tracked_companion_excluded_and_reorganise(vault, conn):
    exp = vault.raw_dir / "my-claude-export"
    exp.mkdir()
    shutil.copy2(FIXTURES / "claude_code" / "session1.jsonl", exp / "session1.jsonl")
    ingest.ingest_path(conn, exp, config=vault)
    (exp / "asset.bin").write_bytes(b"\x00\x01binary companion")
    (vault.raw_dir / "empty.txt").write_text("")
    entries = intake.classify_tree(conn, vault.raw_dir)
    intake.assign_destinations(entries)
    by = {e.rel: e for e in entries}
    assert by["my-claude-export/session1.jsonl"].bucket == "tracked"
    assert by["my-claude-export/asset.bin"].bucket == "companion"
    assert by["my-claude-export/asset.bin"].platform == "claude_code"
    assert by["empty.txt"].bucket == "excluded" and by["empty.txt"].reason == "empty-files"

    result = intake.apply_reorganisation(conn, vault, entries)
    assert result["reconcile"]["missing"] == 0 and result["reconcile"]["moved"] == 1
    assert not exp.exists()
    moved = list((vault.raw_dir / intake.INGESTED / "claude_code").rglob("session1.jsonl"))
    assert len(moved) == 1
    assert (vault.raw_dir / intake.EXCLUDED / "empty-files" / "empty.txt").is_file()
    # idempotent: a second classification moves nothing
    again = intake.classify_tree(conn, vault.raw_dir)
    intake.assign_destinations(again)
    assert [e for e in again if e.dest_rel and e.dest_rel != e.rel] == []


def test_status_file_reports_sections_and_flags_only_needs_adapter(vault, conn):
    inbox = vault.raw_dir / intake.INBOX
    inbox.mkdir(parents=True)
    out = intake.write_status(conn, vault.raw_dir).read_text()
    assert "## 00_inbox — 0 files waiting" in out and "ACTION" not in out
    (vault.raw_dir / intake.NEEDS_ADAPTER).mkdir()
    (vault.raw_dir / intake.NEEDS_ADAPTER / "x.json").write_text("[]")
    out = intake.write_status(conn, vault.raw_dir).read_text()
    assert "20_needs_adapter — 1 items  ⚠ ACTION" in out

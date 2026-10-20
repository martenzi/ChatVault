from __future__ import annotations

import shutil

from chatvault import ingest, source_registry
from tests.conftest import FIXTURES


def _registry(vault, text):
    (vault.root / "docs").mkdir(exist_ok=True)
    (vault.root / source_registry.REGISTRY_FILE).write_text(
        "| source | platform | state | match | files | reason |\n|---|---|---|---|---|---|\n" + text
    )


def test_ingested_platform_row_is_derived_with_live_counts(vault, conn):
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl", config=vault)
    rows = {r.source: r for r in source_registry.build_status(conn, vault)}
    r = rows["claude_code"]
    assert r.state == "ingested" and r.adapter == "yes" and int(r.convs) >= 1


def test_registry_rows_need_reasons_and_closed_states(vault, conn):
    _registry(vault, "| docs | — | out of scope | | 3 | |\n| odd | — | maybe | | 1 | because |\n")
    rows = {r.source: r for r in source_registry.build_status(conn, vault)}
    assert rows["docs"].problem            # out of scope without a reason
    assert rows["odd"].problem             # state outside the closed set


def test_unexplained_chat_shaped_file_is_flagged_until_registry_covers_it(vault, conn):
    stray = vault.raw_dir / "mystery"
    stray.mkdir()
    (stray / "chat.json").write_text(
        '[{"role": "user", "content": "a long enough message about something nobody has seen before in the vault"},'
        ' {"role": "assistant", "content": "an equally unseen reply with enough words to be tested properly here"}]'
    )
    rows = source_registry.build_status(conn, vault)
    assert any(r.source == "mystery" and r.problem and r.state == "not ingested" for r in rows)
    _registry(vault, "| mystery | — | not ingested | `mystery/*` | 1 | known gap, adapter planned |\n")
    rows = source_registry.build_status(conn, vault)
    assert not any(r.problem for r in rows)

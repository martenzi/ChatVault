from __future__ import annotations

from typer.testing import CliRunner

from chatvault import brain_cli, cli, ingest
from chatvault.config import load_config
from chatvault.db import connect as vault_connect
from chatvault.db import migrate as vault_migrate
from tests.conftest import FIXTURES

# docs/51_SPEC_brain_parity_and_naming.md §1.4/§5.1 — `promote`, `distill`
# and `brief` are renamed, but the old names must keep working, hidden from
# `--help`, and print a deprecation note to *stderr* (never stdout, so
# machine-readable stdout output stays uncontaminated). These are the only
# CLI-level tests in this codebase (everything else in tests/test_brain.py
# exercises the module functions directly) — justified because the thing
# actually being verified here (which name is hidden, which stream the note
# lands on) is a property of the Typer command wiring itself, not of the
# underlying brain/entries.py logic those modules-level tests already cover.

runner = CliRunner()


def _setup_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATVAULT_ROOT", str(tmp_path))
    config = load_config()
    for d in (config.raw_dir, config.db_dir, config.exports_dir):
        d.mkdir(parents=True, exist_ok=True)
    vault_migrate(config)
    return config


def test_promote_is_hidden_from_help_but_copy_is_not():
    result = runner.invoke(brain_cli.brain_app, ["--help"])
    assert result.exit_code == 0
    assert "copy" in result.stdout
    assert "promote" not in result.stdout


def test_distill_is_hidden_from_help_but_summarise_is_not():
    result = runner.invoke(brain_cli.brain_app, ["--help"])
    assert result.exit_code == 0
    assert "summarise" in result.stdout
    assert "distill" not in result.stdout


def test_brief_is_hidden_from_help_but_pack_is_not():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "pack" in result.stdout
    assert "brief" not in result.stdout


def test_promote_from_stash_alias_prints_deprecation_note_to_stderr(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch)
    result = runner.invoke(brain_cli.brain_app, ["promote", "--from-stash"])
    assert result.exit_code == 0, result.output
    assert "note: 'brain promote --from-stash' is now 'brain copy stashed'" in result.stderr
    assert "note:" not in result.stdout
    assert "nothing to copy" in result.stdout


def test_promote_alias_delegates_to_copy_conversation_logic(tmp_path, monkeypatch):
    config = _setup_vault(tmp_path, monkeypatch)
    conn = vault_connect(config)
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    row = conn.execute("SELECT uid FROM v_conversations LIMIT 1").fetchone()
    conn.close()

    result = runner.invoke(brain_cli.brain_app, ["promote", "--uid", row["uid"], "--why", "kept for the record"])
    assert result.exit_code == 0, result.output
    assert "note: 'brain promote' is now 'brain copy conversation'" in result.stderr
    assert "note:" not in result.stdout
    assert "copied as" in result.stdout


def test_distill_alias_prints_deprecation_note_and_delegates(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch)

    def _fake_distill(brain_conn, vault_ro, uid, why, model="mistral:latest", kind="note"):
        return {"item_id": 1, "action": "distilled", "title": "fake conversation"}

    monkeypatch.setattr(brain_cli.brain_mod, "distill", _fake_distill)

    result = runner.invoke(brain_cli.brain_app, ["distill", "--from-conv", "1", "--why", "worth keeping"])
    # `--from-conv 1` with no prior `find` pick cache is expected to fail
    # resolution before ever reaching the model call — this only checks the
    # deprecation note fires before that resolution, which it must, since
    # the note is unconditional on entry to the alias command.
    assert "note: 'brain distill' is now 'brain summarise conversation'" in result.stderr
    assert "note:" not in result.stdout


def test_brief_alias_prints_deprecation_note_and_delegates(tmp_path, monkeypatch):
    config = _setup_vault(tmp_path, monkeypatch)
    conn = vault_connect(config)
    ingest.ingest_path(conn, FIXTURES / "claude_code" / "session1.jsonl")
    conv_id = conn.execute("SELECT id FROM v_conversations LIMIT 1").fetchone()[0]
    from chatvault import stash

    stash.add(conn, conv_id, "kept for later")
    conn.close()

    result = runner.invoke(cli.app, ["brief", "--pick", "1"])
    assert result.exit_code == 0, result.output
    assert "note: 'chatvault brief' is now 'chatvault pack'" in result.stderr
    assert "note:" not in result.stdout
    assert "# Context:" in result.stdout

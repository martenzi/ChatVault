from __future__ import annotations

import json
from datetime import date

from typer.testing import CliRunner

from chatvault import brain_cli
from chatvault.config import load_config

# Step 5 of docs/51_SPEC_brain_parity_and_naming.md §2.1's `export --tree` row,
# plus a smaller, non-spec destination-flexibility addition (--out/--dest/--dry-run)
# mirroring chatvault export's own pattern (src/chatvault/cli.py's `export`).
#
# CLI-level tests, same justification as tests/test_cli_deprecation.py: what's
# being verified here (which stream gets the body, whether a file lands on
# disk, the dated-folder naming) is a property of the Typer command wiring
# itself, not of chatvault.brain.stats's rendering logic (already covered in
# tests/test_brain.py's export_items tests).

runner = CliRunner()


def _setup_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATVAULT_ROOT", str(tmp_path))
    config = load_config()
    for d in (config.raw_dir, config.db_dir, config.exports_dir):
        d.mkdir(parents=True, exist_ok=True)
    return config


def test_default_export_still_prints_to_stdout_unchanged(tmp_path, monkeypatch):
    """The critical regression guard: no --out/--dest/--dry-run/--tree must
    behave exactly as it did before this feature existed."""
    _setup_brain(tmp_path, monkeypatch)
    runner.invoke(brain_cli.brain_app, ["add", "entry", "public fact", "--why", "kept for the record"])

    result = runner.invoke(brain_cli.brain_app, ["export"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert {r["title"] for r in data} == {"public fact"}
    # nothing written to disk anywhere under the vault root
    written_files = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix in (".json", ".md", ".yaml")]
    assert written_files == []


def test_dry_run_alone_prints_planned_path_and_writes_nothing(tmp_path, monkeypatch):
    _setup_brain(tmp_path, monkeypatch)
    runner.invoke(brain_cli.brain_app, ["add", "entry", "public fact", "--why", "kept for the record"])

    result = runner.invoke(brain_cli.brain_app, ["export", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would write" in result.stdout
    assert "brain-export.json" in result.stdout
    # export body must NOT have been printed
    assert "public fact" not in result.stdout


def test_out_writes_a_single_combined_file(tmp_path, monkeypatch):
    _setup_brain(tmp_path, monkeypatch)
    runner.invoke(brain_cli.brain_app, ["add", "entry", "public fact", "--why", "kept for the record"])
    runner.invoke(brain_cli.brain_app, ["add", "entry", "another fact", "--why", "also kept"])

    target = tmp_path / "out" / "combined.json"
    result = runner.invoke(brain_cli.brain_app, ["export", "--out", str(target)])
    assert result.exit_code == 0, result.output
    assert target.exists()
    data = json.loads(target.read_text())
    assert {r["title"] for r in data} == {"public fact", "another fact"}
    # single file, not one per item
    assert len(list(target.parent.iterdir())) == 1


def test_dest_here_writes_dated_folder_with_matching_extension(tmp_path, monkeypatch):
    _setup_brain(tmp_path, monkeypatch)
    runner.invoke(brain_cli.brain_app, ["add", "entry", "public fact", "--why", "kept for the record"])

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = runner.invoke(brain_cli.brain_app, ["export", "--format", "markdown", "--dest", "here"])
    assert result.exit_code == 0, result.output
    dated = workdir / f"Chatvault_export_{date.today().isoformat()}"
    expected = dated / "brain-export.md"
    assert expected.exists()
    assert "public fact" in expected.read_text()


def test_tree_writes_nested_folders_matching_tag_hierarchy(tmp_path, monkeypatch):
    _setup_brain(tmp_path, monkeypatch)
    runner.invoke(brain_cli.brain_app, ["add", "entry", "npm note", "--why", "kept", "--ref", "npm-note"])
    runner.invoke(brain_cli.brain_app, ["tag", "entry", "npm-note", "tech/macos/npm"])
    runner.invoke(brain_cli.brain_app, ["add", "entry", "docker note", "--why", "kept", "--ref", "docker-note"])
    runner.invoke(brain_cli.brain_app, ["tag", "entry", "docker-note", "tech/containers/docker"])
    runner.invoke(brain_cli.brain_app, ["add", "entry", "unrelated note", "--why", "kept", "--ref", "unrelated-note"])
    runner.invoke(brain_cli.brain_app, ["tag", "entry", "unrelated-note", "finance/taxes"])

    out_dir = tmp_path / "tree-out"
    result = runner.invoke(brain_cli.brain_app, ["export", "--tree", "tech/**", "--out", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "wrote 2 file(s)" in result.stdout

    npm_files = list((out_dir / "macos" / "npm").glob("*.json"))
    docker_files = list((out_dir / "containers" / "docker").glob("*.json"))
    assert len(npm_files) == 1
    assert len(docker_files) == 1
    assert json.loads(npm_files[0].read_text())[0]["title"] == "npm note"
    assert json.loads(docker_files[0].read_text())[0]["title"] == "docker note"
    # the unrelated, differently-tagged entry must not appear anywhere
    assert not list(out_dir.rglob("*unrelated*"))


def test_tree_dry_run_lists_paths_without_writing(tmp_path, monkeypatch):
    _setup_brain(tmp_path, monkeypatch)
    runner.invoke(brain_cli.brain_app, ["add", "entry", "npm note", "--why", "kept", "--ref", "npm-note"])
    runner.invoke(brain_cli.brain_app, ["tag", "entry", "npm-note", "tech/macos/npm"])

    out_dir = tmp_path / "tree-out"
    result = runner.invoke(brain_cli.brain_app, ["export", "--tree", "tech/**", "--out", str(out_dir), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would write" in result.stdout
    assert not out_dir.exists()

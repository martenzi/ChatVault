"""docs/specs/77: remote live sources. "Remote" is this machine reached through a
fake `ssh` on PATH (rsync is real and uses it), with HOME pointed at a fake
remote home. The alias `deadhost` always refuses the connection."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatvault import remote
from chatvault.cli import app
from chatvault.config import Config
from chatvault.db import connect

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")

FAKE_SSH = """#!/bin/bash
while [ "$1" = "-o" ]; do shift 2; done
host="$1"; shift
echo "$host $*" >> "${FAKE_SSH_LOG:-/dev/null}"
if [ "$host" = deadhost ]; then echo "ssh: connect to host deadhost port 22: Connection refused" >&2; exit 255; fi
case "$*" in *'python3 "$HOME"'*) if [ -n "$FAKE_NO_PYTHON" ]; then echo "bash: python3: command not found" >&2; exit 127; fi;; esac
cd "$HOME"
exec bash -c "$*"
"""

# Writes what the real devin_cli extractor writes: one JSONL under a dated dir.
FAKE_EXTRACTOR = """#!/usr/bin/env python3
import argparse, json, shutil, sys
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--out-dir", type=Path); ap.add_argument("--host"); ap.add_argument("--src")
a = ap.parse_args()
if "FAIL" in (a.src or ""):
    print("boom: extractor crashed", file=sys.stderr); sys.exit(2)
out = a.out_dir / "2026-10-27"; out.mkdir(parents=True)
with open(out / "sessions.jsonl", "w") as fh:
    for line in open("%s"):
        rec = json.loads(line)
        rec["host"] = a.host
        fh.write(json.dumps(rec) + "\\n")
"""


@pytest.fixture()
def env(vault: Config, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "remote_home"
    home.mkdir()
    ssh_log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_SSH_LOG", str(ssh_log))
    monkeypatch.setenv("CHATVAULT_ROOT", str(vault.root))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CHATVAULT_ROLE", raising=False)
    # a local registry that is valid and empty: local ingest has nothing to do
    local = tmp_path / "local_tree"
    local.mkdir()
    c = connect(vault)
    c.execute("INSERT INTO live_roots (source, path, created_at) VALUES ('claude_code', ?, '2026-01-01')", (str(local),))
    c.commit()
    c.close()
    return type("Env", (), {"vault": vault, "home": home, "ssh_log": ssh_log, "bin": bin_dir, "tmp": tmp_path})


def _claude_session(home: Path) -> None:
    d = home / ".claude" / "projects" / "-home-x"
    d.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude_code" / "session1.jsonl", d / "session1.jsonl")


def _extractor(env, name="fake_devin.py") -> Path:
    p = env.tmp / name
    p.write_text(FAKE_EXTRACTOR % (FIXTURES / "devin_cli" / "real_session.jsonl"))
    return p


def _register_devin(env, store: str, host: str = "fedora") -> None:
    c = connect(env.vault)
    c.execute(
        "INSERT INTO live_roots (source, live_store, extract_cmd, stage_dir, created_at, host) VALUES ('devin_cli', ?, ?, ?, '2026-01-01', ?)",
        (json.dumps([store]), str(_extractor(env)), f"raw/00_inbox/devin_cli@{host}", host),
    )
    c.commit()
    c.close()


def _register_claude(env, host: str = "fedora", path: str = "~/.claude/projects") -> None:
    c = connect(env.vault)
    c.execute("INSERT INTO live_roots (source, path, created_at, host) VALUES ('claude_code', ?, '2026-01-01', ?)", (path, host))
    c.commit()
    c.close()


def _run(*args):
    return CliRunner().invoke(app, list(args))


def _count(env, sql: str) -> int:
    c = connect(env.vault)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


# ───────────────────────── provenance ─────────────────────────


def test_host_for_path():
    from chatvault.ingest import host_for_path

    assert host_for_path(Path("/v/raw/live_mirror/fedora/claude_code/projects/a.jsonl")) == "fedora"
    assert host_for_path(Path("/v/raw/10_ingested/devin_cli@fedora/2026/s.jsonl")) == "fedora"
    assert host_for_path(Path("/v/raw/10_ingested/devin_cli/2026/s.jsonl")) is None


def test_migration_adds_host_columns(vault, conn):
    assert {"host", "last_pulled_at"} <= {r[1] for r in conn.execute("PRAGMA table_info(live_roots)")}
    assert "host" in {r[1] for r in conn.execute("PRAGMA table_info(source_files)")}


def test_same_path_registers_once_per_machine(vault, conn):
    for host in (None, "fedora"):
        conn.execute("INSERT INTO live_roots (source, path, created_at, host) VALUES ('claude_code', '~/.claude/projects', 'x', ?)", (host,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO live_roots (source, path, created_at, host) VALUES ('claude_code', '~/.claude/projects', 'x', 'fedora')")


# ───────────────────────── discovery / registry ─────────────────────────


def test_init_host_registers_only_what_exists(env):
    _claude_session(env.home)
    (env.home / ".local/share/devin/cli").mkdir(parents=True)
    (env.home / ".local/share/devin/cli/sessions.db").write_bytes(b"")
    c = connect(env.vault)
    registered, missing, err = remote.init_host(c, env.vault, "fedora")
    assert err is None
    assert {r.split(":")[0] for r in registered} == {"claude_code", "devin_cli"}
    assert {"cursor", "devin_desktop_acp"} <= set(missing)
    # idempotent
    assert remote.init_host(c, env.vault, "fedora")[0] == []
    c.close()


def test_discovery_honours_xdg_config_home_and_flatpak(env, monkeypatch):
    xdg = env.home / "customcfg"
    (xdg / "Cursor/User/globalStorage").mkdir(parents=True)
    (xdg / "Cursor/User/globalStorage/state.vscdb").write_bytes(b"")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    fp = env.home / ".var/app/com.example.Devin/config/Devin/User/acp-messages"
    fp.mkdir(parents=True)
    (fp / "a.db").write_bytes(b"")
    c = connect(env.vault)
    registered, _missing, err = remote.init_host(c, env.vault, "fedora")
    assert err is None
    text = " ".join(registered)
    assert "customcfg/Cursor/User/globalStorage/state.vscdb" in text
    assert ".var/app/*evin*/config/Devin/User/acp-messages/*.db" in text
    c.close()


def test_set_path_survives_init(env):
    _claude_session(env.home)
    c = connect(env.vault)
    remote.init_host(c, env.vault, "fedora")
    remote.set_path(c, "fedora", "claude_code", "~/elsewhere")
    remote.init_host(c, env.vault, "fedora")
    assert c.execute("SELECT path FROM live_roots WHERE host='fedora' AND source='claude_code'").fetchone()[0] == "~/elsewhere"
    c.close()


# ───────────────────────── pulls ─────────────────────────


def test_claude_code_pull_lands_with_host_and_tag_and_second_run_is_quiet(env):
    _claude_session(env.home)
    _register_claude(env)
    r = _run("ingest", "--live", "--remote", "--no-embed")
    assert r.exit_code == 0, r.output
    assert "Remote: fedora (local ingest unaffected)" in r.output
    assert _count(env, "SELECT COUNT(*) FROM source_files WHERE host='fedora'") >= 1
    assert _count(env, "SELECT COUNT(*) FROM source_files WHERE host='mac'") == 0
    assert _count(env, "SELECT COUNT(*) FROM tag_links tl JOIN tags t ON t.id=tl.tag_id WHERE t.name='host/fedora'") >= 1
    n_conv = _count(env, "SELECT COUNT(*) FROM conversations")
    assert n_conv >= 1
    assert list((env.vault.logs_dir).glob("remote-fedora-*.txt"))

    r2 = _run("ingest", "--live", "--remote", "--no-embed")
    assert r2.exit_code == 0, r2.output
    assert _count(env, "SELECT COUNT(*) FROM conversations") == n_conv


def test_ingest_live_without_remote_makes_no_ssh_call(env):
    _claude_session(env.home)
    _register_claude(env)
    r = _run("ingest", "--live", "--no-embed")
    assert r.exit_code == 0, r.output
    assert not env.ssh_log.exists()
    assert "Remote:" not in r.output


def test_devin_cli_pull_namespaces_ids_and_shows_host(env):
    store = env.home / "sessions.db"
    store.write_bytes(b"")
    _register_devin(env, str(store))
    r = _run("ingest", "--live", "--remote", "--no-embed")
    assert r.exit_code == 0, r.output
    c = connect(env.vault)
    ids = [row[0] for row in c.execute("SELECT native_id FROM conversations WHERE platform='devin_cli'")]
    c.close()
    assert ids and all(i.startswith("fedora:") for i in ids)
    found = _run("find")
    assert "devin_cli@fedora" in found.output.replace("\n", "")


def test_export_carries_host(env):
    store = env.home / "sessions.db"
    store.write_bytes(b"")
    _register_devin(env, str(store))
    assert _run("ingest", "--live", "--remote", "--no-embed").exit_code == 0
    c = connect(env.vault)
    from chatvault import export

    row = c.execute("SELECT * FROM v_conversations WHERE platform='devin_cli'").fetchone()
    assert "- host: fedora" in export.render(c, row, "default")
    c.close()


# ───────────────────────── fault injection (§9.9) ─────────────────────────


def test_unreachable_host_is_reported_and_local_ingest_is_unaffected(env):
    _register_claude(env, host="deadhost")
    r = _run("ingest", "--live", "--remote", "--no-embed")
    assert r.exit_code == 0, r.output
    out = " ".join(r.output.split())
    assert "UNREACHABLE" in out and "last successful pull" in out
    assert "re-run without --remote" in out


def test_remote_missing_store(env):
    _register_claude(env, path="~/no/such/dir")
    r = _run("ingest", "--live", "--remote", "--no-embed")
    out = " ".join(r.output.split())
    assert r.exit_code == 0, r.output
    assert "REMOTE-MISSING" in out and "set-path --host fedora" in out
    assert "live-roots disable --host fedora" in out


def test_extractor_failure_ingests_nothing_and_does_not_stop_other_sources(env):
    _claude_session(env.home)
    _register_claude(env)
    _register_devin(env, str(env.home / "FAIL.db"))
    (env.home / "FAIL.db").write_bytes(b"")
    r = _run("ingest", "--live", "--remote", "--no-embed")
    assert r.exit_code == 0, r.output
    assert "REMOTE-ERROR" in r.output and "extractor failed (exit 2)" in " ".join(r.output.split())
    assert _count(env, "SELECT COUNT(*) FROM conversations WHERE platform='devin_cli'") == 0
    assert _count(env, "SELECT COUNT(*) FROM conversations WHERE platform='claude_code'") >= 1
    assert not list((env.vault.raw_dir / "00_inbox").glob("devin_cli@fedora/**/*.jsonl"))


def test_missing_python3_on_remote(env, monkeypatch):
    store = env.home / "sessions.db"
    store.write_bytes(b"")
    _register_devin(env, str(store))
    monkeypatch.setenv("FAKE_NO_PYTHON", "1")
    r = _run("ingest", "--live", "--remote", "--no-embed")
    assert r.exit_code == 0, r.output
    assert "python3 is not available" in " ".join(r.output.split()), "\n".join(r.output.splitlines()[-12:])


def test_rsync_killed_mid_transfer_ingests_nothing(env):
    _claude_session(env.home)
    _register_claude(env)
    killer = env.bin / "rsync"
    killer.write_text("#!/bin/bash\necho 'rsync error: unexplained error (code 255)' >&2\nexit 255\n")
    killer.chmod(killer.stat().st_mode | stat.S_IEXEC)
    r = _run("ingest", "--live", "--remote", "--no-embed")
    assert r.exit_code == 0, r.output
    assert "REMOTE-ERROR" in r.output and "nothing was ingested" in " ".join(r.output.split())
    assert _count(env, "SELECT COUNT(*) FROM conversations") == 0


# ───────────────────────── replica guard (§7) ─────────────────────────


def test_replica_refuses_ingest_and_leaves_rows_alone(env, monkeypatch):
    before = _count(env, "SELECT COUNT(*) FROM source_files")
    monkeypatch.setenv("CHATVAULT_ROLE", "replica")
    for args in (["ingest", "--no-embed"], ["ingest", "--live", "--no-embed"]):
        r = _run(*args)
        assert r.exit_code == 1
        assert "read-only replica" in r.output
    monkeypatch.delenv("CHATVAULT_ROLE")
    assert _count(env, "SELECT COUNT(*) FROM source_files") == before

"""Remote live sources: the Mac pulls from another machine over ssh.

docs/specs/77_SPEC_remote_live_sources.md. The Mac is the only writer; a
remote host is a source, never a peer. Everything here is Mac-initiated and
nothing in this module is imported on the local-only path of `ingest --live`
(no ssh call at all unless --remote / --host is given).

Fault containment (spec §2): no function that runs per source raises. Each
failure becomes a `RemoteResult` with a state, a plain-words cause and a fix,
and the raw ssh/rsync/extractor output goes to logs/remote-<host>-<ts>.txt.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from chatvault import live_roots as lr
from chatvault.config import Config

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
REMOTE_CACHE = "~/.cache/chatvault-remote"
LIVE_MIRROR = "live_mirror"

OK, STALE, UNREACHABLE, REMOTE_MISSING, REMOTE_ERROR = "ok", "STALE", "UNREACHABLE", "REMOTE-MISSING", "REMOTE-ERROR"

# Candidate stores, tried in order (spec §3). {CFG} is $XDG_CONFIG_HOME or
# ~/.config, resolved on the remote in a login shell; the Flatpak globs cover
# sandboxed installs.
_FP = "~/.var/app/*{app}*/config"
REMOTE_DEFAULTS: list[dict] = [
    {"source": "claude_code", "path": "~/.claude/projects"},
    {"source": "devin_cli", "extract_cmd": "scripts/extract_devin_cli.py",
     "candidates": [["~/.local/share/devin/cli/sessions.db"]]},
    {"source": "devin_desktop_acp", "extract_cmd": "scripts/extract_devin_desktop_acp.py",
     "candidates": [["{CFG}/Devin/User/acp-messages/*.db"],
                    ["~/.config/Devin/User/acp-messages/*.db"],
                    [_FP.format(app="evin") + "/Devin/User/acp-messages/*.db"]]},
    {"source": "cursor", "extract_cmd": "scripts/extract_cursor.py",
     "candidates": [[f"{b}/Cursor/User/globalStorage/state.vscdb", f"{b}/Cursor/User/workspaceStorage/*/state.vscdb"]
                    for b in ("{CFG}", "~/.config", _FP.format(app="ursor"))]},
    {"source": "antigravity", "extract_cmd": "scripts/extract_antigravity.py",
     "candidates": [["~/.gemini/antigravity-ide/conversations", "~/.gemini/antigravity-cli/conversations"]]},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── ssh plumbing ─────────────────────────────


def home_quote(path: str) -> str:
    """Shell-quote a remote path, keeping a leading `~` expandable remotely."""
    if path == "~" or path.startswith("~/"):
        return '"$HOME"' + (shlex.quote(path[1:]) if len(path) > 1 else "")
    return shlex.quote(path)


def home_glob(path: str) -> str:
    """Like home_quote, but `* ? [ ]` stay live so the remote shell globs."""
    head, rest = ("", path)
    if path == "~" or path.startswith("~/"):
        head, rest = '"$HOME"', path[1:]
    parts = re.split(r"([*?\[\]])", rest)
    return head + "".join(p if i % 2 else (shlex.quote(p) if p else "") for i, p in enumerate(parts))


class HostLog:
    """logs/remote-<host>-<timestamp>.txt: raw output of every remote command."""

    def __init__(self, config: Config, host: str) -> None:
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = config.logs_dir / f"remote-{host}-{stamp}.txt"

    def write(self, title: str, argv: list[str], rc: object, out: str, err: str) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(argv)}\n[{title}] exit={rc}\n")
            if out.strip():
                fh.write(f"--- stdout\n{out}\n")
            if err.strip():
                fh.write(f"--- stderr\n{err}\n")
            fh.write("\n")


@dataclass
class Run:
    rc: int          # 255 ssh failure; -1 timeout; -2 could not start
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _run(argv: list[str], log: HostLog, title: str, timeout: int) -> Run:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
        r = Run(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as exc:
        r = Run(-1, (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                f"timed out after {timeout}s")
    except OSError as exc:
        r = Run(-2, "", f"could not start {argv[0]}: {exc}")
    log.write(title, argv, r.rc, r.out, r.err)
    return r


def ssh(host: str, script: str, log: HostLog, title: str, timeout: int = 60) -> Run:
    """Run `script` in a login shell on the remote (so $XDG_CONFIG_HOME etc. are set)."""
    return _run(["ssh", *SSH_OPTS, host, "bash -lc " + shlex.quote(script)], log, title, timeout)


def rsync(args: list[str], log: HostLog, title: str, timeout: int = 900) -> Run:
    ssh_cmd = "ssh " + " ".join(SSH_OPTS)
    return _run(["rsync", "-a", "--timeout=60", "-e", ssh_cmd, *args], log, title, timeout)


def probe(host: str, log: HostLog) -> tuple[bool, str]:
    r = _run(["ssh", *SSH_OPTS, host, "true"], log, "probe", timeout=20)
    if r.ok:
        return True, ""
    return False, (r.err.strip().splitlines() or [f"exit {r.rc}"])[-1]


# ───────────────────────────── results ─────────────────────────────


@dataclass
class RemoteResult:
    host: str
    source: str
    state: str
    cause: str = ""
    fix: str = ""
    detail: str = ""


@dataclass
class HostReport:
    host: str
    log_path: Path | None = None
    last_pull: str | None = None
    results: list[RemoteResult] = field(default_factory=list)


def _fixes(host: str, source: str, kind: str) -> str:
    return {
        "missing": f"`chatvault live-roots set-path --host {host} {source} <path>` or "
                   f"`chatvault live-roots disable --host {host} {source} --reason ...`",
        "error": f"see the log; or re-run without --remote (local ingest is unaffected)",
        "unreachable": f"check `ssh {host} true`; or re-run without --remote",
    }[kind]


# ───────────────────────────── discovery / init ─────────────────────────────


def discover(host: str, log: HostLog) -> tuple[dict[str, list[str]], str, str | None]:
    """One login-shell call on the remote. Returns ({source: [live_store entries]},
    remote $HOME, error). Candidate sets are tried in order; the first set with
    an existing store wins, and only its entries that exist are registered."""
    lines = ['shopt -s nullglob', 'CFG="${XDG_CONFIG_HOME:-$HOME/.config}"', 'echo "HOME=$HOME"', 'echo "CFG=$CFG"']
    for d in REMOTE_DEFAULTS:
        sets = [[d["path"]]] if "path" in d else d["candidates"]
        for si, entries in enumerate(sets):
            for ei, e in enumerate(entries):
                glob = home_glob(e.replace("{CFG}", "CFGPLACEHOLDER")).replace("CFGPLACEHOLDER", '"$CFG"')
                lines.append(f'for f in {glob}; do [ -e "$f" ] || continue; echo "FOUND\t{d["source"]}\t{si}\t{ei}"; break; done')
    r = ssh(host, "\n".join(lines), log, "discover", timeout=60)
    if not r.ok:
        return {}, "", (r.err.strip().splitlines() or [f"exit {r.rc}"])[-1]
    home = cfg = ""
    found: dict[str, dict[int, set[int]]] = {}
    for ln in r.out.splitlines():
        if ln.startswith("HOME="):
            home = ln[5:]
        elif ln.startswith("CFG="):
            cfg = ln[4:]
        elif ln.startswith("FOUND\t"):
            _, source, si, ei = ln.split("\t")
            found.setdefault(source, {}).setdefault(int(si), set()).add(int(ei))
    out: dict[str, list[str]] = {}
    for d in REMOTE_DEFAULTS:
        by_set = found.get(d["source"])
        if not by_set:
            continue
        si = min(by_set)
        sets = [[d["path"]]] if "path" in d else d["candidates"]
        out[d["source"]] = [_tilde(sets[si][ei].replace("{CFG}", cfg), home) for ei in sorted(by_set[si])]
    return out, home, None


def _tilde(path: str, home: str) -> str:
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


def init_host(conn: sqlite3.Connection, config: Config, host: str) -> tuple[list[str], list[str], str | None]:
    """Register what exists on `host`. Idempotent; never edits an existing row.
    Returns (registered, not_found, error)."""
    log = HostLog(config, host)
    ok, why = probe(host, log)
    if not ok:
        return [], [], f"{host} is unreachable: {why}"
    found, _home, err = discover(host, log)
    if err:
        return [], [], f"discovery on {host} failed: {err} (log: {log.path})"
    registered, missing = [], []
    for d in REMOTE_DEFAULTS:
        src = d["source"]
        if src not in found:
            missing.append(src)
            continue
        if d.get("extract_cmd"):
            exists = conn.execute("SELECT 1 FROM live_roots WHERE host = ? AND source = ? AND extract_cmd IS NOT NULL", (host, src)).fetchone()
        else:
            exists = conn.execute("SELECT 1 FROM live_roots WHERE host = ? AND path = ?", (host, d["path"])).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO live_roots (source, path, live_store, extract_cmd, stage_dir, created_at, host) VALUES (?,?,?,?,?,?,?)",
            (src, d.get("path"), json.dumps(found[src]) if d.get("extract_cmd") else None, d.get("extract_cmd"),
             f"raw/00_inbox/{src}@{host}" if d.get("extract_cmd") else None, _now(), host),
        )
        registered.append(f"{src}: {', '.join(found[src])}")
    conn.commit()
    return registered, missing, None


def set_path(conn: sqlite3.Connection, host: str, source: str, value: str) -> str:
    """Override a remote source's location (comma-separated for several). Creates
    the row from the default template if the source is not registered yet."""
    entries = [v.strip() for v in value.split(",") if v.strip()]
    tmpl = next((d for d in REMOTE_DEFAULTS if d["source"] == source), None)
    if tmpl is None:
        raise ValueError(f"unknown source {source!r}")
    extracted = bool(tmpl.get("extract_cmd"))
    row = conn.execute(
        "SELECT id FROM live_roots WHERE host = ? AND source = ?" + (" AND extract_cmd IS NOT NULL" if extracted else ""),
        (host, source),
    ).fetchone()
    store = json.dumps(entries) if extracted else None
    path = None if extracted else entries[0]
    if row:
        conn.execute("UPDATE live_roots SET live_store = ?, path = ?, active = 1, note = NULL WHERE id = ?", (store, path, row["id"]))
        verb = "updated"
    else:
        conn.execute(
            "INSERT INTO live_roots (source, path, live_store, extract_cmd, stage_dir, created_at, host) VALUES (?,?,?,?,?,?,?)",
            (source, path, store, tmpl.get("extract_cmd"), f"raw/00_inbox/{source}@{host}" if extracted else None, _now(), host),
        )
        verb = "registered"
    conn.commit()
    return f"{verb} {source} on {host}: {', '.join(entries)}"


# ───────────────────────────── status ─────────────────────────────


def hosts(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT host FROM live_roots WHERE host IS NOT NULL AND active = 1 ORDER BY host")]


def host_rows(conn: sqlite3.Connection, host: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM live_roots WHERE host = ? AND active = 1 ORDER BY source, path", (host,)).fetchall()


def _entries(row: sqlite3.Row) -> list[str]:
    return json.loads(row["live_store"]) if row["live_store"] else [row["path"]]


_STAT_PRELUDE = (
    "shopt -s nullglob\n"
    "if stat -c %Y / >/dev/null 2>&1; then mt() { stat -c %Y \"$@\" 2>/dev/null; }; "
    "else mt() { stat -f %m \"$@\" 2>/dev/null; }; fi\n"
    "export -f mt\n"
    'echo "NOW $(date +%s)"\n'
)


def remote_status(host: str, rows: list[sqlite3.Row], log: HostLog) -> tuple[int | None, dict[int, tuple[int, int | None]], str | None]:
    """One ssh call: the remote clock and, per registered row, how many store
    paths exist and the newest mtime (remote epoch). Age is computed against the
    remote clock in the same call, so clock skew cannot fake STALE/ok."""
    lines = [_STAT_PRELUDE]
    for r in rows:
        for j, e in enumerate(_entries(r)):
            g = home_glob(e)
            lines.append(
                f'n=$(for f in {g}; do [ -e "$f" ] && echo x; done | wc -l)\n'
                f't=$( {{ for f in {g}; do if [ -d "$f" ]; then find "$f" -type f -exec bash -c \'mt "$@"\' _ {{}} +; '
                f'else mt "$f" "$f-wal"; fi; done; }} | sort -n | tail -1 )\n'
                f'echo "ENTRY {r["id"]} {j} $n ${{t:-0}}"'
            )
    res = ssh(host, "\n".join(lines), log, "status", timeout=60)
    if not res.ok:
        return None, {}, (res.err.strip().splitlines() or [f"exit {res.rc}"])[-1]
    now, per_row = None, {}
    for ln in res.out.splitlines():
        p = ln.split()
        if p[:1] == ["NOW"] and len(p) == 2:
            now = int(p[1])
        elif p[:1] == ["ENTRY"] and len(p) == 5:
            rid, n, t = int(p[1]), int(p[3]), int(float(p[4]))
            cnt, newest = per_row.get(rid, (0, None))
            per_row[rid] = (cnt + n, max(newest or 0, t) or None)
    return now, per_row, None


# ───────────────────────────── pulls ─────────────────────────────


def _extractor_args(row: sqlite3.Row) -> list[str]:
    ent = _entries(row)
    src = row["source"]
    parent = lambda p: p.rsplit("/", 1)[0]  # noqa: E731
    if src == "devin_cli":
        return ["--src", ent[0]]
    if src == "devin_desktop_acp":
        return ["--src-dir", parent(ent[0])]
    if src == "cursor":
        g = next((e for e in ent if "globalStorage" in e), None)
        w = next((e for e in ent if "workspaceStorage" in e), None)
        out = []
        if g:
            out += ["--global-storage", parent(g)]
        if w:
            out += ["--workspace-storage", parent(parent(w))]
        elif g:
            out += ["--workspace-storage", parent(g).rsplit("/", 1)[0] + "/workspaceStorage"]
        return out
    if src == "antigravity":
        out = []
        for e in ent:
            out += ["--ide-dir" if "ide" in e else "--cli-dir", e]
        return out
    return []


def pull_direct(conn, config: Config, host: str, row, log: HostLog) -> RemoteResult:
    src = row["source"]
    base = row["path"].rstrip("/")
    dest = config.raw_dir / LIVE_MIRROR / host / src / Path(base).name
    dest.mkdir(parents=True, exist_ok=True)
    r = rsync(
        ["--include=*/", "--include=*.jsonl", "--exclude=*", f"{host}:{home_quote_rsync(base)}/", f"{dest}/"],
        log, f"rsync {src}",
    )
    if not r.ok:
        why = (r.err.strip().splitlines() or [f"rsync exit {r.rc}"])[-1]
        return RemoteResult(host, src, REMOTE_ERROR, f"rsync did not finish cleanly ({why}); nothing was ingested",
                            _fixes(host, src, "error"))
    conn.execute("UPDATE live_roots SET last_pulled_at = ? WHERE id = ?", (_now(), row["id"]))
    conn.commit()
    return RemoteResult(host, src, OK, detail=str(dest))


def home_quote_rsync(path: str) -> str:
    """rsync's remote side expands nothing itself; `~/x` works as `x` relative to
    the remote home, so strip the tilde."""
    return path[2:] if path.startswith("~/") else path


def pull_extracted(conn, config: Config, host: str, row, log: HostLog) -> tuple[RemoteResult, Path | None]:
    """Run the extractor on the remote, rsync its output into a local temp dir,
    and only on a clean exit move it into raw/00_inbox/<source>@<host>/."""
    src = row["source"]
    script = lr.PROJECT_DIR / row["extract_cmd"]
    if not script.is_file():
        return RemoteResult(host, src, REMOTE_ERROR, f"extractor not found locally: {script}", _fixes(host, src, "error")), None

    mk = ssh(host, f"mkdir -p {home_quote(REMOTE_CACHE)} && mktemp -d", log, "mktemp", timeout=30)
    tmp = mk.out.strip().splitlines()[-1] if mk.ok and mk.out.strip() else ""
    if not tmp:
        return RemoteResult(host, src, REMOTE_ERROR, f"could not prepare {REMOTE_CACHE} on {host}: {(mk.err.strip().splitlines() or ['no output'])[-1]}",
                            _fixes(host, src, "error")), None
    try:
        up = rsync([str(script), f"{host}:{home_quote_rsync(REMOTE_CACHE)}/"], log, f"copy {script.name}", timeout=120)
        if not up.ok:
            return RemoteResult(host, src, REMOTE_ERROR, "could not copy the extractor to the remote", _fixes(host, src, "error")), None
        args = " ".join(["--out-dir", shlex.quote(tmp), "--host", shlex.quote(host)] + [home_quote(a) if a.startswith(("~", "/")) else shlex.quote(a) for a in _extractor_args(row)])
        ex = ssh(host, f"python3 {home_quote(REMOTE_CACHE)}/{script.name} {args}", log, f"extract {src}", timeout=600)
        if not ex.ok:
            if ex.rc == 127 or "python3: command not found" in ex.err or "python3: not found" in ex.err:
                cause = f"python3 is not available on {host}"
            elif "not found" in ex.err and ex.rc == 1:
                return RemoteResult(host, src, REMOTE_MISSING, (ex.err.strip().splitlines() or ["store not found"])[-1], _fixes(host, src, "missing")), None
            else:
                cause = f"extractor failed (exit {ex.rc}): {(ex.err.strip().splitlines() or ['no message'])[-1]}"
            return RemoteResult(host, src, REMOTE_ERROR, cause, _fixes(host, src, "error")), None
        stage = Path(tempfile.mkdtemp(prefix=f"pull-{src}-", dir=config.raw_dir))
        try:
            dl = rsync([f"{host}:{home_quote_rsync(tmp)}/" if not tmp.startswith("/") else f"{host}:{tmp}/", f"{stage}/"], log, f"fetch {src}")
            if not dl.ok:
                shutil.rmtree(stage, ignore_errors=True)
                return RemoteResult(host, src, REMOTE_ERROR,
                                    f"rsync did not finish cleanly ({(dl.err.strip().splitlines() or [f'exit {dl.rc}'])[-1]}); nothing was ingested",
                                    _fixes(host, src, "error")), None
            inbox = config.raw_dir / "00_inbox" / f"{src}@{host}"
            inbox.mkdir(parents=True, exist_ok=True)
            for item in sorted(stage.iterdir()):
                target = inbox / item.name
                if target.exists():
                    shutil.copytree(item, target, dirs_exist_ok=True) if item.is_dir() else shutil.copy2(item, target)
                else:
                    shutil.move(str(item), str(target))
            shutil.rmtree(stage, ignore_errors=True)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        conn.execute("UPDATE live_roots SET last_extracted_at = ?, last_pulled_at = ? WHERE id = ?", (_now(), _now(), row["id"]))
        conn.commit()
        return RemoteResult(host, src, OK, detail=(ex.out.strip().splitlines() or [""])[-1]), inbox
    finally:
        ssh(host, f"rm -rf {shlex.quote(tmp)}", log, "cleanup", timeout=30)


# ───────────────────────────── orchestration ─────────────────────────────


def pull_host(
    conn: sqlite3.Connection,
    config: Config,
    host: str,
    *,
    dry_run: bool = False,
    ingest_direct: Callable[[Path], object] | None = None,
    ingest_inbox: Callable[[str], list] | None = None,
) -> HostReport:
    """Pull every registered source of `host`. Never raises: each source is
    independent and every failure is a result row (spec §2)."""
    rep = HostReport(host)
    rows = host_rows(conn, host)
    log = HostLog(config, host)
    rep.log_path = log.path
    rep.last_pull = conn.execute("SELECT MAX(last_pulled_at) FROM live_roots WHERE host = ?", (host,)).fetchone()[0]
    try:
        ok, why = probe(host, log)
        if not ok:
            rep.results = [RemoteResult(host, r["source"], UNREACHABLE, f"{host} did not answer ({why})", _fixes(host, r["source"], "unreachable")) for r in rows]
            return rep
        now, per_row, err = remote_status(host, rows, log)
        if err:
            rep.results = [RemoteResult(host, r["source"], REMOTE_ERROR, f"could not read the remote stores: {err}", _fixes(host, r["source"], "error")) for r in rows]
            return rep
        mac_now = datetime.now(timezone.utc).timestamp()
        for r in rows:
            src = r["source"]
            try:
                count, newest = per_row.get(r["id"], (0, None))
                if count == 0:
                    rep.results.append(RemoteResult(host, src, REMOTE_MISSING, f"no store at {', '.join(_entries(r))} on {host}", _fixes(host, src, "missing")))
                    continue
                stale = True
                lp = lr._ts(r["last_pulled_at"])
                if lp is not None and newest and now:
                    stale = (now - newest) < (mac_now - lp)
                if dry_run:
                    rep.results.append(RemoteResult(host, src, STALE if stale else OK, detail="dry run — not pulled"))
                    continue
                if r["extract_cmd"]:
                    res, inbox = pull_extracted(conn, config, host, r, log)
                    if res.state == OK and ingest_inbox is not None:
                        try:
                            items = ingest_inbox(f"{src}@{host}")
                            bad = [i for i in items if getattr(i, "action", "") == "failed"]
                            res.detail = f"{len(items)} file(s) filed" + (f", {len(bad)} failed" if bad else "")
                            if bad:
                                res.state, res.cause, res.fix = REMOTE_ERROR, "ingest of the pulled snapshot failed", _fixes(host, src, "error")
                        except Exception as exc:  # noqa: BLE001
                            log.write("ingest", [src], "exception", "", repr(exc))
                            res.state, res.cause, res.fix = REMOTE_ERROR, f"ingest of the pulled snapshot failed: {exc}", _fixes(host, src, "error")
                    rep.results.append(res)
                else:
                    res = pull_direct(conn, config, host, r, log)
                    if res.state == OK and ingest_direct is not None:
                        try:
                            ingest_direct(Path(res.detail))
                            res.detail = "mirrored and ingested"
                        except Exception as exc:  # noqa: BLE001
                            log.write("ingest", [src], "exception", "", repr(exc))
                            res.state, res.cause, res.fix = REMOTE_ERROR, f"ingest of the mirror failed: {exc}", _fixes(host, src, "error")
                    rep.results.append(res)
            except Exception as exc:  # noqa: BLE001 — one source never takes down the rest
                log.write("unexpected", [src], "exception", "", repr(exc))
                rep.results.append(RemoteResult(host, src, REMOTE_ERROR, f"unexpected error: {exc}", _fixes(host, src, "error")))
    except Exception as exc:  # noqa: BLE001
        log.write("unexpected", [host], "exception", "", repr(exc))
        rep.results.append(RemoteResult(host, "*", REMOTE_ERROR, f"unexpected error: {exc}", _fixes(host, "*", "error")))
    return rep

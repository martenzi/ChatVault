from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from chatvault import audit as audit_mod
from chatvault import backup as backup_mod
from chatvault import curation
from chatvault import curation_io
from chatvault import dedupe as dedupe_mod
from chatvault import doctor as doctor_mod
from chatvault import export as export_mod
from chatvault import graph as graph_mod
from chatvault import ingest as ingest_mod
from chatvault import inventory as inventory_mod
from chatvault import search as search_mod
from chatvault import sources as sources_mod
from chatvault import verify as verify_mod
from chatvault.config import load_config
from chatvault.db import connect, migrate

app = typer.Typer(no_args_is_help=True, add_completion=False)
tag_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Manage tags.")
view_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Manage saved views.")
graph_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Neo4j projection layer.")
sources_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Track provenance of raw/ files.")
curation_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Export/import the curation layer (tags, annotations, exclusions, views).")
app.add_typer(tag_app, name="tag")
app.add_typer(view_app, name="view")
app.add_typer(graph_app, name="graph")
app.add_typer(sources_app, name="sources")
app.add_typer(curation_app, name="curation")
console = Console()


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _auto_backup(config, label: str) -> None:
    """Best-effort safety snapshot before a bulk/irreversible write. No-op on a fresh vault."""
    if not config.db_path.exists():
        return
    info = backup_mod.create_backup(config, label=label)
    console.print(f"[dim]auto-backup: {info.path.name}[/dim]")


@app.command()
def doctor() -> None:
    """Environment preflight: sqlite version, FTS5, neo4j reachability."""
    config = load_config()
    results = doctor_mod.run_all(config)
    table = Table(title="chatvault doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")

    fatal_failed = False
    for r in results:
        status = "[green]OK[/green]" if r.ok else ("[red]FAIL[/red]" if r.fatal else "[yellow]WARN[/yellow]")
        table.add_row(r.name, status, r.detail)
        if r.fatal and not r.ok:
            fatal_failed = True

    console.print(table)
    console.print(f"vault root: {config.root}")
    if fatal_failed:
        console.print("[red]One or more fatal checks failed.[/red]")
        raise typer.Exit(code=1)


@app.command()
def init() -> None:
    """Create vault directories and run migrations."""
    config = load_config()
    for d in [config.raw_dir, config.db_dir, config.exports_dir, config.neo4j_dir]:
        d.mkdir(parents=True, exist_ok=True)
        console.print(f"ensured {d}")

    _auto_backup(config, "pre-migrate")
    applied = migrate(config)
    if applied:
        console.print(f"[green]applied migrations:[/green] {', '.join(applied)}")
    else:
        console.print("[green]schema already up to date[/green]")
    console.print(f"database: {config.db_path}")


@app.command()
def backup(
    out: Path = typer.Option(None, "--out", help="Backup directory (default: <vault>/db/backups/)."),
    label: str = typer.Option(None, "--label"),
    list_: bool = typer.Option(False, "--list"),
    restore: Path = typer.Option(None, "--restore", help="Backup file to restore from."),
    to: Path = typer.Option(None, "--to", help="Restore target (default: the live DB)."),
    force: bool = typer.Option(False, "--force", help="With --restore, overwrite an existing target."),
    prune: bool = typer.Option(False, "--prune"),
    keep: int = typer.Option(None, "--keep", help="With --prune, how many recent backups to keep."),
) -> None:
    """SQLite Online Backup API + zstd/gzip compression. Never a plain file copy."""
    config = load_config()

    if list_:
        infos = backup_mod.list_backups(config, out_dir=out)
        table = Table(title="backups")
        table.add_column("file")
        table.add_column("label")
        table.add_column("created_at")
        table.add_column("size", justify="right")
        table.add_column("conversations", justify="right")
        table.add_column("tags", justify="right")
        for info in infos:
            table.add_row(
                info.path.name, info.label or "-", info.created_at,
                _human_bytes(info.bytes), str(info.conversation_count), str(info.tag_count),
            )
        console.print(table)
        return

    if restore:
        try:
            target = backup_mod.restore_backup(config, restore, to=to, force=force)
        except FileExistsError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        console.print(f"restored to {target}")
        return

    if prune:
        if keep is None:
            console.print("[red]--prune requires --keep N[/red]")
            raise typer.Exit(code=1)
        deleted = backup_mod.prune_backups(config, keep, out_dir=out)
        console.print(f"pruned {len(deleted)} backup(s)")
        return

    info = backup_mod.create_backup(config, label=label, out_dir=out)
    console.print(f"wrote {info.path} ({_human_bytes(info.bytes)}, {info.conversation_count} conversations, {info.tag_count} tags)")


@app.command()
def inventory(
    directory: Path = typer.Argument(None, exists=False, file_okay=False, help="Directory to scan (default: $CHATVAULT_ROOT/raw). Read-only."),
    deep: bool = typer.Option(False, "--deep", help="Parse file contents fully for accurate conversation counts."),
    force: bool = typer.Option(False, "--force", help="Allow scanning raw_exclusions/ directly."),
) -> None:
    """Scan a directory, detect platform + counts. Makes NO writes."""
    config = load_config()
    directory = directory or config.raw_dir
    if not directory.exists():
        console.print(f"[red]{directory} does not exist[/red]")
        raise typer.Exit(code=1)
    try:
        sources_mod.guard_raw_exclusions(config, directory, force)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    report = inventory_mod.scan(directory, deep=deep)

    table = Table(title=f"inventory: {directory}")
    table.add_column("platform")
    table.add_column("files", justify="right")
    table.add_column("size", justify="right")
    table.add_column("conversations" if deep else "conversations (use --deep for exact)", justify="right")

    totals = report.totals_by_platform()
    for platform, stats in sorted(totals.items()):
        conv = str(stats["conversations"]) if deep else "-"
        table.add_row(platform, str(stats["files"]), _human_bytes(stats["bytes"]), conv)

    console.print(table)

    if report.skipped:
        console.print(f"[dim]skipped {len(report.skipped)} file(s) with unrecognised extensions[/dim]")

    total_files = len(report.files)
    total_bytes = sum(f.bytes for f in report.files)
    console.print(f"total: {total_files} file(s), {_human_bytes(total_bytes)}")


@app.command()
def ingest(
    path: Path = typer.Argument(None, exists=False, help="File or directory to ingest (default: $CHATVAULT_ROOT/raw)."),
    adapter: str = typer.Option(None, "--adapter", help="Force a specific adapter instead of auto-detecting."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and report without writing to the database."),
    force: bool = typer.Option(False, "--force", help="Allow ingesting raw_exclusions/ directly."),
) -> None:
    """Ingest RAW export file(s) into SQLite. Idempotent — safe to re-run."""
    config = load_config()
    path = path or config.raw_dir
    if not path.exists():
        console.print(f"[red]{path} does not exist[/red]")
        raise typer.Exit(code=1)

    conn = connect(config)
    try:
        try:
            summary = ingest_mod.ingest_path(conn, path, adapter_name=adapter, dry_run=dry_run, force=force, config=config)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()

    table = Table(title=f"ingest{' (dry-run)' if dry_run else ''}: {path}")
    table.add_column("file")
    table.add_column("adapter")
    table.add_column("status")
    table.add_column("inserted", justify="right")
    table.add_column("noop", justify="right")
    table.add_column("superseded", justify="right")
    table.add_column("flagged", justify="right")

    for f in summary.files:
        table.add_row(
            str(f.path),
            f.adapter or "-",
            f.status if not f.error else f"[red]{f.status}[/red]",
            str(f.inserted),
            str(f.noop),
            str(f.superseded),
            str(f.flagged),
        )
    console.print(table)

    for f in summary.files:
        if f.error:
            console.print(f"[red]{f.path}: {f.error}[/red]")

    totals = summary.totals
    console.print(
        f"totals: inserted={totals['inserted']} noop={totals['noop']} "
        f"superseded={totals['superseded']} flagged={totals['flagged']}"
    )
    if totals["flagged"]:
        console.print("[yellow]Some conversations were flagged as ambiguous duplicates — see `chatvault dedupe --report`.[/yellow]")


@app.command()
def verify() -> None:
    """Row counts, orphan checks, FTS integrity, hash spot-checks."""
    config = load_config()
    conn = connect(config)
    try:
        results = verify_mod.run(conn)
    finally:
        conn.close()

    table = Table(title="chatvault verify")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")

    all_ok = True
    for r in results:
        status = "[green]OK[/green]" if r.ok else "[red]FAIL[/red]"
        table.add_row(r.name, status, r.detail)
        all_ok = all_ok and r.ok

    console.print(table)
    if not all_ok:
        raise typer.Exit(code=1)


@app.command()
def audit(
    raw_dir: Path = typer.Option(None, "--raw-dir", help="Directory to check for unclaimed files (default: $CHATVAULT_ROOT/raw)."),
    platform: str = typer.Option(None, "--platform"),
    json_out: bool = typer.Option(False, "--json"),
    markdown: Path = typer.Option(None, "--markdown", help="Write the report as markdown to this file."),
    sample: bool = typer.Option(False, "--sample", help="Emit a stratified edge-case sample, 5 per platform."),
    strict: bool = typer.Option(False, "--strict", help="Treat WARN as FAIL for exit code purposes."),
) -> None:
    """Did everything in the files end up in the database, in the right place?
    Compares DB state against disk and against expectations — distinct from
    `verify`, which only checks internal DB consistency."""
    config = load_config()
    conn = connect(config)
    try:
        report = audit_mod.run(conn, config, raw_dir=raw_dir, platform=platform, sample=sample)
        audit_mod.persist(conn, report)
    finally:
        conn.close()

    if json_out:
        import json as _json

        print(_json.dumps(audit_mod.to_dict(report), indent=2))
    else:
        for group in "ABCDEF":
            group_results = [r for r in report.results if r.group == group]
            if not group_results:
                continue
            table = Table(title=f"audit — group {group}")
            table.add_column("check")
            table.add_column("status")
            table.add_column("detail")
            for r in group_results:
                colour = {"pass": "green", "warn": "yellow", "fail": "red"}[r.status]
                table.add_row(r.name, f"[{colour}]{r.status.upper()}[/{colour}]", r.detail)
            console.print(table)

        if report.sample:
            table = Table(title="sample manifest (edge cases)")
            table.add_column("platform")
            table.add_column("slot")
            table.add_column("uid")
            table.add_column("command")
            for s in report.sample:
                table.add_row(s["platform"], s["slot"], s["uid"], s["show_cmd"])
            console.print(table)

        v = audit_mod.verdict(report)
        colour = {"pass": "green", "warn": "yellow", "fail": "red"}[v]
        console.print(f"[bold {colour}]verdict: {v.upper()}[/bold {colour}]")
        safe = "YES" if v != "fail" else "NO"
        console.print(f"Safe to archive sources: {safe}")

    if markdown:
        markdown.write_text(audit_mod.render_markdown(report))
        console.print(f"wrote {markdown}")

    raise typer.Exit(code=audit_mod.exit_code(report, strict=strict))


@app.command()
def export(
    uid: str = typer.Option(None, "--uid", help="Export a single conversation by uid."),
    where: str = typer.Option(None, "--where", help="SQL predicate over v_conversations."),
    view: str = typer.Option(None, "--view", help="Name of a saved view to export."),
    template: str = typer.Option("default", "--template", help="default | full | obsidian | digest"),
    out: Path = typer.Option(None, "--out", help="Output directory (default: <vault>/exports)."),
    split_by: str = typer.Option(None, "--split-by", help="'conversation' writes one file per conversation."),
) -> None:
    """Export conversations to markdown. Pure SQL + Jinja2 — no LLM calls, deterministic output."""
    config = load_config()
    conn = connect(config)
    try:
        rows = export_mod.get_conversations(conn, uid=uid, where=where, view=view)
        if not rows:
            console.print("[yellow]no conversations matched[/yellow]")
            raise typer.Exit(code=0)

        out_dir = out or config.exports_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        if template == "digest":
            content = export_mod.render_digest(conn, rows)
            path = out_dir / "digest.md"
            path.write_text(content)
            console.print(f"wrote {path}")
            return

        if split_by == "conversation" or (uid is None and split_by is None and len(rows) > 1):
            for row in rows:
                content = export_mod.render(conn, row, template)
                conv = dict(row)
                conv["uid_short"] = conv["uid"].split(":")[-1][:12]
                fname = export_mod.filename_for(conv, template)
                (out_dir / fname).write_text(content)
            console.print(f"wrote {len(rows)} file(s) to {out_dir}")
        else:
            row = rows[0]
            content = export_mod.render(conn, row, template)
            conv = dict(row)
            conv["uid_short"] = conv["uid"].split(":")[-1][:12]
            fname = export_mod.filename_for(conv, template)
            path = out_dir / fname
            path.write_text(content)
            console.print(f"wrote {path}")
    finally:
        conn.close()


@tag_app.command("add")
def tag_add(
    name: str = typer.Argument(...),
    where: str = typer.Option(..., "--where", help="SQL predicate over the target view."),
    target: str = typer.Option("conversation", "--target", help="conversation | message | block"),
) -> None:
    """Tag every row matching --where. Idempotent — safe to re-run."""
    config = load_config()
    conn = connect(config)
    try:
        n = curation.tag_add(conn, name, where, target)
    finally:
        conn.close()
    console.print(f"tagged {n} {target}(s) with '{name}'")


@tag_app.command("list")
def tag_list() -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = curation.tag_list(conn)
    finally:
        conn.close()
    table = Table(title="tags")
    table.add_column("name")
    table.add_column("uses", justify="right")
    for r in rows:
        table.add_row(r["name"], str(r["uses"]))
    console.print(table)


@view_app.command("save")
def view_save(
    name: str = typer.Argument(...),
    sql: str = typer.Option(..., "--sql", help="SQL returning at least an `id` column referencing conversations.id."),
    description: str = typer.Option(None, "--description"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        curation.view_save(conn, name, sql, description)
    finally:
        conn.close()
    console.print(f"saved view '{name}'")


@view_app.command("run")
def view_run(name: str = typer.Argument(...)) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = curation.view_run(conn, name)
    finally:
        conn.close()
    if not rows:
        console.print("[dim]no rows[/dim]")
        return
    table = Table(title=f"view: {name}")
    for col in rows[0].keys():
        table.add_column(col)
    for r in rows:
        table.add_row(*[str(v) for v in r])
    console.print(table)


@view_app.command("list")
def view_list() -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = curation.view_list(conn)
    finally:
        conn.close()
    table = Table(title="saved views")
    table.add_column("name")
    table.add_column("description")
    for r in rows:
        table.add_row(r["name"], r["description"] or "")
    console.print(table)


@app.command()
def note(
    target_type: str = typer.Argument(..., help="conversation | message | block"),
    target_id: int = typer.Argument(...),
    text: str = typer.Argument(...),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        note_id = curation.note_add(conn, target_type, target_id, text)
    finally:
        conn.close()
    console.print(f"annotation #{note_id} added to {target_type} {target_id}")


@app.command()
def exclude(
    target: str = typer.Option(..., "--target", help="conversation | message | block"),
    where: str = typer.Option(..., "--where", help="SQL predicate over the base table."),
    reason: str = typer.Option(None, "--reason"),
) -> None:
    """Reversible, logged exclusion. Never deletes anything."""
    config = load_config()
    conn = connect(config)
    try:
        precount = curation.matching_count(conn, target, where, currently_excluded=False)
        if precount > 100:
            _auto_backup(config, "pre-exclude")
        n = curation.exclude(conn, target, where, reason)
    finally:
        conn.close()
    console.print(f"excluded {n} {target}(s)")


@app.command()
def restore(
    target: str = typer.Option(..., "--target", help="conversation | message | block"),
    where: str = typer.Option(..., "--where", help="SQL predicate over the base table."),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        precount = curation.matching_count(conn, target, where, currently_excluded=True)
        if precount > 100:
            _auto_backup(config, "pre-restore-curation")
        n = curation.restore(conn, target, where)
    finally:
        conn.close()
    console.print(f"restored {n} {target}(s)")


@app.command()
def dedupe(
    report: bool = typer.Option(True, "--report/--no-report", help="Print superseded / candidate / repeated-block summary."),
    scan: bool = typer.Option(False, "--scan", help="Re-scan for cross-source head_hash collisions without applying anything."),
    apply: bool = typer.Option(False, "--apply", help="Resolve all verified-containment candidates (writes conversations.superseded_by only)."),
    candidate: int = typer.Option(None, "--candidate", help="With --apply, resolve only this candidate id."),
    supersede: str = typer.Option(None, "--supersede", help="uid to mark as superseded (manual override)."),
    by: str = typer.Option(None, "--by", help="With --supersede, the uid that supersedes it."),
    dismiss: int = typer.Option(None, "--dismiss", help="Candidate id to mark not-a-duplicate; stops re-flagging."),
    reason: str = typer.Option(None, "--reason", help="With --dismiss, why this isn't a duplicate."),
    undo: int = typer.Option(None, "--undo", help="Reverse a previously applied candidate."),
) -> None:
    """Cross-source dedupe. --apply only ever writes conversations.superseded_by —
    never touches blocks or excluded. Level 4 (repeated blocks) stays report-only
    forever; no threshold ever auto-excludes."""
    config = load_config()
    conn = connect(config)
    try:
        if scan:
            n = dedupe_mod.scan_candidates(conn)
            console.print(f"scan: {n} new candidate(s)")

        if apply:
            _auto_backup(config, "pre-dedupe-apply")
            if candidate is not None:
                applied, detail = dedupe_mod.apply_candidate(conn, candidate)
                console.print(f"candidate {candidate}: {'applied' if applied else 'left open'} — {detail}")
            else:
                results = dedupe_mod.apply_all(conn)
                applied_n = sum(1 for _, ok, _ in results if ok)
                console.print(f"--apply: {applied_n}/{len(results)} candidate(s) applied")
                for cid, ok, detail in results:
                    if not ok:
                        console.print(f"  [yellow]candidate {cid} left open — {detail}[/yellow]")

        if supersede:
            if not by:
                console.print("[red]--supersede requires --by <uid>[/red]")
                raise typer.Exit(code=1)
            _auto_backup(config, "pre-dedupe-supersede")
            cid = dedupe_mod.supersede(conn, supersede, by)
            console.print(f"{supersede} superseded by {by} (candidate {cid})")

        if dismiss is not None:
            dedupe_mod.dismiss(conn, dismiss, reason or "")
            console.print(f"candidate {dismiss} dismissed")

        if undo is not None:
            dedupe_mod.undo(conn, undo)
            console.print(f"candidate {undo} undone")

        if report:
            data = dedupe_mod.report(conn)

            console.print(f"[bold]superseded (level 3):[/bold] {len(data['superseded'])}")
            for r in data["superseded"]:
                console.print(f"  id={r['id']} uid={r['uid']} -> superseded_by={r['superseded_by']}")

            console.print(f"[bold]flagged duplicates (ambiguous, needs review):[/bold] {len(data['flagged_duplicates'])}")
            for r in data["flagged_duplicates"]:
                console.print(f"  id={r['id']} uid={r['uid']}")

            console.print("[bold]dedupe candidates:[/bold]")
            for r in data["candidates"]:
                console.print(f"  status={r['status']} reason={r['reason']} count={r['n']}")

            console.print(f"[bold]repeated blocks (level 4, >20 occurrences — always a deliberate exclude, never auto):[/bold] {len(data['repeated_blocks'])}")
            for r in data["repeated_blocks"][:20]:
                console.print(f"  hash={r['content_hash'][:12]}… count={r['n']}")
                console.print(f"    chatvault exclude --target block --where \"content_hash='{r['content_hash']}'\" --reason \"...\"")
    finally:
        conn.close()


@app.command()
def search(
    query: str = typer.Argument(...),
    platform: str = typer.Option(None, "--platform"),
    type: str = typer.Option(None, "--type", help="text | thinking | tool_use | tool_result | code | ..."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = search_mod.search(conn, query, platform=platform, type_=type, limit=limit)
    finally:
        conn.close()
    table = Table(title=f"search: {query!r}")
    table.add_column("uid")
    table.add_column("type")
    table.add_column("snippet")
    for r in rows:
        snippet = r["text"][:100].replace("\n", " ")
        table.add_row(r["uid"], r["type"], snippet)
    console.print(table)


@app.command()
def stats(
    by: str = typer.Option("platform", "--by", help="model | platform | project | month | tool"),
    unknown: bool = typer.Option(False, "--unknown", help="Report unmapped 'unknown' blocks by platform, so adapters can be improved incrementally."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = search_mod.unknown_block_report(conn) if unknown else search_mod.stats(conn, by)
    finally:
        conn.close()

    if as_json:
        import json as _json

        print(_json.dumps([dict(r) for r in rows], indent=2))
        return

    if unknown:
        table = Table(title="unknown blocks by platform")
        table.add_column("platform")
        table.add_column("count", justify="right")
        for r in rows:
            table.add_row(str(r["platform"]), str(r["n"]))
        console.print(table)
        return

    table = Table(title=f"stats by {by}")
    table.add_column(by)
    table.add_column("count", justify="right")
    for r in rows:
        table.add_row(str(r["key"]), str(r["n"]))
    console.print(table)


@app.command()
def show(
    uid: str = typer.Argument(...),
    template: str = typer.Option("full", "--template"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = export_mod.get_conversations(conn, uid=uid)
        if not rows:
            console.print(f"[red]no conversation with uid {uid!r}[/red]")
            raise typer.Exit(code=1)
        console.print(export_mod.render(conn, rows[0], template))
    finally:
        conn.close()


@graph_app.command("rebuild")
def graph_rebuild(depth: str = typer.Option("meta", "--depth", help="meta | full")) -> None:
    """Drop and re-seed the Neo4j projection from SQLite. Neo4j is always disposable."""
    config = load_config()
    conn = connect(config)
    driver = graph_mod.get_driver(config)
    try:
        counts = graph_mod.rebuild(conn, driver, depth=depth)
    finally:
        driver.close()
        conn.close()
    for k, v in counts.items():
        console.print(f"{k}: {v}")


@graph_app.command("push")
def graph_push(
    since: str = typer.Option(None, "--since", help="ISO date — only push conversations started on/after this date."),
    depth: str = typer.Option("meta", "--depth", help="meta | full"),
) -> None:
    """Incremental MERGE-based push — does not drop existing graph data."""
    config = load_config()
    conn = connect(config)
    driver = graph_mod.get_driver(config)
    try:
        counts = graph_mod.push(conn, driver, since=since, depth=depth)
    finally:
        driver.close()
        conn.close()
    for k, v in counts.items():
        console.print(f"{k}: {v}")


@graph_app.command("query")
def graph_query(cypher: str = typer.Argument(...)) -> None:
    config = load_config()
    driver = graph_mod.get_driver(config)
    try:
        rows = graph_mod.run_query(driver, cypher)
    finally:
        driver.close()
    if not rows:
        console.print("[dim]no rows[/dim]")
        return
    table = Table()
    for col in rows[0].keys():
        table.add_column(col)
    for r in rows:
        table.add_row(*[str(v) for v in r.values()])
    console.print(table)


@graph_app.command("canned")
def graph_canned(name: str = typer.Argument(..., help=f"one of: {', '.join(graph_mod.CANNED_QUERIES)}")) -> None:
    """Run one of the pre-written Cypher queries from the architecture spec."""
    if name not in graph_mod.CANNED_QUERIES:
        console.print(f"[red]unknown query {name!r}. options: {', '.join(graph_mod.CANNED_QUERIES)}[/red]")
        raise typer.Exit(code=1)
    config = load_config()
    driver = graph_mod.get_driver(config)
    try:
        rows = graph_mod.run_query(driver, graph_mod.CANNED_QUERIES[name])
    finally:
        driver.close()
    if not rows:
        console.print("[dim]no rows[/dim]")
        return
    table = Table(title=name)
    for col in rows[0].keys():
        table.add_column(col)
    for r in rows:
        table.add_row(*[str(v) for v in r.values()])
    console.print(table)


@sources_app.command("list")
def sources_list(kind: str = typer.Option(None, "--kind", help="live | frozen")) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = sources_mod.list_sources(conn, kind=kind)
    finally:
        conn.close()
    table = Table(title="sources")
    table.add_column("root_dir")
    table.add_column("kind")
    table.add_column("files", justify="right")
    table.add_column("last_seen_at")
    for r in rows:
        table.add_row(r["root_dir"] or "-", r["kind"], str(r["files"]), r["last_seen_at"] or "-")
    console.print(table)


@sources_app.command("mark")
def sources_mark(
    path_or_root: str = typer.Argument(...),
    kind: str = typer.Option(..., "--kind", help="live | frozen"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        n = sources_mod.mark_kind(conn, path_or_root, kind)
    finally:
        conn.close()
    console.print(f"marked {n} source_files row(s) as '{kind}'")


@sources_app.command("unclaimed")
def sources_unclaimed(
    directory: Path = typer.Argument(None, help="default: $CHATVAULT_ROOT/raw"),
) -> None:
    config = load_config()
    directory = directory or config.raw_dir
    conn = connect(config)
    try:
        paths = sources_mod.unclaimed(conn, directory, ignore_dir=config.root / "raw_exclusions")
    finally:
        conn.close()
    if not paths:
        console.print("[green]no unclaimed files[/green]")
        return
    for p in paths:
        console.print(str(p))
    console.print(f"[yellow]{len(paths)} unclaimed file(s)[/yellow]")


@sources_app.command("export-manifest")
def sources_export_manifest(out: Path = typer.Option(None, "--out")) -> None:
    config = load_config()
    conn = connect(config)
    try:
        path = sources_mod.export_manifest(conn, config, out=out)
    finally:
        conn.close()
    console.print(f"wrote {path}")


def _print_import_summary(summary) -> None:
    table = Table(title="curation import")
    table.add_column("entity")
    table.add_column("added", justify="right")
    table.add_column("updated", justify="right")
    table.add_column("unchanged", justify="right")
    for entity, c in summary.counts.items():
        table.add_row(entity, str(c.get("added", 0)), str(c.get("updated", 0)), str(c.get("unchanged", 0)))
    console.print(table)
    console.print(
        "[dim]annotations are always additive by design — never overwritten, "
        "regardless of --on-conflict[/dim]"
    )
    if summary.unresolved:
        table = Table(title="unresolved")
        table.add_column("entity")
        table.add_column("reason")
        table.add_column("target")
        for u in summary.unresolved:
            table.add_row(u["entity"], u["reason"], str(u["target"]))
        console.print(table)


@curation_app.command("export")
def curation_export(out: Path = typer.Option(None, "--out")) -> None:
    config = load_config()
    conn = connect(config)
    try:
        data = curation_io.export_curation(conn)
    finally:
        conn.close()
    out = out or (config.exports_dir / f"curation-{data['exported_at'][:10]}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2))
    console.print(f"wrote {out} ({', '.join(f'{k}={v}' for k, v in data['counts'].items())})")


@curation_app.command("import")
def curation_import(
    file: Path = typer.Argument(...),
    on_conflict: str = typer.Option("skip", "--on-conflict", help="skip | overwrite (annotations always additive)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    config = load_config()
    data = json.loads(file.read_text())
    conn = connect(config)
    try:
        if not dry_run:
            _auto_backup(config, "pre-curation-import")
        summary = curation_io.import_curation(conn, data, on_conflict=on_conflict, dry_run=dry_run)
    finally:
        conn.close()
    _print_import_summary(summary)
    if summary.unresolved:
        unresolved_path = curation_io.write_unresolved(file, summary.unresolved)
        console.print(f"[yellow]{len(summary.unresolved)} unresolved item(s) written to {unresolved_path}[/yellow]")


@curation_app.command("diff")
def curation_diff(file: Path = typer.Argument(...)) -> None:
    config = load_config()
    data = json.loads(file.read_text())
    conn = connect(config)
    try:
        summary = curation_io.diff_curation(conn, data)
    finally:
        conn.close()
    console.print("[dim]dry-run — nothing written[/dim]")
    _print_import_summary(summary)


@curation_app.command("replace-annotations")
def curation_replace_annotations(
    file: Path = typer.Argument(...),
    target: str = typer.Option(..., "--target", help="conv_uid whose annotations will be replaced."),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    """Destructive escape hatch — physical DELETE, no restore path. Refuses
    without --confirm. Takes a backup first. Every removal is logged."""
    config = load_config()
    data = json.loads(file.read_text())
    conn = connect(config)
    try:
        try:
            summary = curation_io.replace_annotations(conn, config, data, target, confirm)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"deleted {summary.deleted} annotation(s), added {summary.added} from {file}")


if __name__ == "__main__":
    app()

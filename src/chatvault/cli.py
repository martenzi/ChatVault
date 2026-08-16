from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from chatvault import attachments as attachments_mod
from chatvault import audit as audit_mod
from chatvault import backup as backup_mod
from chatvault import brain as brain_mod
from chatvault import brain_db
from chatvault import brief as brief_mod
from chatvault import browse as browse_mod
from chatvault import curation
from chatvault import curation_io
from chatvault import dedupe as dedupe_mod
from chatvault import embeddings as embeddings_mod
from chatvault import doctor as doctor_mod
from chatvault import export as export_mod
from chatvault import graph as graph_mod
from chatvault import ingest as ingest_mod
from chatvault import inventory as inventory_mod
from chatvault import ledger as ledger_mod
from chatvault import portfolio as portfolio_mod
from chatvault import projects as projects_mod
from chatvault import reconcile as reconcile_mod
from chatvault import search as search_mod
from chatvault import sources as sources_mod
from chatvault import stash as stash_mod
from chatvault import tags as tags_mod
from chatvault import verify as verify_mod
from chatvault.config import load_config
from chatvault.db import connect, migrate

app = typer.Typer(no_args_is_help=True, add_completion=False)
tag_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Manage tags.")
tag_rule_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Automatic tagging rules, applied on ingest.")
view_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Manage saved views.")
graph_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Neo4j projection layer.")
sources_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Track provenance of raw/ files.")
curation_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Export/import the curation layer (tags, annotations, exclusions, views).")
attachments_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Browse and curate attachments.")
projects_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Browse projects (Claude Projects, etc.).")
stash_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Second Brain v0 — the deliberate keep gesture (docs/40_SPEC_second_brain_v0.md).")
brain_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Second Brain, Layer 2 — a separate, portable database of curated items (docs/42_SPEC_brain_db.md).")
tag_app.add_typer(tag_rule_app, name="rule")
app.add_typer(tag_app, name="tag")
app.add_typer(view_app, name="view")
app.add_typer(graph_app, name="graph")
app.add_typer(sources_app, name="sources")
app.add_typer(curation_app, name="curation")
app.add_typer(attachments_app, name="attachments")
app.add_typer(projects_app, name="projects")
app.add_typer(stash_app, name="stash")
app.add_typer(brain_app, name="brain")
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
def ledger(
    markdown: Path = typer.Option(None, "--markdown", help="Regenerate this doc's '## 1. What's ingested' section in place (e.g. docs/05_INGESTION_LEDGER.md)."),
    stale: bool = typer.Option(False, "--stale", help="List server_side sources not re-exported in --days days."),
    days: int = typer.Option(30, "--days", help="With --stale, staleness threshold in days."),
) -> None:
    """What's in the vault, live from source_files — see docs/05_INGESTION_LEDGER.md §5.
    No flags: print the table. --markdown: regenerate a doc's ledger section.
    --stale: which server-side exports (claude_desktop/chatgpt/grok/gemini/deepseek) need refreshing."""
    config = load_config()
    conn = connect(config)
    try:
        if stale:
            stale_rows = ledger_mod.stale(conn, days=days)
            if not stale_rows:
                console.print(f"[green]nothing stale (threshold: {days}d)[/green]")
                return
            table = Table(title=f"stale server_side sources (>{days}d)")
            table.add_column("platform")
            table.add_column("kind")
            table.add_column("last_seen")
            table.add_column("days_stale", justify="right")
            for s in stale_rows:
                table.add_row(s.platform, s.kind, s.last_seen or "-", str(s.days_stale) if s.days_stale is not None else "?")
            console.print(table)
            return

        if markdown:
            ledger_mod.regenerate_markdown(conn, markdown)
            console.print(f"regenerated '## 1. What's ingested' in {markdown}")
            return

        rows = ledger_mod.render_table_rows(ledger_mod.rows(conn))
        table = Table(title="ledger")
        table.add_column("ingested")
        table.add_column("platform")
        table.add_column("kind")
        table.add_column("files", justify="right")
        table.add_column("convs", justify="right")
        table.add_column("source")
        for ingested, platform, kind, files, convs, source in rows:
            table.add_row(ingested, platform, kind, files, convs, source)
        console.print(table)
    finally:
        conn.close()


@stash_app.command("add")
def stash_add(
    pick: str = typer.Option(None, "--pick", help="Row number from the last `find` (exactly one)."),
    uid: str = typer.Option(None, "--uid", help="Conversation uid, alternative to --pick."),
    why: str = typer.Option(None, "--why", help="Required. Why this is worth keeping."),
    kind: str = typer.Option(None, "--kind", help=f"One of {sorted(stash_mod.KINDS)}."),
) -> None:
    """Deliberate keep gesture — tags stash/ (+ stash/<kind>) and records
    --why as an annotation. Refuses an empty --why (spec §2/§8)."""
    config = load_config()
    conn = connect(config)
    try:
        if uid is None and pick is None:
            console.print("[red]pass --uid or --pick <row>[/red]")
            raise typer.Exit(code=1)
        if pick is not None:
            try:
                conv_ids = browse_mod.resolve_pick(conn, config, pick)
            except (browse_mod.StaleCacheError, ValueError) as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1)
            if len(conv_ids) != 1:
                console.print("[red]`stash add --pick` takes exactly one row[/red]")
                raise typer.Exit(code=1)
            conv_id = conv_ids[0]
        else:
            row = conn.execute("SELECT id FROM v_conversations WHERE uid = ?", (uid,)).fetchone()
            if row is None:
                console.print(f"[red]no conversation with uid {uid!r}[/red]")
                raise typer.Exit(code=1)
            conv_id = row["id"]

        try:
            result = stash_mod.add(conn, conv_id, why, kind=kind)
        except stash_mod.StashError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"[green]stashed[/green] conv {result['conv_id']}" + (f" ({result['kind']})" if result["kind"] else ""))


@stash_app.command("list")
def stash_list(
    kind: str = typer.Option(None, "--kind", help=f"Filter to one of {sorted(stash_mod.KINDS)}."),
    since: str = typer.Option(None, "--since", help="ISO date — only entries stashed on/after this date."),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        entries = stash_mod.list_all(conn, kind=kind, since=since)
    finally:
        conn.close()
    if not entries:
        console.print("[dim]stash is empty[/dim]")
        return
    table = Table(title="stash")
    table.add_column("#", justify="right")
    table.add_column("saved")
    table.add_column("kind")
    table.add_column("why")
    table.add_column("source")
    for e in entries:
        source = f"{e.platform} · {(e.started_at or '')[:10] or '-'}"
        table.add_row(str(e.n), e.saved_at[:10], e.kind or "-", e.why, source)
    console.print(table)


@stash_app.command("show")
def stash_show(n: int = typer.Argument(..., help="Row number from `stash list` (full, unfiltered).")) -> None:
    config = load_config()
    conn = connect(config)
    try:
        try:
            entry = stash_mod.get_entry(conn, n)
        except stash_mod.StashError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        row = conn.execute("SELECT * FROM v_conversations WHERE id = ?", (entry.conv_id,)).fetchone()
        content = export_mod.render_profile(conn, row, profile="read")
    finally:
        conn.close()
    header = (
        f"why: {entry.why}\n"
        f"kind: {entry.kind or '-'}\n"
        f"saved: {entry.saved_at}\n"
        f"source: {entry.platform} · {(entry.started_at or '')[:10] or '-'}\n"
        f"uid: {entry.uid}\n"
        + "-" * 40
    )
    full = f"{header}\n\n{content}"
    if not sys.stdout.isatty():
        console.print(full)
    else:
        import click

        click.echo_via_pager(full)


@stash_app.command("drop")
def stash_drop(n: int = typer.Argument(..., help="Row number from `stash list` (full, unfiltered).")) -> None:
    """Removes the stash tags + why note. The conversation itself is never
    touched (spec §2/§8 — verify with `chatvault stats` before/after)."""
    config = load_config()
    conn = connect(config)
    try:
        try:
            result = stash_mod.drop(conn, n)
        except stash_mod.StashError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(
        f"dropped conv {result['conv_id']} from stash "
        f"({result['removed_tags']} tag link(s), {result['removed_notes']} note(s) removed; conversation untouched)"
    )


@app.command()
def brief(
    pick: str = typer.Option(..., "--pick", help="Row number(s) from `chatvault stash list`, e.g. '1' or '1,4'."),
    out: Path = typer.Option(None, "--out", help="Write to this directory instead of stdout."),
) -> None:
    """Turns a stashed conversation into a self-contained context pack you
    can paste or attach into a fresh AI conversation (docs/40_SPEC_second_brain_v0.md §3).
    Reuses the `lean` export profile for the body — no second renderer.
    --pick refers to `stash list`'s own numbering, not `find`'s — brief only
    ever operates on stashed conversations, and stash list's numbering is
    always fresh (never staled by the stash add that makes an item worth
    briefing in the first place)."""
    config = load_config()
    conn = connect(config)
    try:
        try:
            conv_ids = stash_mod.resolve_pick(conn, pick)
        except stash_mod.StashError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        if not conv_ids:
            console.print("[red]no rows resolved from --pick[/red]")
            raise typer.Exit(code=1)

        try:
            content = brief_mod.render_many(conn, conv_ids)
        except brief_mod.BriefError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            fname = brief_mod.filename_for(conn, conv_ids)
            path = out / fname
            path.write_text(content)
    finally:
        conn.close()

    if out is not None:
        console.print(f"wrote {path}")
    else:
        console.print(content)


@brain_app.command("promote")
def brain_promote(
    uid: str = typer.Option(None, "--uid", help="Conversation uid to promote."),
    pick: str = typer.Option(None, "--pick", help="Row number from the last `find` (exactly one) — alternative to --uid."),
    why: str = typer.Option(None, "--why", help="Required, unless --from-stash (read from each stash entry's own why)."),
    kind: str = typer.Option("conversation", "--kind", help=f"One of {sorted(brain_mod.KINDS)}."),
    from_stash: bool = typer.Option(False, "--from-stash", help="Promote every stash/-tagged conversation not already promoted."),
) -> None:
    """Snapshots a vault conversation into brain.sqlite: reads it read-only,
    renders with `lean`, freezes it as an item, marks the vault conversation
    stash/promoted (docs/42_SPEC_brain_db.md §4)."""
    config = load_config()
    vault_conn = connect(config)
    brain_conn = brain_db.connect(config)
    vault_ro = brain_db.connect_vault_readonly(config.db_path)
    try:
        if from_stash:
            results = brain_mod.promote_from_stash(brain_conn, vault_ro, vault_conn)
            if not results:
                console.print("[dim]nothing to promote — no stash/ items pending[/dim]")
                return
            for r in results:
                verb = "already promoted:" if r["action"] == "already_promoted" else "promoted:"
                console.print(f"[green]{verb}[/green] item {r['item_id']} — {r['title']}")
            return

        if uid is None and pick is None:
            console.print("[red]pass --uid, --pick, or --from-stash[/red]")
            raise typer.Exit(code=1)
        if pick is not None:
            try:
                conv_ids = browse_mod.resolve_pick(vault_conn, config, pick)
            except (browse_mod.StaleCacheError, ValueError) as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1)
            if len(conv_ids) != 1:
                console.print("[red]`brain promote --pick` takes exactly one row[/red]")
                raise typer.Exit(code=1)
            row = vault_conn.execute("SELECT uid FROM v_conversations WHERE id = ?", (conv_ids[0],)).fetchone()
            uid = row["uid"]

        try:
            result = brain_mod.promote(brain_conn, vault_ro, vault_conn, uid, why, kind=kind)
        except brain_mod.BrainError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        vault_ro.close()
        brain_conn.close()
        vault_conn.close()
    verb = "already promoted as" if result["action"] == "already_promoted" else "promoted as"
    console.print(f"[green]{verb}[/green] item {result['item_id']} — {result['title']}")


@brain_app.command("add")
def brain_add(
    title: str = typer.Argument(...),
    why: str = typer.Option(None, "--why", help="Required."),
    kind: str = typer.Option("note", "--kind", help=f"One of {sorted(brain_mod.KINDS)}."),
    body_file: Path = typer.Option(None, "--body-file", help="Markdown file to use as the item body."),
) -> None:
    """Manual item — no vault involved (spec §4)."""
    config = load_config()
    brain_conn = brain_db.connect(config)
    try:
        body = body_file.read_text() if body_file is not None else None
        try:
            result = brain_mod.add(brain_conn, title, why, kind=kind, body=body)
        except brain_mod.BrainError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        brain_conn.close()
    console.print(f"[green]added[/green] item {result['item_id']} — {result['title']}")


@brain_app.command("list")
def brain_list(
    kind: str = typer.Option(None, "--kind", help=f"One of {sorted(brain_mod.KINDS)}."),
    tag: str = typer.Option(None, "--tag", help="Exact tag name, or a prefix ending in '/**'."),
    confidence: str = typer.Option(None, "--confidence", help="verified | probable | unverified."),
) -> None:
    config = load_config()
    brain_conn = brain_db.connect(config)
    try:
        entries = brain_mod.list_all(brain_conn, kind=kind, tag=tag, confidence=confidence)
    finally:
        brain_conn.close()
    if not entries:
        console.print("[dim]brain is empty[/dim]")
        return
    table = Table(title="brain")
    table.add_column("#", justify="right")
    table.add_column("kind")
    table.add_column("title")
    table.add_column("why")
    table.add_column("confidence")
    table.add_column("saved")
    for e in entries:
        table.add_row(str(e.n), e.kind, e.title, e.why, e.confidence or "-", e.created_at[:10])
    console.print(table)


@brain_app.command("show")
def brain_show(n: int = typer.Argument(..., help="Row number from `brain list` (full, unfiltered).")) -> None:
    config = load_config()
    brain_conn = brain_db.connect(config)
    try:
        try:
            entry = brain_mod.get_entry(brain_conn, n)
        except brain_mod.BrainError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        row = brain_mod.get_item_row(brain_conn, entry.item_id)
    finally:
        brain_conn.close()
    header = (
        f"why: {row['why']}\n"
        f"kind: {row['kind']}\n"
        f"confidence: {row['confidence'] or '-'}\n"
        f"status: {row['status']}\n"
        f"source: {row['source_uid'] or '-'} ({row['source_kind'] or '-'})\n"
        f"saved: {row['created_at']}\n"
        + "-" * 40
    )
    full = f"{header}\n\n# {row['title']}\n\n{row['body'] or '(no body)'}"
    if not sys.stdout.isatty():
        console.print(full)
    else:
        import click

        click.echo_via_pager(full)


@brain_app.command("search")
def brain_search(
    query: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    config = load_config()
    brain_conn = brain_db.connect(config)
    try:
        rows = brain_mod.search(brain_conn, query, limit=limit)
    finally:
        brain_conn.close()
    if not rows:
        console.print("[dim]no matches[/dim]")
        return
    table = Table(title=f"brain search: {query!r}")
    table.add_column("id", justify="right")
    table.add_column("kind")
    table.add_column("title")
    table.add_column("why")
    for r in rows:
        table.add_row(str(r["id"]), r["kind"], r["title"], r["why"])
    console.print(table)


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
    reprocess: bool = typer.Option(False, "--reprocess", help="Bypass the file-level dedup and re-parse already-seen files. Existing conversations still resolve as noop by content_hash — this is for backfilling metadata an adapter fix now extracts (e.g. attachments) without ever duplicating rows."),
    embed: bool = typer.Option(True, "--embed/--no-embed", help="Embed newly inserted/superseded conversations for semantic search (spec: 'incrementally on new ingests'). Best-effort — an unreachable Ollama server is reported, not fatal."),
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
            summary = ingest_mod.ingest_path(conn, path, adapter_name=adapter, dry_run=dry_run, force=force, config=config, reprocess=reprocess, embed=embed)
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
    if summary.embedding is not None:
        e = summary.embedding
        console.print(f"embedding: embedded={e.embedded} skipped={e.skipped} failed={e.failed}")
        if e.unavailable:
            console.print(
                "[yellow]Ollama wasn't reachable — semantic search is stale for these conversations "
                "until you run `chatvault embed` (or `ollama serve`) later.[/yellow]"
            )


@app.command()
def reconcile(
    root: list[str] = typer.Option(None, "--root", help="Limit to specific tracked root_dir(s). Default: all tracked roots in scope."),
    kind: str = typer.Option("live", "--kind", help="live | frozen | all. Default 'live' — frozen sources are archived on purpose."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without writing presence changes."),
    json_out: bool = typer.Option(False, "--json"),
    retire: str = typer.Option(None, "--retire", help="Mark a root_dir or path as deliberately gone; requires --reason."),
    reason: str = typer.Option(None, "--reason", help="With --retire, why this source is expected to be absent."),
    status: bool = typer.Option(False, "--status", help="Show presence counts and the last reconcile run, without scanning disk."),
) -> None:
    """Diff DB-tracked source_files against what's actually on disk. Detects
    deletions that `ingest` (a push, disk -> DB) can never observe on its own."""
    config = load_config()
    conn = connect(config)
    try:
        if retire:
            if not reason:
                console.print("[red]--retire requires --reason[/red]")
                raise typer.Exit(code=1)
            n = reconcile_mod.retire(conn, retire, reason)
            console.print(f"retired {n} source_files row(s): {reason}")
            return

        if status:
            data = reconcile_mod.status(conn)
            console.print(f"presence counts: {data['counts'] or {}}")
            if data["last_run"]:
                lr = data["last_run"]
                console.print(
                    f"last run: {lr['run_at']} checked={lr['checked']} "
                    f"still_present={lr['still_present']} newly_missing={lr['newly_missing']} reappeared={lr['reappeared']}"
                )
            else:
                console.print("[dim]no reconcile runs yet[/dim]")
            return

        report = reconcile_mod.reconcile(conn, roots=root or None, kind=kind, dry_run=dry_run)
    finally:
        conn.close()

    if json_out:
        print(json.dumps(reconcile_mod.to_dict(report), indent=2))
        return

    console.print(f"roots checked: {len(report.roots)}")
    console.print(
        f"files checked={report.checked} still_present={report.still_present} "
        f"newly_missing={report.newly_missing} reappeared={report.reappeared}"
    )
    if report.missing:
        console.print("[yellow]missing:[/yellow]")
        for m in report.missing[:20]:
            console.print(f"  {m['path']}")
        if len(report.missing) > 20:
            console.print(f"  ... and {len(report.missing) - 20} more")


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


def _render_find_table(title: str, rows: list, total: int) -> None:
    from rich.markup import escape

    table = Table(title=title)
    table.add_column("#", justify="right")
    table.add_column("date")
    table.add_column("platform")
    table.add_column("project")
    table.add_column("title")
    table.add_column("msgs", justify="right")
    table.add_column("📎", justify="right")
    for i, r in enumerate(rows, start=1):
        table.add_row(
            str(i),
            (r["started_at"] or "")[:10] or "-",
            r["platform"],
            escape(r["project"]) if r["project"] else "—",
            escape(r["title"]) if r["title"] else "Untitled",
            str(r["message_count"]),
            str(r["attachment_count"]) if r["attachment_count"] else "",
        )
    console.print(table)
    console.print(f"showing {len(rows)} of {total} matches · saved as last query")


@app.command()
def find(
    query: str = typer.Argument(None, help="Free text. Titles match first, body text (FTS) second. Omit for most recent."),
    platform: str = typer.Option(None, "--platform"),
    since: str = typer.Option(None, "--since", help="ISO date, inclusive."),
    before: str = typer.Option(None, "--before", help="ISO date, exclusive."),
    project: str = typer.Option(None, "--project"),
    tag: str = typer.Option(None, "--tag", help="Exact tag name, or a prefix ending in '/**' to match that tag and everything nested under it, e.g. 'tech/macos/**'."),
    has_attachments: bool = typer.Option(False, "--has-attachments"),
    untagged: bool = typer.Option(False, "--untagged", help="Only conversations with no tags at all."),
    limit: int = typer.Option(20, "--limit"),
    sort: str = typer.Option("recent", "--sort", help="recent | oldest | longest"),
) -> None:
    """Human-facing lister — find by title/date/platform, not uid. Numbers the
    results and caches them so `export --pick`/`show --pick` can select by row."""
    config = load_config()
    conn = connect(config)
    try:
        try:
            rows, total = browse_mod.find(
                conn, query=query, platform=platform, since=since, before=before,
                project=project, tag=tag, has_attachments=has_attachments, untagged=untagged, limit=limit, sort=sort,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        browse_mod.save_result_cache(
            conn, config, rows,
            {"query": query, "platform": platform, "since": since, "before": before,
             "project": project, "tag": tag, "has_attachments": has_attachments, "untagged": untagged,
             "limit": limit, "sort": sort},
        )
    finally:
        conn.close()

    _render_find_table(f"find: {query!r}" if query else "find: most recent", rows, total)


def _interactive_pick(conn):
    try:
        import questionary
    except ImportError:
        return "no-questionary"

    rows, _total = browse_mod.find(conn, limit=20, sort="recent")
    if not rows:
        console.print("[yellow]no conversations to pick from[/yellow]")
        return None

    choices = [
        questionary.Choice(
            title=f"{(r['started_at'] or '')[:10] or '-':10} {r['platform']:12} {(r['title'] or 'Untitled')[:50]}",
            value=r,
        )
        for r in rows
    ]
    selected = questionary.select("Pick a conversation to export:", choices=choices).ask()
    if selected is None:
        return None
    profile = questionary.select("Profile:", choices=["read", "lean", "full"], default="read").ask()
    if profile is None:
        return None
    return {"row": selected, "profile": profile}


@app.command()
def export(
    uid: str = typer.Option(None, "--uid", help="Export a single conversation by uid."),
    where: str = typer.Option(None, "--where", help="SQL predicate over v_conversations."),
    view: str = typer.Option(None, "--view", help="Name of a saved view to export."),
    pick: str = typer.Option(None, "--pick", help="Row number(s) from the last `find`, e.g. '2' or '1,3'."),
    tree: str = typer.Option(None, "--tree", help="A tag or prefix (e.g. 'tech/**') — writes every matching conversation into a nested folder structure mirroring the tag tree, under --out. A conversation matching more than one tag in the tree appears under each."),
    profile: str = typer.Option(None, "--profile", help="read | lean | full. Defaults to 'read' whenever --pick/--tree/--blocks/--max-tool-chars is used."),
    blocks: str = typer.Option(None, "--blocks", help="Comma-separated block types to keep, e.g. 'text,code'. Overrides the profile's block selection."),
    max_tool_chars: int = typer.Option(None, "--max-tool-chars", help="Collapse tool_use/tool_result bodies longer than this. Overrides the profile's own budget."),
    template: str = typer.Option("default", "--template", help="default | full | obsidian | digest (legacy path — ignored once --profile/--pick/--blocks is set)."),
    out: Path = typer.Option(None, "--out", help="Output directory (default: <vault>/exports)."),
    split_by: str = typer.Option(None, "--split-by", help="'conversation' writes one file per conversation."),
) -> None:
    """Export conversations to markdown. Pure SQL + Jinja2 — no LLM calls, deterministic output."""
    config = load_config()
    conn = connect(config)
    try:
        if tree is not None:
            tree_rows = export_mod.get_conversations_for_tree(conn, tree)
            if not tree_rows:
                console.print(f"[yellow]no conversations tagged under '{tree}'[/yellow]")
                raise typer.Exit(code=0)

            out_dir = out or config.exports_dir
            effective_profile = profile or "read"
            written = 0
            for row in tree_rows:
                content = export_mod.render_profile(
                    conn, row, profile=effective_profile, blocks=blocks, max_tool_chars=max_tool_chars
                )
                conv = dict(row)
                conv["uid_short"] = conv["uid"].split(":")[-1][:12]
                target_dir = out_dir / export_mod.tree_relative_dir(tree, row["matched_tag"])
                target_dir.mkdir(parents=True, exist_ok=True)
                fname = export_mod.filename_for(conv, effective_profile)
                (target_dir / fname).write_text(content)
                written += 1
            console.print(f"wrote {written} file(s) under {out_dir} (tag tree '{tree}')")
            return

        use_profile = pick is not None or profile is not None or blocks is not None or max_tool_chars is not None

        if uid is None and where is None and view is None and pick is None:
            picked = _interactive_pick(conn)
            if picked == "no-questionary":
                console.print(
                    "[red]No selection given, and the interactive picker (`questionary`) isn't installed.[/red]\n"
                    "Install it with `pip install questionary`, or pass --uid/--where/--view/--pick."
                )
                raise typer.Exit(code=1)
            if picked is None:
                console.print("[dim]cancelled[/dim]")
                raise typer.Exit(code=0)
            rows = [picked["row"]]
            use_profile = True
            if profile is None:
                profile = picked["profile"]
        elif pick is not None:
            try:
                conv_ids = browse_mod.resolve_pick(conn, config, pick)
            except (browse_mod.StaleCacheError, ValueError) as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1)
            rows = export_mod.get_conversations_by_ids(conn, conv_ids)
        else:
            rows = export_mod.get_conversations(conn, uid=uid, where=where, view=view)

        if not rows:
            console.print("[yellow]no conversations matched[/yellow]")
            raise typer.Exit(code=0)

        out_dir = out or config.exports_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        if not use_profile and template == "digest":
            content = export_mod.render_digest(conn, rows)
            path = out_dir / "digest.md"
            path.write_text(content)
            console.print(f"wrote {path}")
            return

        effective_profile = profile or "read"

        def _render(row):
            if use_profile:
                return export_mod.render_profile(
                    conn, row, profile=effective_profile, blocks=blocks, max_tool_chars=max_tool_chars
                )
            return export_mod.render(conn, row, template)

        name_for_filename = effective_profile if use_profile else template

        if split_by == "conversation" or (uid is None and split_by is None and len(rows) > 1):
            for row in rows:
                content = _render(row)
                conv = dict(row)
                conv["uid_short"] = conv["uid"].split(":")[-1][:12]
                fname = export_mod.filename_for(conv, name_for_filename)
                (out_dir / fname).write_text(content)
            console.print(f"wrote {len(rows)} file(s) to {out_dir}")
        else:
            row = rows[0]
            content = _render(row)
            conv = dict(row)
            conv["uid_short"] = conv["uid"].split(":")[-1][:12]
            fname = export_mod.filename_for(conv, name_for_filename)
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


@tag_app.command("tree")
def tag_tree_cmd(
    pattern: str = typer.Argument(None, help="e.g. 'tech/**' to show one subtree. Omit for the whole tree."),
) -> None:
    """Browse the tag vocabulary as a hierarchy with cumulative counts —
    'inspecting a structure you built', not searching. Use `find --tag` when
    a tag is one filter among several instead."""
    config = load_config()
    conn = connect(config)
    try:
        roots = tags_mod.tag_tree(conn, pattern)
        info = tags_mod.stats(conn)
    finally:
        conn.close()

    if not roots:
        console.print("[dim]no tags[/dim]" if not pattern else f"[dim]no tags under {pattern!r}[/dim]")
        return

    from rich.markup import escape
    from rich.tree import Tree

    def add_children(rich_node, nodes: dict) -> None:
        for name in sorted(nodes):
            node = nodes[name]
            count = tags_mod.cumulative_count(node)
            branch = rich_node.add(f"{escape(node.name)}  [dim]{count}[/dim]")
            add_children(branch, node.children)

    root_tree = Tree(pattern or "tags")
    add_children(root_tree, roots)
    console.print(root_tree)
    console.print(
        f"{info['total_tags']} tags · {info['tagged_convs']} tagged · {info['untagged_convs']} untagged"
    )


@tag_app.command("show")
def tag_show_cmd(
    name: str = typer.Argument(...),
    exact: bool = typer.Option(False, "--exact", help="Only this exact tag, not everything nested under it."),
    include_children: bool = typer.Option(True, "--include-children/--no-include-children", help="Default true: covers everything nested under `name` too."),
    limit: int = typer.Option(200, "--limit"),
) -> None:
    """Drill into a tag — the conversations carrying it. Same engine, output,
    and --pick cache as `find --tag`, just the right verb for browsing."""
    config = load_config()
    conn = connect(config)
    try:
        tag_query = tags_mod.show_query(name, exact=exact, include_children=include_children)
        rows, total = browse_mod.find(conn, tag=tag_query, limit=limit, sort="recent")
        browse_mod.save_result_cache(
            conn, config, rows,
            {"query": None, "platform": None, "since": None, "before": None,
             "project": None, "tag": tag_query, "has_attachments": False, "untagged": False,
             "limit": limit, "sort": "recent"},
        )
    finally:
        conn.close()
    _render_find_table(f"tag show: {name}", rows, total)


@tag_app.command("review")
def tag_review_cmd(limit: int = typer.Option(200, "--limit")) -> None:
    """Shorthand for `tag show 'review/**'` — the anomalies worth checking
    regularly, without spelling out a glob."""
    config = load_config()
    conn = connect(config)
    try:
        rows, total = browse_mod.find(conn, tag="review/**", limit=limit, sort="recent")
        browse_mod.save_result_cache(
            conn, config, rows,
            {"query": None, "platform": None, "since": None, "before": None,
             "project": None, "tag": "review/**", "has_attachments": False, "untagged": False,
             "limit": limit, "sort": "recent"},
        )
    finally:
        conn.close()
    _render_find_table("tag review", rows, total)


@tag_app.command("stats")
def tag_stats_cmd() -> None:
    """The tagging backlog at a glance: tag count, tagged/untagged
    conversations, and tags nobody has used yet."""
    config = load_config()
    conn = connect(config)
    try:
        info = tags_mod.stats(conn)
    finally:
        conn.close()
    console.print(f"tags: {info['total_tags']}  (unused: {info['zero_use_tags']})")
    console.print(f"conversations tagged: {info['tagged_convs']}  untagged: {info['untagged_convs']}")


@tag_app.command("untag")
def tag_untag(
    name: str = typer.Argument(None, help="Tag name. Omit when using --source alone to undo an entire bulk operation across every tag name it wrote."),
    where: str = typer.Option(None, "--where", help="SQL predicate over the target view."),
    target: str = typer.Option("conversation", "--target", help="conversation | message | block"),
    source: str = typer.Option(None, "--source", help="Only remove links applied by this source, e.g. 'folder_import', 'rule', or 'llm' — the bulk-reversibility path. Combine with a tag name to narrow, or use alone to undo every tag that source ever wrote."),
) -> None:
    """Remove a tag (or, with --source alone, every tag from that source
    regardless of name) from matching rows. Pairs with `tag add`/
    `import-from-folders`/`rule`/`portfolio-analyze` for reversibility."""
    if name is None and source is None:
        console.print("[red]pass a tag name, --source, or both[/red]")
        raise typer.Exit(code=1)
    config = load_config()
    conn = connect(config)
    try:
        n = tags_mod.untag(conn, name=name, where=where, target=target, source=source)
    finally:
        conn.close()
    label = f"'{name}'" if name else f"source='{source}'"
    console.print(f"removed {label} from {n} {target}(s)")


@tag_app.command("import-from-folders")
def tag_import_from_folders(
    path: Path = typer.Argument(..., exists=True, file_okay=False, help="Root of the folder tree to import."),
    prefix: str = typer.Option(None, "--prefix", help="Prepended to every tag, e.g. 'project/claude-web'."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report matches/unmatches without writing tags."),
) -> None:
    """Turn an existing folder organisation into hierarchical tags. Matches
    files to conversations by title + date; unmatched files are reported,
    never silently dropped."""
    config = load_config()
    conn = connect(config)
    try:
        report = tags_mod.import_from_folders(conn, path, prefix=prefix, dry_run=dry_run)
    finally:
        conn.close()

    table = Table(title=f"import-from-folders{' (dry-run)' if dry_run else ''}: {path}")
    table.add_column("file")
    table.add_column("tag")
    table.add_column("conv_uid")
    for m in report.matched:
        table.add_row(m["file"], m["tag"], m["conv_uid"])
    console.print(table)

    console.print(f"matched: {len(report.matched)}  unmatched: {len(report.unmatched)}")
    if report.unmatched:
        u_table = Table(title="unmatched")
        u_table.add_column("file")
        u_table.add_column("reason")
        for u in report.unmatched:
            u_table.add_row(u["file"], u["reason"])
        console.print(u_table)


@tag_app.command("import-tree")
def tag_import_tree_cmd(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="YAML taxonomy file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be created without writing anything."),
) -> None:
    """Parse a nested YAML taxonomy into real tag rows — no conversations
    tagged, just makes the whole tree exist in the palette up front."""
    config = load_config()
    conn = connect(config)
    try:
        names = tags_mod.import_tree(conn, path, dry_run=dry_run)
    finally:
        conn.close()
    console.print(f"{'would create' if dry_run else 'ensured'} {len(names)} tag(s)")
    for name in names:
        console.print(f"  {name}")


@tag_app.command("rename")
def tag_rename_cmd(
    old_name: str = typer.Argument(...),
    new_name: str = typer.Argument(...),
) -> None:
    """Renames a tag and everything nested under it (old/**). Every tag_link
    is preserved. Merges into the destination if it already exists."""
    config = load_config()
    conn = connect(config)
    try:
        n = tags_mod.rename(conn, old_name, new_name)
    finally:
        conn.close()
    console.print(f"renamed {n} tag(s): '{old_name}' -> '{new_name}' (and everything nested under it)")


@tag_app.command("merge")
def tag_merge_cmd(
    source: str = typer.Argument(...),
    target: str = typer.Argument(...),
) -> None:
    """Moves every use of `source` onto `target`, then deletes `source`.
    Both must already exist. Does not recurse into nested tags — use `rename`
    for a whole branch."""
    config = load_config()
    conn = connect(config)
    try:
        try:
            n = tags_mod.merge(conn, source, target)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"merged '{source}' into '{target}': {n} link(s) moved")


@tag_rule_app.command("add")
def tag_rule_add(
    tag_name: str = typer.Argument(...),
    where: str = typer.Option(..., "--where", help="SQL predicate over the real `blocks` table, e.g. \"blocks.text LIKE '%docker%'\"."),
    min_hits: int = typer.Option(1, "--min-hits", help="Minimum matching blocks within a conversation before the tag applies."),
) -> None:
    """Store a rule. Applied automatically to new/changed conversations on
    every future `ingest` (source='rule'); run `tag rule apply` to backfill
    existing ones too. Always reversible: `tag untag <name> --source rule`."""
    config = load_config()
    conn = connect(config)
    try:
        rule_id = tags_mod.rule_add(conn, tag_name, where, min_hits=min_hits)
    finally:
        conn.close()
    console.print(f"rule #{rule_id} added: tag '{tag_name}' where ({where}) min_hits={min_hits}")


@tag_rule_app.command("list")
def tag_rule_list() -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = tags_mod.rule_list(conn)
    finally:
        conn.close()
    table = Table(title="tag rules")
    table.add_column("id")
    table.add_column("tag")
    table.add_column("where")
    table.add_column("min_hits", justify="right")
    table.add_column("enabled")
    for r in rows:
        table.add_row(str(r["id"]), r["tag_name"], r["where_sql"], str(r["min_hits"]), "yes" if r["enabled"] else "no")
    console.print(table)


@tag_rule_app.command("remove")
def tag_rule_remove(rule_id: int = typer.Argument(...)) -> None:
    """Deletes the rule definition. Does not retroactively untag anything —
    use `tag untag <name> --source rule` for that."""
    config = load_config()
    conn = connect(config)
    try:
        ok = tags_mod.rule_remove(conn, rule_id)
    finally:
        conn.close()
    if not ok:
        console.print(f"[red]no rule #{rule_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"removed rule #{rule_id}")


@tag_rule_app.command("apply")
def tag_rule_apply() -> None:
    """Backfill: (re-)evaluate every enabled rule against every live
    conversation, not just new ones. Idempotent — already-applied tags are
    skipped via the tag_links unique constraint."""
    config = load_config()
    conn = connect(config)
    try:
        n = tags_mod.apply_rules(conn)
    finally:
        conn.close()
    console.print(f"applied {n} new tag(s)")


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
    semantic: bool = typer.Option(False, "--semantic", help="Search conversations by meaning (embeddings), not keyword overlap. Ignores --type (block-level, meaningless for a per-conversation vector search)."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Keyword search over blocks (default) or semantic search over
    conversations (--semantic) — finds conversations whose meaning matches
    the query even if they share none of its actual words."""
    config = load_config()
    conn = connect(config)
    try:
        if semantic:
            try:
                rows = embeddings_mod.semantic_search(conn, query, limit=limit)
            except embeddings_mod.EmbeddingUnavailable as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1)
        else:
            rows = search_mod.search(conn, query, platform=platform, type_=type, limit=limit)
    finally:
        conn.close()

    if semantic:
        table = Table(title=f"semantic search: {query!r}")
        table.add_column("date")
        table.add_column("platform")
        table.add_column("title")
        for r in rows:
            table.add_row((r["started_at"] or "")[:10] or "-", r["platform"], r["title"] or "Untitled")
        console.print(table)
        console.print(f"{len(rows)} result(s)")
        return

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
    by: str = typer.Option("platform", "--by", help="model | platform | project | month | tool | tag"),
    prefix: str = typer.Option(None, "--prefix", help="With --by tag: restrict to a tag prefix, e.g. 'tech/macos' (matches that tag and everything nested under it)."),
    unknown: bool = typer.Option(False, "--unknown", help="Report unmapped 'unknown' blocks by platform, so adapters can be improved incrementally."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = search_mod.unknown_block_report(conn) if unknown else search_mod.stats(conn, by, prefix=prefix)
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
    uid: str = typer.Argument(None, help="Conversation uid. Omit when using --pick."),
    pick: str = typer.Option(None, "--pick", help="Row number from the last `find` (exactly one)."),
    profile: str = typer.Option("read", "--profile", help="read | lean | full."),
    blocks: str = typer.Option(None, "--blocks", help="Comma-separated block types to keep, e.g. 'text,code'."),
    max_tool_chars: int = typer.Option(None, "--max-tool-chars"),
    template: str = typer.Option(None, "--template", help="Legacy path: default | full | obsidian. Overrides --profile/--blocks when given."),
    raw: bool = typer.Option(False, "--raw", help="Print directly, no pager — for piping."),
) -> None:
    """Read a conversation without exporting a file."""
    config = load_config()
    conn = connect(config)
    try:
        if uid is None and pick is None:
            console.print("[red]pass a uid or --pick <row>[/red]")
            raise typer.Exit(code=1)

        if pick is not None:
            try:
                conv_ids = browse_mod.resolve_pick(conn, config, pick)
            except (browse_mod.StaleCacheError, ValueError) as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1)
            if len(conv_ids) != 1:
                console.print("[red]`show --pick` takes exactly one row[/red]")
                raise typer.Exit(code=1)
            rows = export_mod.get_conversations_by_ids(conn, conv_ids)
        else:
            rows = export_mod.get_conversations(conn, uid=uid)

        if not rows:
            console.print("[red]no matching conversation[/red]")
            raise typer.Exit(code=1)

        if template:
            content = export_mod.render(conn, rows[0], template)
        else:
            content = export_mod.render_profile(
                conn, rows[0], profile=profile, blocks=blocks, max_tool_chars=max_tool_chars
            )
    finally:
        conn.close()

    if raw or not sys.stdout.isatty():
        console.print(content)
    else:
        import click

        click.echo_via_pager(content)


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
def sources_list(kind: str = typer.Option(None, "--kind", help="live | frozen | server_side")) -> None:
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
    table.add_column("missing", justify="right")
    table.add_column("retired", justify="right")
    for r in rows:
        missing = r["missing"] or 0
        table.add_row(
            r["root_dir"] or "-", r["kind"], str(r["files"]), r["last_seen_at"] or "-",
            f"[yellow]{missing}[/yellow]" if missing else "0",
            str(r["retired"] or 0),
        )
    console.print(table)


@sources_app.command("mark")
def sources_mark(
    path_or_root: str = typer.Argument(...),
    kind: str = typer.Option(..., "--kind", help="live | frozen | server_side"),
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


@attachments_app.command("list")
def attachments_list(
    conv: int = typer.Option(None, "--conv", help="Conversation id."),
    project: int = typer.Option(None, "--project", help="Project id."),
    platform: str = typer.Option(None, "--platform"),
    type_: str = typer.Option(None, "--type", help="Extension, e.g. 'pdf'."),
    missing: bool = typer.Option(False, "--missing", help="Only availability='missing' rows."),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = attachments_mod.list_attachments(conn, conv=conv, project=project, platform=platform, type_=type_, missing=missing)
    finally:
        conn.close()
    table = Table(title="attachments")
    table.add_column("id")
    table.add_column("filename")
    table.add_column("platform")
    table.add_column("availability")
    table.add_column("bytes", justify="right")
    for r in rows:
        table.add_row(str(r["id"]), r["filename"] or "-", r["platform"], r["availability"], str(r["bytes"] or ""))
    console.print(table)
    console.print(f"{len(rows)} attachment(s)")


@attachments_app.command("show")
def attachments_show(attachment_id: int = typer.Argument(...)) -> None:
    config = load_config()
    conn = connect(config)
    try:
        row = attachments_mod.show(conn, attachment_id)
    finally:
        conn.close()
    if row is None:
        console.print(f"[red]no attachment with id {attachment_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"# {row['filename'] or 'untitled'}  ({row['platform']}, {row['availability']})")
    console.print(row["content"] or "[dim](no content — availability != 'text')[/dim]")


@attachments_app.command("stats")
def attachments_stats() -> None:
    config = load_config()
    conn = connect(config)
    try:
        data = attachments_mod.stats(conn)
    finally:
        conn.close()
    console.print(f"total: {data['total']}  bytes: {_human_bytes(data['bytes'])}  missing: {data['missing']}")
    table = Table(title="by platform")
    table.add_column("platform")
    table.add_column("count", justify="right")
    for r in data["by_platform"]:
        table.add_row(r["platform"], str(r["n"]))
    console.print(table)


@attachments_app.command("search")
def attachments_search(
    query: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = attachments_mod.search(conn, query, limit=limit)
    finally:
        conn.close()
    table = Table(title=f"attachments search: {query!r}")
    table.add_column("id")
    table.add_column("filename")
    table.add_column("platform")
    table.add_column("snippet")
    for r in rows:
        table.add_row(str(r["id"]), r["filename"] or "-", r["platform"], r["snippet"])
    console.print(table)


@attachments_app.command("exclude")
def attachments_exclude(
    attachment_id: int = typer.Argument(None),
    where: str = typer.Option(None, "--where", help="SQL predicate over attachments."),
    reason: str = typer.Option(None, "--reason"),
) -> None:
    """Hide it. Reversible. The normal move."""
    config = load_config()
    conn = connect(config)
    try:
        try:
            n = attachments_mod.exclude(conn, attachment_id=attachment_id, where=where, reason=reason)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"excluded {n} attachment(s)")


@attachments_app.command("restore")
def attachments_restore(
    attachment_id: int = typer.Argument(None),
    where: str = typer.Option(None, "--where"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        try:
            n = attachments_mod.restore(conn, attachment_id=attachment_id, where=where)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"restored {n} attachment(s)")


@attachments_app.command("redact")
def attachments_redact(
    attachment_id: int = typer.Argument(None),
    where: str = typer.Option(None, "--where"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    """Wipe the content, keep the record: content -> NULL, availability ->
    'missing'. You keep the historical fact it existed, lose the text."""
    config = load_config()
    conn = connect(config)
    try:
        try:
            n = attachments_mod.redact(conn, attachment_id=attachment_id, where=where, confirm=confirm)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"redacted {n} attachment(s)")


@attachments_app.command("purge")
def attachments_purge(
    attachment_id: int = typer.Argument(None),
    where: str = typer.Option(None, "--where"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    """Delete the row entirely. Takes a backup first, logs it, requires --confirm."""
    config = load_config()
    conn = connect(config)
    try:
        try:
            n = attachments_mod.purge(conn, config, attachment_id=attachment_id, where=where, confirm=confirm)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"purged {n} attachment(s)")


@projects_app.command("list")
def projects_list() -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = projects_mod.list_projects(conn)
    finally:
        conn.close()
    from rich.markup import escape

    table = Table(title="projects")
    table.add_column("id")
    table.add_column("platform")
    table.add_column("kind")
    table.add_column("project")
    table.add_column("convs", justify="right")
    table.add_column("docs", justify="right")
    table.add_column("path")
    for r in rows:
        name = f"[yellow]⚠ {escape(r['name'])}[/yellow]" if r["ambiguous"] else escape(r["name"])
        table.add_row(
            str(r["id"]), r["platform"], r["kind"], name, str(r["convs"]), str(r["docs"]),
            escape(r["path"]) if r["path"] else "—",
        )
    console.print(table)


@projects_app.command("backfill")
def projects_backfill() -> None:
    """Promote `conversations.project` labels into real `projects` rows,
    keyed on normalised workspace path (docs/FIX_project_identity.md).
    Idempotent — safe to re-run after every ingest."""
    config = load_config()
    _auto_backup(config, "pre-project-identity")
    conn = connect(config)
    try:
        report = projects_mod.backfill(conn)
    finally:
        conn.close()
    console.print(
        f"resolved={report.resolved} scratch={report.scratch} unresolved={report.unresolved} "
        f"guessed_unresolved={report.guessed_unresolved} "
        f"projects_created={report.projects_created} ambiguous_projects={report.ambiguous_projects}"
    )
    if report.unresolved:
        console.print("[yellow]some conversations had no determinable workspace — tagged review/project-unresolved[/yellow]")
    if report.guessed_unresolved:
        console.print(f"[yellow]{report.guessed_unresolved} conversation(s) had a file-like project guess with no project root found on disk — tagged review/project-guessed[/yellow]")
    if report.ambiguous_projects:
        console.print(f"[yellow]{report.ambiguous_projects} project name(s) collide across different paths — see `chatvault projects review`[/yellow]")


@projects_app.command("show")
def projects_show(name: str = typer.Argument(...)) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = [r for r in projects_mod.list_projects(conn) if r["name"] == name]
    finally:
        conn.close()
    if not rows:
        console.print(f"[yellow]no project named {name!r}[/yellow]")
        raise typer.Exit(code=1)
    from rich.markup import escape

    table = Table(title=f"project: {escape(name)}")
    table.add_column("id")
    table.add_column("platform")
    table.add_column("kind")
    table.add_column("convs", justify="right")
    table.add_column("path")
    for r in rows:
        table.add_row(str(r["id"]), r["platform"], r["kind"], str(r["convs"]), escape(r["path"]) if r["path"] else "—")
    console.print(table)


@projects_app.command("review")
def projects_review() -> None:
    """Everything flagged: ambiguous names (same name, different path) and
    scratch buckets ('(no workspace)'). Flagged, never silently resolved."""
    config = load_config()
    conn = connect(config)
    try:
        rows = projects_mod.review(conn)
    finally:
        conn.close()
    if not rows:
        console.print("[dim]nothing flagged[/dim]")
        return
    from rich.markup import escape

    table = Table(title="projects review")
    table.add_column("id")
    table.add_column("platform")
    table.add_column("kind")
    table.add_column("project")
    table.add_column("convs", justify="right")
    table.add_column("path")
    for r in rows:
        flag = "⚠ " if r["ambiguous"] else ""
        table.add_row(str(r["id"]), r["platform"], r["kind"], f"{flag}{escape(r['name'])}", str(r["convs"]), escape(r["path"]) if r["path"] else "—")
    console.print(table)


@projects_app.command("merge")
def projects_merge_cmd(source_id: int = typer.Argument(...), target_id: int = typer.Argument(...)) -> None:
    """Moves every conversation from `source_id` onto `target_id`, then
    deletes the source project row. Your decision — never automatic."""
    config = load_config()
    conn = connect(config)
    try:
        try:
            n = projects_mod.merge(conn, source_id, target_id)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"merged project {source_id} into {target_id}: {n} conversation(s) moved")


@projects_app.command("rename")
def projects_rename_cmd(project_id: int = typer.Argument(...), new_name: str = typer.Argument(...)) -> None:
    config = load_config()
    conn = connect(config)
    try:
        try:
            projects_mod.rename(conn, project_id, new_name)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    console.print(f"renamed project {project_id} -> {new_name!r}")


@app.command()
def screen() -> None:
    """The tagging screen (Tagging Workflow doc §3) — search, multi-select,
    click a tag in the tree to apply it to everything selected. Keyboard:
    / search, t/Enter apply a tag, u untag (tree cursor), z undo, a/n
    select all/none, Enter on a result reads it (Escape returns), s saves
    the current filters as a view, q quits."""
    from chatvault import screen as screen_mod

    config = load_config()
    conn = connect(config)
    try:
        screen_mod.run(conn)
    finally:
        conn.close()


@app.command()
def embed(
    force: bool = typer.Option(False, "--force", help="Re-embed even conversations whose text hasn't changed."),
    status: bool = typer.Option(False, "--status", help="Show embedded/pending counts without embedding anything."),
) -> None:
    """Backfill semantic-search embeddings over the corpus (spec: 'runs once
    over the full existing corpus, then incrementally on new ingests' — this
    is the once; ingest handles the incrementally). Safe to interrupt and
    re-run: already-embedded, unchanged conversations are skipped."""
    config = load_config()
    conn = connect(config)
    try:
        if status:
            data = embeddings_mod.status(conn)
            console.print(
                f"embedded: {data['embedded']}  pending: {data['pending']}  "
                f"unembeddable: {data['unembeddable']}  total: {data['total']}"
            )
            console.print(f"[dim]stale (superseded/excluded, safe to ignore): {data['stale']}[/dim]")
            return

        result = embeddings_mod.index_conversations(conn, force=force)
    finally:
        conn.close()

    console.print(f"embedded: {result.embedded}  skipped: {result.skipped}  failed: {result.failed}")
    if result.unavailable:
        console.print(
            "[red]Ollama wasn't reachable — stopped early. "
            "Run `ollama serve` (and `ollama pull nomic-embed-text` if needed), then re-run `chatvault embed`.[/red]"
        )
        raise typer.Exit(code=1)


@app.command("portfolio-analyze")
def portfolio_analyze(
    where: str = typer.Option(None, "--where", help="SQL predicate over v_conversations — analyze a subset instead of everything."),
    pick: str = typer.Option(None, "--pick", help="Row number(s) from the last `find` — analyze just those."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute and report without writing any tags."),
    report: Path = typer.Option(None, "--report", help="Also write a human-readable markdown summary to this path."),
    source: str = typer.Option("llm", "--source", help="tag_links.source for everything this writes. Keep it 'llm' unless you have a specific reason not to — `tag untag --source <this>` is how the whole run gets undone in one shot."),
) -> None:
    """Port of technical_portfolio_extractor.py + reasoning_profile_analyzer.py
    (Build Order Step 8) — tags conversations by technical classification,
    LLM-testing evidence, two independent reasoning-pattern sets, and skill
    validation. Same keyword scoring as the originals, unchanged; input is
    the database instead of a directory of markdown files, output is tags
    instead of a JSON snapshot. Conversation-level only, by design — a
    keyword-matching heuristic shouldn't claim more precision than that.

    Every tag lands with --source (default 'llm'), so the entire run is
    reversible in one command regardless of how many distinct tag names it
    touched: `chatvault tag untag --source llm`."""
    config = load_config()
    conn = connect(config)
    try:
        conv_ids = None
        if pick is not None:
            try:
                conv_ids = browse_mod.resolve_pick(conn, config, pick)
            except (browse_mod.StaleCacheError, ValueError) as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1)
        elif where is not None:
            conv_ids = [r[0] for r in conn.execute(f"SELECT id FROM v_conversations WHERE {where}").fetchall()]  # noqa: S608

        result = portfolio_mod.run(conn, conv_ids=conv_ids, dry_run=dry_run, source=source)

        if report:
            report.write_text(portfolio_mod.render_report(result))
    finally:
        conn.close()

    console.print(f"scanned {result.conversations_scanned} conversation(s) — {result.technical_conversations} technical")
    console.print(
        f"{'would apply' if dry_run else 'applied'} {result.tag_links_written} tag application(s) "
        f"across {len(result.tag_conv_ids)} distinct tag name(s)"
    )
    if dry_run:
        console.print("[dim]--dry-run: nothing was written[/dim]")
    else:
        console.print(f"[dim]undo the whole run: chatvault tag untag --source {source}[/dim]")
    if report:
        console.print(f"wrote {report}")


if __name__ == "__main__":
    app()

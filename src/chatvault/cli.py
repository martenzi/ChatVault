from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from chatvault import curation
from chatvault import doctor as doctor_mod
from chatvault import export as export_mod
from chatvault import graph as graph_mod
from chatvault import ingest as ingest_mod
from chatvault import inventory as inventory_mod
from chatvault import search as search_mod
from chatvault import verify as verify_mod
from chatvault.config import load_config
from chatvault.db import connect, migrate

app = typer.Typer(no_args_is_help=True, add_completion=False)
tag_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Manage tags.")
view_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Manage saved views.")
graph_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Neo4j projection layer.")
app.add_typer(tag_app, name="tag")
app.add_typer(view_app, name="view")
app.add_typer(graph_app, name="graph")
console = Console()


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


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

    applied = migrate(config)
    if applied:
        console.print(f"[green]applied migrations:[/green] {', '.join(applied)}")
    else:
        console.print("[green]schema already up to date[/green]")
    console.print(f"database: {config.db_path}")


@app.command()
def inventory(
    directory: Path = typer.Argument(..., exists=True, file_okay=False, help="Directory to scan (read-only)."),
    deep: bool = typer.Option(False, "--deep", help="Parse file contents fully for accurate conversation counts."),
) -> None:
    """Scan a directory, detect platform + counts. Makes NO writes."""
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
    path: Path = typer.Argument(..., exists=True, help="File or directory to ingest."),
    adapter: str = typer.Option(None, "--adapter", help="Force a specific adapter instead of auto-detecting."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and report without writing to the database."),
) -> None:
    """Ingest RAW export file(s) into SQLite. Idempotent — safe to re-run."""
    config = load_config()
    conn = connect(config)
    try:
        summary = ingest_mod.ingest_path(conn, path, adapter_name=adapter, dry_run=dry_run)
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
        n = curation.restore(conn, target, where)
    finally:
        conn.close()
    console.print(f"restored {n} {target}(s)")


@app.command()
def dedupe(
    report: bool = typer.Option(False, "--report", help="Print superseded / flagged-duplicate / repeated-block summary."),
    apply: bool = typer.Option(False, "--apply", help="Not yet interactive — use `chatvault exclude` for level 4 (repeated blocks)."),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        data = curation.dedupe_report(conn)
    finally:
        conn.close()

    console.print(f"[bold]superseded (level 3):[/bold] {len(data['superseded'])}")
    for r in data["superseded"]:
        console.print(f"  id={r['id']} uid={r['uid']} -> superseded_by={r['superseded_by']}")

    console.print(f"[bold]flagged duplicates (ambiguous, needs review):[/bold] {len(data['flagged_duplicates'])}")
    for r in data["flagged_duplicates"]:
        console.print(f"  id={r['id']} uid={r['uid']}")

    console.print(f"[bold]repeated blocks (level 4, >20 occurrences — review before excluding):[/bold] {len(data['repeated_blocks'])}")
    for r in data["repeated_blocks"][:20]:
        console.print(f"  hash={r['content_hash'][:12]}… count={r['n']}")

    if apply:
        console.print(
            "[yellow]--apply only acts on levels 1-3, which already happen automatically during ingest. "
            "Level 4 (repeated blocks) is always a deliberate `chatvault exclude --target block --where \"content_hash='...'\"` call.[/yellow]"
        )


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
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    config = load_config()
    conn = connect(config)
    try:
        rows = search_mod.stats(conn, by)
    finally:
        conn.close()

    if as_json:
        import json as _json

        console.print(_json.dumps([dict(r) for r in rows], indent=2))
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


if __name__ == "__main__":
    app()

from __future__ import annotations

import json
import os
import pydoc
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from chatvault import brain as brain_mod
from chatvault import brain_db
from chatvault import export as export_mod
from chatvault.config import load_config

# Second Brain, Layer 2+ (docs/45_SPEC_brain_v2_full.md). Command grammar is
# `brain <verb> <noun>`, without exception — §0/§7 acceptance: there is no
# bare `brain add`. Typer models this as verb sub-Typers whose commands are
# the nouns ("add entry", "add tag", ...); a verb with exactly one possible
# noun (find, tree, pick, variants, due, stale, orphans, verify, backup,
# promote) stays a flat top-level command, matching the spec's own listing
# in §3, which never writes e.g. "brain find query" with a noun either.

console = Console()

brain_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Second Brain v2 — a portable, curated personal knowledge base (docs/45_SPEC_brain_v2_full.md).")

add_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Add a new entry, tag, alias, collection, rel type, or kind.")
add_to_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Add item(s) to a collection.")
edit_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Edit an entry or a kind's description.")
show_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Show an entry or a collection.")
list_app = typer.Typer(no_args_is_help=True, add_completion=False, help="List entries, tags, aliases, collections, links, or kinds.")
remove_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Remove (hard-delete) an entry, a kind, or an alias.")
remove_from_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Remove an item from a collection.")
archive_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Archive an entry.")
restore_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Restore an archived or excluded entry.")
exclude_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Soft-hide an entry from views without marking it done (see `archive` for that).")
tag_verb_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Attach tag(s) to an entry.")
untag_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Remove tag(s) from an entry.")
rename_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Rename a tag.")
merge_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Merge two tags into one.")
link_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Link one entry to another with a named relationship.")
unlink_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Remove a link between two entries.")
review_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Schedule an entry for future review.")
stats_app = typer.Typer(no_args_is_help=False, add_completion=False, invoke_without_command=True, help="Counts by kind/tag/collection/confidence, or `stats edits`/`stats usage`.")
import_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Bulk-import markdown, YAML, or CSV into the Brain.")
view_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Manage saved queries against brain.sqlite.")
tag_rule_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Automatic tagging rules, applied on `add entry`/`accept`.")
curation_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Export/import/diff the curation layer (tags, links, collections, notes, exclusions, views).")
attachments_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Attach and browse local files on a Brain entry.")
graph_app = typer.Typer(
    no_args_is_help=True, add_completion=False,
    help="Neo4j projection layer, built but not run against a real instance (docs/51_SPEC_brain_parity_and_naming.md §7 non-goal).",
)
copy_app = typer.Typer(
    no_args_is_help=True, add_completion=False,
    help="Copy a conversation from ChatVault into the Brain as a frozen snapshot. Scripted: no AI model is used.",
)
summarise_app = typer.Typer(
    no_args_is_help=True, add_completion=False,
    help="Ask a local language model to read a conversation or Brain entry and write structured notes from it (facts, decisions, methods). This runs an AI model — it will say which one before it starts. Output is the model's writing, not an extract of yours; review it.",
)

add_app.add_typer(add_to_app, name="to")
remove_app.add_typer(remove_from_app, name="from")
_GRAMMAR_PANEL = "Core grammar (verb + noun — try `brain add --help`)"
brain_app.add_typer(add_app, name="add", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(edit_app, name="edit", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(show_app, name="show", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(list_app, name="list", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(remove_app, name="remove", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(archive_app, name="archive", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(restore_app, name="restore", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(exclude_app, name="exclude", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(tag_verb_app, name="tag", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(untag_app, name="untag", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(rename_app, name="rename", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(merge_app, name="merge", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(link_app, name="link", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(unlink_app, name="unlink", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(review_app, name="review", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(stats_app, name="stats", rich_help_panel=_GRAMMAR_PANEL)
brain_app.add_typer(import_app, name="import", rich_help_panel=_GRAMMAR_PANEL)

# docs/51_SPEC_brain_parity_and_naming.md §2.1 batch B — dedupe/view/
# curation/attachments/tag rule/graph. `tag rule` nests under the existing
# `tag` verb sub-Typer, matching ChatVault's own `tag rule` shape.
tag_verb_app.add_typer(tag_rule_app, name="rule")
brain_app.add_typer(view_app, name="view", rich_help_panel="Curation & Data Quality")
brain_app.add_typer(curation_app, name="curation", rich_help_panel="Curation & Data Quality")
brain_app.add_typer(attachments_app, name="attachments", rich_help_panel="Curation & Data Quality")
brain_app.add_typer(graph_app, name="graph", rich_help_panel="Neo4j Graph (built, not run — see --help)")

# docs/51_SPEC_brain_parity_and_naming.md §1 — `promote`/`distill` renamed to
# name what they actually do (a scripted copy vs. a model writing prose),
# and split into verb+noun sub-Typers because each verb now has two nouns
# (conversation/stashed, conversation/entry). `promote`/`distill` themselves
# stay registered below as hidden top-level aliases that print a
# deprecation note to stderr and delegate to the exact same logic.
_AI_PANEL = "AI-assisted (a local language model writes the output)"
brain_app.add_typer(copy_app, name="copy", rich_help_panel="Bringing Content from ChatVault")
brain_app.add_typer(summarise_app, name="summarise", rich_help_panel=_AI_PANEL)
# "summarise" is the primary (British-ish) spelling; "summarize" is a
# visible alias, not hidden — both are first-class per §8 Q2, unlike the
# promote/distill/brief renames themselves, which keep their old names
# working only as hidden deprecation shims.
brain_app.add_typer(summarise_app, name="summarize", rich_help_panel=_AI_PANEL)


@contextmanager
def _conn():
    config = load_config()
    conn = brain_db.connect(config)
    try:
        yield conn, config
    finally:
        conn.close()


def _fail(exc: Exception) -> None:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1)


def _split_csv(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _edit_text(initial: str = "") -> str:
    """--body-editor: opens $EDITOR (falling back to vi) on a temp file and
    returns what comes back. Deliberately stdlib-only (tempfile + subprocess)
    rather than click.edit()/click.echo_via_pager() — this codebase's typer
    (0.27.1) vendors click internally as typer._click, so the top-level
    `click` package isn't actually installed. The repo's pager code paths
    (`cli.py::_page`, `_page` below) use stdlib pydoc.pager for the same
    reason — see 02_DECISIONS.md D15."""
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as fh:
        fh.write(initial)
        path = fh.name
    try:
        subprocess.call([editor, path])
        return Path(path).read_text()
    finally:
        Path(path).unlink(missing_ok=True)


def _page(text: str) -> None:
    if sys.stdout.isatty():
        pydoc.pager(text)
    else:
        console.print(text)


def _resolve_conv_ref(vault_conn, config, ref: str) -> str:
    """Resolves `--from-conv <n-or-title>` to a vault conversation uid —
    the addressing `brain distill` actually exposes to a human. Never a
    bare uid: a row number means 'row N of the most recent `chatvault find`
    result' (the same --pick cache `brain promote --pick`/`stash add
    --pick` already use), and anything else is matched as a case-
    insensitive title substring. The uid itself only ever ends up stored
    internally (items.source_uid) for provenance — it's not something this
    function's caller ever has to type."""
    from chatvault import browse as browse_mod

    if ref.strip().isdigit():
        try:
            conv_ids = browse_mod.resolve_pick(vault_conn, config, ref)
        except (browse_mod.StaleCacheError, ValueError) as exc:
            raise brain_mod.BrainError(str(exc)) from exc
        if len(conv_ids) != 1:
            raise brain_mod.BrainError("--from-conv takes exactly one row number")
        row = vault_conn.execute("SELECT uid FROM v_conversations WHERE id = ?", (conv_ids[0],)).fetchone()
        return row["uid"]

    matches = vault_conn.execute(
        "SELECT uid, title FROM v_conversations WHERE title LIKE ? ORDER BY started_at DESC LIMIT 10",
        (f"%{ref}%",),
    ).fetchall()
    if not matches:
        raise brain_mod.BrainError(f"no conversation title matches {ref!r} — try `chatvault find` and pass a row number instead")
    if len(matches) > 1:
        listing = "\n".join(f"  - {m['title']}" for m in matches)
        raise brain_mod.BrainError(f"{len(matches)} conversations match {ref!r}, be more specific:\n{listing}")
    return matches[0]["uid"]


# ═══════════════════════════════ entries ═══════════════════════════════


@add_app.command("entry")
def add_entry_cmd(
    title: str = typer.Argument(...),
    why: str = typer.Option(..., "--why"),
    kind: str = typer.Option("note", "--kind"),
    ref: str = typer.Option(None, "--ref"),
    parent: str = typer.Option(None, "--parent", help="ref or id of the parent entry."),
    rank: int = typer.Option(None, "--rank", help="Preference among siblings under the same parent. 1 = first choice. Not a quality score."),
    body: str = typer.Option(None, "--body"),
    body_file: Path = typer.Option(None, "--body-file"),
    body_editor: bool = typer.Option(False, "--body-editor"),
    tag: list[str] = typer.Option([], "--tag"),
    alias: list[str] = typer.Option([], "--alias"),
    meta: str = typer.Option(None, "--meta", help='JSON, e.g. \'{"length":"short"}\'.'),
    set_: list[str] = typer.Option([], "--set", help="key=value, repeatable. Applied after --meta."),
    collection: str = typer.Option(None, "--collection"),
    confidence: str = typer.Option(None, "--confidence"),
    sensitivity: str = typer.Option("normal", "--sensitivity", help="normal | personal | secret."),
    url: str = typer.Option(None, "--url"),
    vetted: bool = typer.Option(False, "--vetted", help="Sets vetted_at = now."),
) -> None:
    """Creates one curated entry — the primary way anything lasting gets
    into the Brain. Single-target, not bulk (see `import` for bulk).
    --why is mandatory; everything else is optional and freely editable
    later with `edit entry`. Prints one status line."""
    if body_editor:
        body = _edit_text()
    elif body_file is not None:
        body = body_file.read_text()
    with _conn() as (conn, _config):
        try:
            result = brain_mod.add_entry(
                conn, title, why, kind=kind, ref=ref, parent=parent, rank=rank, body=body,
                tags=tag, aliases=alias, meta_json=meta, set_pairs=set_, collection=collection,
                confidence=confidence, sensitivity=sensitivity, url=url, vetted=vetted,
            )
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]added[/green] entry {result['item_id']} — {result['title']}")


# docs/51_SPEC_brain_parity_and_naming.md §2.1 `note` row.
@add_app.command("note")
def add_note_cmd(ref: str = typer.Argument(...), text: str = typer.Argument(..., help="The note text.")) -> None:
    """Appends free-form running commentary to an entry — distinct from
    `why` (one, mandatory, set once at creation) and from `history`
    (records field edits, not standalone remarks). Scripted: no AI model is
    used. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.add_note(conn, ref, text)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]noted[/green] on entry {result['item_id']} — {result['title']}")


@edit_app.command("entry")
def edit_entry_cmd(
    ref: str = typer.Argument(...),
    title: str = typer.Option(None, "--title"),
    why: str = typer.Option(None, "--why"),
    body_editor: bool = typer.Option(False, "--body-editor"),
    rank: int = typer.Option(None, "--rank", help="Preference among siblings. 1 = first choice."),
    kind: str = typer.Option(None, "--kind"),
    set_: list[str] = typer.Option([], "--set"),
) -> None:
    """Changes one or more fields on an existing entry. Only what you pass
    changes; each changed field writes one `history` row and bumps
    `edit_count` — the full edit trail is downstream of this command, not
    something you manage separately. Prints one status line."""
    with _conn() as (conn, _config):
        body = None
        if body_editor:
            try:
                current = brain_mod.get_item_row(conn, ref)
            except brain_mod.BrainError as exc:
                _fail(exc)
            body = _edit_text(current["body"] or "")
        try:
            result = brain_mod.edit_entry(conn, ref, title=title, why=why, body=body, rank=rank, kind=kind, set_pairs=set_)
        except brain_mod.BrainError as exc:
            _fail(exc)
    if not result["changed"]:
        console.print("[dim]nothing changed[/dim]")
    else:
        console.print(f"[green]edited[/green] entry {result['item_id']}: {', '.join(result['changed'])}")


@show_app.command("entry")
def show_entry_cmd(
    ref: str = typer.Argument(...),
    with_: str = typer.Option(None, "--with", help="Comma-separated: children,links,history,attachments,notes"),
) -> None:
    """Reads one entry in full. Marks it used (`use_count`/`last_used_at`),
    which is what makes `stale`/`stats usage` meaningful — so `show` isn't
    just read-only browsing, it's also the signal the Brain uses to know
    what you actually reach for. Pages the full text (or prints raw if piped)."""
    with _conn() as (conn, _config):
        try:
            row = brain_mod.get_item_row(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
        brain_mod.touch_used(conn, row["id"])

        parts = [
            f"ref: {row['ref'] or '-'}   kind: {row['kind']}   status: {row['status']}",
            f"why: {row['why']}",
            f"confidence: {row['confidence'] or '-'}   sensitivity: {row['sensitivity']}",
            f"rank: {row['rank'] if row['rank'] is not None else '-'} (preference among siblings, 1 = first choice)",
            f"saved: {row['created_at']}   vetted: {row['vetted_at'] or '-'}   used: {row['use_count']}x   edits: {row['edit_count']}",
            "-" * 40,
            f"# {row['title']}",
            "",
            row["body"] or "(no body)",
        ]

        with_parts = _split_csv(with_)
        if "children" in with_parts:
            kids = brain_mod.variants(conn, ref)
            parts.append("\nchildren:")
            for k in kids:
                parts.append(f"  [{k['rank'] or '-'}] {k['title']} ({k['ref'] or k['id']})")
        if "links" in with_parts:
            links = [l for l in brain_mod.list_links(conn) if l["from_id"] == row["id"] or l["to_id"] == row["id"]]
            parts.append("\nlinks:")
            for l in links:
                parts.append(f"  {l['from_title']} --{l['rel']}--> {l['to_title']}")
        if "history" in with_parts:
            hist = conn.execute("SELECT * FROM history WHERE item_id=? ORDER BY changed_at", (row["id"],)).fetchall()
            parts.append("\nhistory:")
            for h in hist:
                parts.append(f"  {h['changed_at']} [{h['origin']}] {h['field']}: {h['old_value']!r} -> {h['new_value']!r}")
        if "attachments" in with_parts:
            atts = conn.execute("SELECT * FROM attachments WHERE item_id=?", (row["id"],)).fetchall()
            parts.append("\nattachments:")
            for a in atts:
                parts.append(f"  {a['filename']} ({a['bytes']} bytes)")
        if "notes" in with_parts:
            note_rows = brain_mod.list_notes(conn, ref)
            parts.append("\nnotes:")
            for n in note_rows:
                parts.append(f"  {n['created_at']}: {n['text']}")

    _page("\n".join(parts))


@list_app.command("entries")
def list_entries_cmd(
    kind: str = typer.Option(None, "--kind"),
    tag: str = typer.Option(None, "--tag"),
    parent: str = typer.Option(None, "--parent"),
    collection: str = typer.Option(None, "--collection"),
    length: str = typer.Option(None, "--length"),
    lang: str = typer.Option(None, "--lang"),
    confidence: str = typer.Option(None, "--confidence"),
    sensitivity: str = typer.Option(None, "--sensitivity"),
    rank: int = typer.Option(None, "--rank"),
) -> None:
    """The full Brain, newest first, with every filter optional — the daily
    browsing surface. Exploratory (no arguments needed); row numbers are
    fresh every call for use with any ref/alias/row-number-taking command. Prints a table."""
    with _conn() as (conn, _config):
        entries = brain_mod.list_entries(
            conn, kind=kind, tag=tag, parent=parent, collection=collection, length=length,
            lang=lang, confidence=confidence, sensitivity=sensitivity, rank=rank,
        )
    if not entries:
        console.print("[dim]brain is empty[/dim]")
        return
    table = Table(title="brain entries")
    table.add_column("#", justify="right")
    table.add_column("ref")
    table.add_column("kind")
    table.add_column("title")
    table.add_column("why")
    table.add_column("rank", justify="right")
    table.add_column("saved")
    for e in entries:
        table.add_row(str(e.n), e.ref or "-", e.kind, e.title, e.why, str(e.rank) if e.rank is not None else "-", e.created_at[:10])
    console.print(table)


@brain_app.command("find", rich_help_panel="Search & Retrieval")
def find_cmd(
    query: str = typer.Argument(...),
    kind: str = typer.Option(None, "--kind"),
    tag: str = typer.Option(None, "--tag"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """FTS over title, why, body, ref, aliases, tags — keyword only, never
    semantic. Verified against the real ChatVault CLI before naming this:
    `chatvault find` (src/chatvault/cli.py) has no `--semantic` option at
    all and never has — it's `title LIKE` + `blocks_fts MATCH`
    (src/chatvault/browse.py's find()), full stop. `brain find` matches
    that exactly; `brain search --semantic` (below) is where the semantic
    mode lives, matching where it actually lives in ChatVault. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.find_entries(conn, query, kind=kind, tag=tag, limit=limit)
    if not rows:
        console.print("[dim]no matches[/dim]")
        return
    table = Table(title=f"brain find: {query!r}")
    table.add_column("id", justify="right")
    table.add_column("ref")
    table.add_column("kind")
    table.add_column("title")
    table.add_column("why")
    for r in rows:
        table.add_row(str(r["id"]), r["ref"] or "-", r["kind"], r["title"], r["why"])
    console.print(table)


@brain_app.command("search", rich_help_panel="Search & Retrieval")
def search_cmd(
    query: str = typer.Argument(...),
    kind: str = typer.Option(None, "--kind"),
    limit: int = typer.Option(20, "--limit"),
    semantic: bool = typer.Option(False, "--semantic", help="Search by meaning (local Ollama + nomic-embed-text) instead of exact words. Needs `brain embed` to have run first."),
) -> None:
    """Keyword by default, --semantic switches to meaning-based search —
    exactly ChatVault's own `search`/`search --semantic` split
    (src/chatvault/cli.py's `search` command: keyword over blocks by
    default, `embeddings_mod.semantic_search` under `--semantic`). The
    `--semantic` flag lives on `search` in both tools now, not on `find` —
    confirmed from the real ChatVault code, not assumed from either of our
    memories of it. Prints a table."""
    with _conn() as (conn, _config):
        if semantic:
            try:
                rows = brain_mod.semantic_find(conn, query, limit=limit)
            except brain_mod.EmbeddingUnavailable as exc:
                _fail(exc)
            if kind is not None:
                rows = [r for r in rows if r["kind"] == kind]
        else:
            rows = brain_mod.find_entries(conn, query, kind=kind, limit=limit)
    if not rows:
        console.print("[dim]no matches[/dim]")
        return
    table = Table(title=f"brain search{' --semantic' if semantic else ''}: {query!r}")
    table.add_column("id", justify="right")
    table.add_column("ref")
    table.add_column("kind")
    table.add_column("title")
    table.add_column("why")
    for r in rows:
        table.add_row(str(r["id"]), r["ref"] or "-", r["kind"], r["title"], r["why"])
    console.print(table)


@brain_app.command("embed", rich_help_panel="Search & Retrieval")
def embed_cmd(
    force: bool = typer.Option(False, "--force", help="Re-embed even items whose text hasn't changed."),
    status: bool = typer.Option(False, "--status", help="Show embedded/pending counts without embedding anything."),
) -> None:
    """Backfill semantic-search embeddings over the Brain — same command
    shape as `chatvault embed`. Safe to interrupt and re-run: already-
    embedded, unchanged items are skipped. Prints status lines."""
    with _conn() as (conn, _config):
        if status:
            data = brain_mod.embed_status(conn)
            console.print(
                f"embedded: {data['embedded']}  pending: {data['pending']}  "
                f"unembeddable: {data['unembeddable']}  total: {data['total']}"
            )
            console.print(f"[dim]stale (archived since embedding, safe to ignore): {data['stale']}[/dim]")
            return
        result = brain_mod.index_entries(conn, force=force)
    console.print(f"embedded: {result.embedded}  skipped: {result.skipped}  failed: {result.failed}")
    if result.unavailable:
        console.print(
            "[red]Ollama wasn't reachable — stopped early. "
            "Run `ollama serve` (and `ollama pull nomic-embed-text` if needed), then re-run `brain embed`.[/red]"
        )
        raise typer.Exit(code=1)


@brain_app.command("tree", rich_help_panel="Search & Retrieval")
def tree_cmd(ref: str = typer.Argument(..., help="An entry ref, alias, or row number — or the literal word 'tags'.")) -> None:
    """Entry + descendants (parent_id chain), ranked — or, given the literal
    word 'tags', the tag hierarchy instead. Two different trees behind one
    command; requires an argument either way. Prints a table."""
    with _conn() as (conn, _config):
        if ref == "tags":
            rows = brain_mod.tree_tags(conn)
            table = Table(title="brain tags")
            table.add_column("tag")
            table.add_column("items", justify="right")
            for r in rows:
                table.add_row(("  " * r["depth"]) + r["name"].rsplit("/", 1)[-1], str(r["item_count"]))
            console.print(table)
            return
        try:
            rows = brain_mod.tree_entry(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    table = Table(title=f"brain tree: {ref}")
    table.add_column("depth", justify="right")
    table.add_column("rank", justify="right")
    table.add_column("kind")
    table.add_column("title")
    for r in rows:
        table.add_row(str(r["depth"]), str(r["rank"]) if r["rank"] is not None else "-", r["kind"], r["title"])
    console.print(table)


@brain_app.command("screen", rich_help_panel="Search & Retrieval")
def screen_cmd() -> None:
    """docs/51_SPEC_brain_parity_and_naming.md §2.1 `screen` row: a Textual
    TUI for browsing the Brain — filter/search entries, click a tag in the
    tree to filter by it, read one entry's full detail (tags, links,
    history, collections), and follow links between entries. Read-only: no
    tagging/archiving/editing happens here, those stay CLI acts.

    Operates on brain.sqlite alone (§8 Q8: "does brain screen need to reach
    the vault? No.") — never opens a ChatVault connection; `copy
    conversation`/`copy stashed` stay CLI-only acts. Keyboard: / search, c
    clears filters, click (or Enter with the tree focused) a tag to filter
    by it, Enter/r reads the highlighted entry; inside the read pane, Enter
    on a link follows it and Escape steps back one link at a time before
    returning to the list, q quits. Full-screen interactive TUI, not a
    one-shot printout."""
    from chatvault import brain_screen

    config = load_config()
    conn = brain_db.connect(config)
    try:
        brain_screen.run(conn)
    finally:
        conn.close()


@brain_app.command("pick", rich_help_panel="Search & Retrieval")
def pick_cmd(
    ref: str = typer.Argument(...),
    length: str = typer.Option(None, "--length"),
    audience: str = typer.Option(None, "--audience"),
) -> None:
    """Best-match single result among a parent entry's children, ranked (1 = first choice). Prints raw text to stdout, pipeable."""
    with _conn() as (conn, _config):
        try:
            row = brain_mod.pick(conn, ref, length=length, audience=audience)
        except brain_mod.BrainError as exc:
            _fail(exc)
        brain_mod.touch_used(conn, row["id"])
    console.print(row["body"] or row["title"])


@brain_app.command("variants", rich_help_panel="Search & Retrieval")
def variants_cmd(ref: str = typer.Argument(...)) -> None:
    """All of one entry's children side by side, ranked — see every phrasing of
    a claim at once, instead of `pick`'s single best match. Prints a table."""
    with _conn() as (conn, _config):
        try:
            rows = brain_mod.variants(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    if not rows:
        console.print("[dim]no children[/dim]")
        return
    table = Table(title=f"brain variants: {ref}")
    table.add_column("rank", justify="right")
    table.add_column("title")
    table.add_column("why")
    for r in rows:
        table.add_row(str(r["rank"]) if r["rank"] is not None else "-", r["title"], r["why"])
    console.print(table)


@archive_app.command("entry")
def archive_entry_cmd(ref: str = typer.Argument(...)) -> None:
    """Soft-removes one entry — hidden from `list`/`find`/`export`, but the
    row and its history stay intact. Reversible with `restore entry`. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.archive_entry(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]archived[/green] entry {result['item_id']} — {result['title']}")


@restore_app.command("entry")
def restore_entry_cmd(ref: str = typer.Argument(...)) -> None:
    """Undoes `archive entry` and/or `exclude entry` — whichever hidden
    state applied, `restore` brings the entry back into `list`/`find`/
    `export`. Not the counterpart to `remove entry` (a hard delete has no
    restore path). Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.restore_entry(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]restored[/green] entry {result['item_id']} — {result['title']}")


# docs/51_SPEC_brain_parity_and_naming.md §2.1 `exclude`/`restore` row (D3
# parity with ChatVault) — deliberately separate from archive/restore
# (§8 Q6): exclude never asserts "this is done."
@exclude_app.command("entry")
def exclude_entry_cmd(ref: str = typer.Argument(...)) -> None:
    """Soft-hides one entry from `list entries`/`find`/`export`, without
    asserting it's done — that's what `archive entry` means instead.
    Reversible with `restore entry`. Scripted: no AI model is used. Prints
    one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.exclude_entry(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]excluded[/green] entry {result['item_id']} — {result['title']}")


@remove_app.command("entry")
def remove_entry_cmd(ref: str = typer.Argument(...), confirm: bool = typer.Option(False, "--confirm")) -> None:
    """The only hard delete in the Brain. Backs up brain.sqlite first. Prints one status line."""
    with _conn() as (conn, config):
        try:
            result = brain_mod.remove_entry(conn, ref, confirm=confirm, config=config)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]removed[/green] entry {result['item_id']} — {result['title']}")


# ─────────── copy conversation / copy stashed (née promote) ───────────
# docs/51_SPEC_brain_parity_and_naming.md §1.3/§1.5/§4. Scripted: no AI
# model is used. The actual logic lives in brain.entries.promote /
# promote_from_stash (unrenamed per §1.4/§8 Q1) — these two commands, plus
# the hidden `brain promote` alias below, are thin wrappers around it.


def _print_tag_carryover(result: dict) -> None:
    if result.get("tags_carried"):
        console.print(f"[dim]tags carried from ChatVault: {', '.join(result['tags_carried'])}[/dim]")
    for d in result.get("tags_dropped", []):
        # §4.2/§4.5 — never silently drop a tag; always say so.
        console.print(f"[yellow]tag not carried:[/yellow] {d['tag']!r} — {d['reason']}")


def _copy_conversation(
    uid: str | None, pick: str | None, why: str | None, kind: str, profile: str,
    no_tags: bool, tag: list[str],
) -> None:
    from chatvault import browse as browse_mod
    from chatvault.db import connect as vault_connect

    config = load_config()
    vault_conn = vault_connect(config)
    brain_conn = brain_db.connect(config)
    vault_ro = brain_db.connect_vault_readonly(config.db_path)
    try:
        if uid is None and pick is None:
            console.print("[red]pass --uid or --pick[/red]")
            raise typer.Exit(code=1)
        if pick is not None:
            try:
                conv_ids = browse_mod.resolve_pick(vault_conn, config, pick)
            except (browse_mod.StaleCacheError, ValueError) as exc:
                _fail(exc)
            if len(conv_ids) != 1:
                console.print("[red]`--pick` takes exactly one row[/red]")
                raise typer.Exit(code=1)
            row = vault_conn.execute("SELECT uid FROM v_conversations WHERE id = ?", (conv_ids[0],)).fetchone()
            uid = row["uid"]

        try:
            result = brain_mod.promote(
                brain_conn, vault_ro, vault_conn, uid, why, kind=kind,
                profile=profile, no_tags=no_tags, extra_tags=tag,
            )
        except brain_mod.BrainError as exc:
            _fail(exc)
    finally:
        vault_ro.close()
        brain_conn.close()
        vault_conn.close()
    verb = "already copied as" if result["action"] == "already_promoted" else "copied as"
    console.print(f"[green]{verb}[/green] item {result['item_id']} — {result['title']}")
    _print_tag_carryover(result)


def _copy_stashed(profile: str, no_tags: bool, tag: list[str]) -> None:
    from chatvault.db import connect as vault_connect

    config = load_config()
    vault_conn = vault_connect(config)
    brain_conn = brain_db.connect(config)
    vault_ro = brain_db.connect_vault_readonly(config.db_path)
    try:
        results = brain_mod.promote_from_stash(brain_conn, vault_ro, vault_conn, profile=profile, no_tags=no_tags, extra_tags=tag)
    finally:
        vault_ro.close()
        brain_conn.close()
        vault_conn.close()
    if not results:
        console.print("[dim]nothing to copy — no stash/ items pending[/dim]")
        return
    for r in results:
        verb = "already copied:" if r["action"] == "already_promoted" else "copied:"
        console.print(f"[green]{verb}[/green] item {r['item_id']} — {r['title']}")
        _print_tag_carryover(r)


@copy_app.command("conversation")
def copy_conversation_cmd(
    uid: str = typer.Option(None, "--uid"),
    pick: str = typer.Option(None, "--pick", help="Row number from the last vault `find` (exactly one) — alternative to --uid."),
    why: str = typer.Option(..., "--why"),
    kind: str = typer.Option("conversation", "--kind"),
    profile: str = typer.Option("lean", "--profile", help="read | lean | full — which export.render_profile tier to snapshot the body with."),
    no_tags: bool = typer.Option(False, "--no-tags", help="Don't carry the source conversation's tags over."),
    tag: list[str] = typer.Option([], "--tag", help="Extra tag(s) to add at copy time, on top of any carried over. Repeatable."),
) -> None:
    """Copy a conversation from ChatVault into the Brain as a frozen
    snapshot. Scripted: no AI model is used. The Brain keeps its own copy
    of the text, plus a pointer back to the vault, so the Brain still
    works if the vault is unavailable. Carries the source conversation's
    tags along by default (--no-tags to skip); curation-namespace tags
    (review/*, agent-app:*, chatgpt-account:*, stash/*) are carried in
    prefixed `vault/` so they never collide with your own Brain taxonomy.
    Prints one status line."""
    _copy_conversation(uid, pick, why, kind, profile, no_tags, tag)


@copy_app.command("stashed")
def copy_stashed_cmd(
    profile: str = typer.Option("lean", "--profile", help="read | lean | full — which export.render_profile tier to snapshot each body with."),
    no_tags: bool = typer.Option(False, "--no-tags", help="Don't carry each source conversation's tags over."),
    tag: list[str] = typer.Option([], "--tag", help="Extra tag(s) to add to every copied item. Repeatable."),
) -> None:
    """Copy every conversation you've stashed in ChatVault that isn't in
    the Brain yet. Scripted: no AI model is used. Same tag-carryover
    behaviour as `copy conversation`. Prints one status line per item."""
    _copy_stashed(profile, no_tags, tag)


@brain_app.command("promote", hidden=True)
def promote_cmd(
    uid: str = typer.Option(None, "--uid"),
    pick: str = typer.Option(None, "--pick"),
    why: str = typer.Option(None, "--why", help="Required, unless --from-stash."),
    kind: str = typer.Option("conversation", "--kind"),
    from_stash: bool = typer.Option(False, "--from-stash"),
    profile: str = typer.Option("lean", "--profile"),
    no_tags: bool = typer.Option(False, "--no-tags"),
    tag: list[str] = typer.Option([], "--tag"),
) -> None:
    """Deprecated — use `brain copy conversation` / `brain copy stashed`."""
    if from_stash:
        print("note: 'brain promote --from-stash' is now 'brain copy stashed' — the old name still works for now.", file=sys.stderr)
        _copy_stashed(profile, no_tags, tag)
        return
    print("note: 'brain promote' is now 'brain copy conversation' — the old name still works for now.", file=sys.stderr)
    _copy_conversation(uid, pick, why, kind, profile, no_tags, tag)


# ─────────── summarise conversation / summarise entry (née distill) ───────────
# docs/51_SPEC_brain_parity_and_naming.md §1.2 tier 3/§1.5/§8 Q4. Generative
# AI: hands content to a local language model and lets it write the output —
# lives in its own rich-help panel, must accept --model, must print the
# model name + endpoint before running.


def _announce_model(model: str) -> None:
    console.print(f"[dim]running extraction with {model} via {brain_mod.DISTILL_OLLAMA_URL}, this can take a minute...[/dim]")


def _summarise_conversation(from_conv: str, why: str, model: str, kind: str) -> None:
    from chatvault.db import connect as vault_connect

    config = load_config()
    vault_conn = vault_connect(config)
    brain_conn = brain_db.connect(config)
    vault_ro = brain_db.connect_vault_readonly(config.db_path)
    try:
        try:
            uid = _resolve_conv_ref(vault_conn, config, from_conv)
        except brain_mod.BrainError as exc:
            _fail(exc)
        _announce_model(model)
        try:
            result = brain_mod.distill(brain_conn, vault_ro, uid, why, model=model, kind=kind)
        except (brain_mod.BrainError, brain_mod.DistillUnavailable) as exc:
            _fail(exc)
    finally:
        vault_ro.close()
        brain_conn.close()
        vault_conn.close()

    if result["action"] == "nothing_durable":
        console.print(f"[dim]nothing durable found in {result['title']!r} — no item created[/dim]")
        return
    console.print(f"[green]summarised[/green] entry {result['item_id']} — from {result['title']!r}")


def _summarise_entry(ref: str, why: str, model: str, kind: str) -> None:
    with _conn() as (conn, _config):
        _announce_model(model)
        try:
            result = brain_mod.distill_entry(conn, ref, why, model=model, kind=kind)
        except (brain_mod.BrainError, brain_mod.DistillUnavailable) as exc:
            _fail(exc)

    if result["action"] == "nothing_durable":
        console.print(f"[dim]nothing durable found in {result['title']!r} — no item created[/dim]")
        return
    console.print(f"[green]summarised[/green] entry {result['item_id']} — from {result['title']!r}")


@summarise_app.command("conversation")
def summarise_conversation_cmd(
    from_conv: str = typer.Option(..., "--from-conv", help="Row number from the last `chatvault find` result, or a (unique) conversation title substring. Never a raw uid."),
    why: str = typer.Option(..., "--why"),
    model: str = typer.Option(brain_mod.DISTILL_DEFAULT_MODEL, "--model", help="Local Ollama model to run the extraction with."),
    kind: str = typer.Option("note", "--kind"),
) -> None:
    """Ask a local language model to read a conversation and write
    structured notes from it (facts, decisions, methods). This runs an AI
    model — it will say which one before it starts. Output is the model's
    writing, not an extract of yours; review it. Prints one status line,
    after a live model call."""
    _summarise_conversation(from_conv, why, model, kind)


@summarise_app.command("entry")
def summarise_entry_cmd(
    ref: str = typer.Argument(..., help="An entry ref, alias, or row number."),
    why: str = typer.Option(..., "--why"),
    model: str = typer.Option(brain_mod.DISTILL_DEFAULT_MODEL, "--model", help="Local Ollama model to run the extraction with."),
    kind: str = typer.Option("note", "--kind"),
) -> None:
    """Ask a local language model to read an *existing Brain entry* (its
    why + body) and write structured notes from it — same AI-assisted act
    as `summarise conversation`, run over something already in the Brain
    instead of a vault conversation. This runs an AI model — it will say
    which one before it starts. Prints one status line, after a live model call."""
    _summarise_entry(ref, why, model, kind)


@brain_app.command("distill", hidden=True)
def distill_cmd(
    from_conv: str = typer.Option(..., "--from-conv"),
    why: str = typer.Option(..., "--why"),
    model: str = typer.Option(brain_mod.DISTILL_DEFAULT_MODEL, "--model"),
    kind: str = typer.Option("note", "--kind"),
) -> None:
    """Deprecated — use `brain summarise conversation`."""
    print("note: 'brain distill' is now 'brain summarise conversation' — the old name still works for now.", file=sys.stderr)
    _summarise_conversation(from_conv, why, model, kind)


# ═══════════════════════════ tags / aliases / kinds ═══════════════════════════


@add_app.command("tag")
def add_tag_cmd(name: str = typer.Argument(...), description: str = typer.Option(None, "--description")) -> None:
    """Creates a tag with no entry attached yet — for pre-building a
    vocabulary. Most of the time you don't need this: `tag entry` creates
    the tag automatically the first time it's used. Prints one status line."""
    with _conn() as (conn, _config):
        result = brain_mod.add_tag(conn, name, description=description)
    console.print(f"[green]{result['action']}[/green] tag {name!r}")


@tag_verb_app.command("entry")
def tag_entry_cmd(ref: str = typer.Argument(...), tags: list[str] = typer.Argument(...)) -> None:
    """Attaches one or more tags to one entry — bulk over tag names, single
    target entry. Creates any tag that doesn't exist yet. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.tag_entry(conn, ref, tags)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]tagged[/green] {ref} with {', '.join(tags)}")


@untag_app.command("entry")
def untag_entry_cmd(
    ref: str = typer.Argument(...),
    tags: list[str] = typer.Argument(None),
    source: str = typer.Option(None, "--source", help="Only remove tag_links written by this source (e.g. 'chatvault' — reverses a whole `copy conversation`/`copy stashed` tag carryover in one command)."),
    prefix: str = typer.Option(None, "--prefix", help="Only remove tags whose name starts with this (e.g. 'vault/')."),
) -> None:
    """The exact inverse of `tag entry` — removes tags from one entry, tag
    names themselves untouched (still exist for other entries). Pass tag
    names, or narrow/replace the selection with --source and/or --prefix.
    Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.untag_entry(conn, ref, tags, source=source, prefix=prefix)
        except brain_mod.BrainError as exc:
            _fail(exc)
    if not result["removed"]:
        console.print(f"[dim]nothing to untag on {ref}[/dim]")
    else:
        console.print(f"[green]untagged[/green] {ref}: removed {', '.join(result['removed'])}")


@list_app.command("tags")
def list_tags_cmd() -> None:
    """Every tag with its item count. Exploratory, no arguments. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.list_tags(conn)
    table = Table(title="brain tags")
    table.add_column("tag")
    table.add_column("items", justify="right")
    for r in rows:
        table.add_row(r["name"], str(r["item_count"]))
    console.print(table)


@rename_app.command("tag")
def rename_tag_cmd(old: str = typer.Argument(...), new: str = typer.Argument(...)) -> None:
    """Renames a tag in place — every entry that carried it keeps carrying
    it, just under the new name. Refuses if `new` already exists (use
    `merge tags` for that case instead). Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.rename_tag(conn, old, new)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]renamed[/green] tag {old!r} -> {new!r}")


@merge_app.command("tags")
def merge_tags_cmd(a: str = typer.Argument(...), b: str = typer.Argument(...)) -> None:
    """Folds tag `a` into tag `b`: every entry tagged `a` becomes tagged
    `b`, then `a` is deleted. For consolidating two names that turned out
    to mean the same thing — the counterpart to `rename tag`. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.merge_tags(conn, a, b)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]merged[/green] {a!r} into {b!r} ({result['item_count']} item(s))")


@add_app.command("alias")
def add_alias_cmd(ref: str = typer.Argument(...), alias: str = typer.Argument(...), note: str = typer.Option(None, "--note")) -> None:
    """Gives an entry another name it can be found by — feeds into `find`
    (FTS indexes aliases too), so 'that npm thing' finds it even if
    neither the title nor body ever says those words. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.add_alias(conn, ref, alias, note=note)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]added alias[/green] {alias!r} to {ref}")


@remove_app.command("alias")
def remove_alias_cmd(ref: str = typer.Argument(...), alias: str = typer.Argument(...)) -> None:
    """Removes one alias from one entry. The entry itself is untouched —
    this is not `remove entry`. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.remove_alias(conn, ref, alias)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]removed alias[/green] {alias!r} from {ref}")


@list_app.command("aliases")
def list_aliases_cmd() -> None:
    """Every alias across the whole Brain, with which entry it belongs to.
    Exploratory, no arguments. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.list_aliases(conn)
    table = Table(title="brain aliases")
    table.add_column("alias")
    table.add_column("entry")
    for r in rows:
        table.add_row(r["alias"], r["title"])
    console.print(table)


@add_app.command("kind")
def add_kind_cmd(name: str = typer.Argument(...), description: str = typer.Option(None, "--description")) -> None:
    """Adds a new entry-kind — kinds are a table you edit, not a hardcoded
    list. A new kind works in `add entry --kind` immediately, zero code
    change. Downstream effect: every `list`/`stats` breakdown by kind picks
    it up automatically. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.add_kind(conn, name, description=description)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]added kind[/green] {name!r}")


@edit_app.command("kind")
def edit_kind_cmd(name: str = typer.Argument(...), description: str = typer.Option(..., "--description")) -> None:
    """Changes a kind's description only — not its name, and doesn't touch
    any entry currently using it. Prints one status line."""
    with _conn() as (conn, _config):
        if not conn.execute("SELECT 1 FROM kinds WHERE name=?", (name,)).fetchone():
            _fail(brain_mod.BrainError(f"no kind named {name!r}"))
        conn.execute("UPDATE kinds SET description=? WHERE name=?", (description, name))
        conn.commit()
    console.print(f"[green]edited[/green] kind {name!r}")


@list_app.command("kinds")
def list_kinds_cmd() -> None:
    """Every kind, builtin and custom, with how many entries use each.
    Exploratory, no arguments. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.list_kinds(conn)
    table = Table(title="brain kinds")
    table.add_column("kind")
    table.add_column("description")
    table.add_column("items", justify="right")
    for r in rows:
        table.add_row(r["name"], r["description"] or "-", str(r["item_count"]))
    console.print(table)


@remove_app.command("kind")
def remove_kind_cmd(name: str = typer.Argument(...), reassign_to: str = typer.Option(None, "--reassign-to")) -> None:
    """Deletes a kind. Refuses if any entry still uses it, unless
    --reassign-to gives a replacement kind for them all — a bulk move, not
    per-entry. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.remove_kind(conn, name, reassign_to=reassign_to)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]removed kind[/green] {name!r}")


# ═══════════════════════════════ collections ═══════════════════════════════


@add_app.command("collection")
def add_collection_cmd(name: str = typer.Argument(...), why: str = typer.Option(..., "--why"), ref: str = typer.Option(None, "--ref")) -> None:
    """Creates an empty, ordered collection — a curated set distinct from
    tags (unordered) and parent_id (single-parent containment). Use
    `add to collection` next to actually put entries in it. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.add_collection(conn, name, why, ref=ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]added[/green] collection {result['collection_id']} — {name}")


@add_to_app.command("collection")
def add_to_collection_cmd(
    coll: str = typer.Argument(...),
    refs: list[str] = typer.Argument(...),
    position: int = typer.Option(None, "--position"),
    note: str = typer.Option(None, "--note"),
) -> None:
    """Adds one or more entries to a collection, at the end unless
    --position is given. Bulk over entries, single target collection —
    an entry can belong to any number of collections at once. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.add_to_collection(conn, coll, refs, position=position, note=note)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]added[/green] {len(refs)} item(s) to collection {coll}")


@remove_from_app.command("collection")
def remove_from_collection_cmd(coll: str = typer.Argument(...), ref: str = typer.Argument(...)) -> None:
    """Removes one entry from one collection. The entry itself is
    untouched — still exists, still in any other collection it's part of. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.remove_from_collection(conn, coll, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]removed[/green] {ref} from collection {coll}")


@list_app.command("collections")
def list_collections_cmd() -> None:
    """Every collection with its item count. Exploratory, no arguments. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.list_collections(conn)
    table = Table(title="brain collections")
    table.add_column("ref")
    table.add_column("name")
    table.add_column("why")
    table.add_column("items", justify="right")
    for r in rows:
        table.add_row(r["ref"] or "-", r["name"], r["why"], str(r["item_count"]))
    console.print(table)


@show_app.command("collection")
def show_collection_cmd(ref: str = typer.Argument(...)) -> None:
    """A collection's members in order — the one place order is visible.
    Reordering (`brain reorder collection`) changes only this output, never
    the entries or any other collection they're in. Prints a table."""
    with _conn() as (conn, _config):
        try:
            coll, items = brain_mod.show_collection(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[bold]{coll['name']}[/bold] — {coll['why']}")
    table = Table()
    table.add_column("#", justify="right")
    table.add_column("title")
    table.add_column("note")
    for i, item in enumerate(items, start=1):
        table.add_row(str(i), item["title"], item["collection_note"] or "-")
    console.print(table)


@brain_app.command("reorder", rich_help_panel="Review & Maintenance")
def reorder_collection_cmd(noun: str = typer.Argument(..., help="Literally 'collection'."), ref: str = typer.Argument(...)) -> None:
    """Interactive: `brain reorder collection` followed by its ref. Interactive — prompts for the new order."""
    if noun != "collection":
        console.print("[red]usage: brain reorder collection, followed by its ref[/red]")
        raise typer.Exit(code=1)
    with _conn() as (conn, _config):
        try:
            _coll, items = brain_mod.show_collection(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
        console.print("current order:")
        for i, item in enumerate(items, start=1):
            console.print(f"  {i}. {item['title']} ({item['ref'] or item['id']})")
        new_order_raw = typer.prompt("new order, as a comma-separated list of the numbers above")
        try:
            positions = [int(x.strip()) for x in new_order_raw.split(",")]
            ordered_refs = [str(items[p - 1]["ref"] or items[p - 1]["id"]) for p in positions]
        except (ValueError, IndexError):
            _fail(brain_mod.BrainError("that isn't a valid permutation of the numbers shown"))
        try:
            brain_mod.reorder_collection(conn, ref, ordered_refs)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print("[green]reordered[/green]")


# ═══════════════════════════════ links ═══════════════════════════════


@add_app.command("rel")
def add_rel_cmd(
    name: str = typer.Argument(...),
    description: str = typer.Option(..., "--description"),
    symmetric: bool = typer.Option(False, "--symmetric"),
    inverse: str = typer.Option(None, "--inverse"),
) -> None:
    """Adds a new relationship type for `link entry --as NAME` — the
    vocabulary (extends/contradicts/supersedes/...) is a table you extend,
    same principle as kinds. Rarely needed: the 8 builtin rel types cover
    most cases. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.add_rel(conn, name, description=description, symmetric=symmetric, inverse=inverse)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]added rel[/green] {name!r}")


@link_app.command("entry")
def link_entry_cmd(a: str = typer.Argument(...), to: str = typer.Option(..., "--to"), as_: str = typer.Option(..., "--as"), note: str = typer.Option(None, "--note")) -> None:
    """Connects two entries with a named, directional relationship — the
    fourth organising axis alongside parent_id/tags/collections, for
    'what does this relate to, and how' (`extends`, `contradicts`,
    `supersedes`, ... — `list links` to browse existing ones, `add rel`
    to define a new relationship name). Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.link_entry(conn, a, to, as_, note=note)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]linked[/green] {a} --{as_}--> {to}")


@unlink_app.command("entry")
def unlink_entry_cmd(a: str = typer.Argument(...), to: str = typer.Option(..., "--to"), as_: str = typer.Option(..., "--as")) -> None:
    """Removes one specific link (same --to/--as it was created with). Both
    entries are untouched. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.unlink_entry(conn, a, to, as_)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]unlinked[/green] {a} --{as_}--> {to}")


@list_app.command("links")
def list_links_cmd(as_: str = typer.Option(None, "--as")) -> None:
    """Every link, optionally filtered to one relationship type.
    Exploratory, no required arguments. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.list_links(conn, as_rel=as_)
    table = Table(title="brain links")
    table.add_column("from")
    table.add_column("rel")
    table.add_column("to")
    for r in rows:
        table.add_row(r["from_title"], r["rel"], r["to_title"])
    console.print(table)


# ═══════════════════════════════ review / maintenance ═══════════════════════════════


@review_app.command("entry")
def review_entry_cmd(ref: str = typer.Argument(...), in_: str = typer.Option(..., "--in", help="e.g. 3months, 2weeks, 10days."), reason: str = typer.Option(None, "--reason")) -> None:
    """Schedules a future check-in on one entry — 'remind me to verify this
    is still true in 3 months.' Shows up in `due` once the date arrives;
    close it out with `done`. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.review_entry(conn, ref, in_, reason=reason)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]scheduled[/green] review of {ref} at {result['review_at'][:10]}")


@brain_app.command("due", rich_help_panel="Review & Maintenance")
def due_cmd() -> None:
    """Reviews scheduled with `review entry --in ...` whose date has arrived.
    Exploratory, no arguments — check this like a to-do list. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.due(conn)
    if not rows:
        console.print("[dim]nothing due[/dim]")
        return
    table = Table(title="brain due")
    table.add_column("title")
    table.add_column("reason")
    table.add_column("due")
    for r in rows:
        table.add_row(r["title"], r["reason"] or "-", r["review_at"][:10])
    console.print(table)


@brain_app.command("done", rich_help_panel="Review & Maintenance")
def done_cmd(ref: str = typer.Argument(...), outcome: str = typer.Option(..., "--outcome", help="kept | edited | archived | superseded")) -> None:
    """Closes out the oldest pending review on one entry — pairs with `due`,
    clearing one item off that list with an outcome recorded. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.done(conn, ref, outcome)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]marked done[/green] {ref}: {outcome}")


@brain_app.command("stale", rich_help_panel="Review & Maintenance")
def stale_cmd() -> None:
    """Entries saved but never `show`n/`pick`ed (use_count = 0), older than
    90 days. Exploratory, no arguments — either archive these or you forgot
    you had them; both are worth knowing. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.stale(conn)
    if not rows:
        console.print("[dim]nothing stale[/dim]")
        return
    table = Table(title="brain stale — saved, never used")
    table.add_column("title")
    table.add_column("saved")
    for r in rows:
        table.add_row(r["title"], r["created_at"][:10])
    console.print(table)


@brain_app.command("orphans", rich_help_panel="Review & Maintenance")
def orphans_cmd() -> None:
    """Entries with no parent, no tag, and no collection — the ones that
    will get lost, since nothing links back to them. Exploratory, no
    arguments; a periodic sweep, not a one-time fix. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.orphans(conn)
    if not rows:
        console.print("[dim]no orphans[/dim]")
        return
    table = Table(title="brain orphans — no parent, no tag, no collection")
    table.add_column("title")
    for r in rows:
        table.add_row(r["title"])
    console.print(table)


@brain_app.command("jot", rich_help_panel="Quick Capture (Inbox)")
def jot_cmd(
    text: str = typer.Argument(None, help="Quick, unstructured text — no --why needed. Omit and pass --file instead to jot a file's contents."),
    file: Path = typer.Option(None, "--file", help="Read the jot's text from this file instead of the command line (the 'drop a file' capture path)."),
) -> None:
    """Near-zero-friction capture (docs/46_AUDIT_second_brain_landscape.md
    §5): lands in the inbox, not the Brain proper — review with `brain
    inbox`, then `accept`/`reject`. Prints one status line."""
    if text is None and file is None:
        console.print("[red]pass some text, or --file, with a path[/red]")
        raise typer.Exit(code=1)
    with _conn() as (conn, _config):
        try:
            if file is not None:
                result = brain_mod.jot_file(conn, file)
            else:
                result = brain_mod.jot(conn, text)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]jotted[/green] — {result['title']}")


@brain_app.command("inbox", rich_help_panel="Quick Capture (Inbox)")
def inbox_cmd() -> None:
    """Everything captured with `jot`, awaiting `accept`/`reject`.
    Exploratory, no arguments — invisible to `list`/`find`/`export` until
    you act on it. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.list_inbox(conn)
    if not rows:
        console.print("[dim]inbox is empty[/dim]")
        return
    table = Table(title="brain inbox")
    table.add_column("#", justify="right")
    table.add_column("title")
    table.add_column("jotted")
    for i, r in enumerate(rows, start=1):
        table.add_row(str(i), r["title"], r["created_at"][:10])
    console.print(table)


@brain_app.command("accept", rich_help_panel="Quick Capture (Inbox)")
def accept_cmd(
    n: str = typer.Argument(..., help="Row number from `brain inbox`."),
    why: str = typer.Option(..., "--why"),
    kind: str = typer.Option(None, "--kind"),
    ref: str = typer.Option(None, "--ref"),
    tag: list[str] = typer.Option([], "--tag"),
) -> None:
    """Promotes an inbox item into a real, curated Brain entry. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.accept(conn, n, why, kind=kind, ref=ref, tags=tag)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]accepted[/green] entry {result['item_id']} — {result['title']}")


@brain_app.command("reject", rich_help_panel="Quick Capture (Inbox)")
def reject_cmd(n: str = typer.Argument(..., help="Row number from `brain inbox`.")) -> None:
    """Discards an inbox item outright — nothing was curated yet, so there's
    nothing to archive. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.reject(conn, n)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]rejected[/green] — {result['title']}")


_STATS_BY_AXES = ("kind", "tag", "collection", "status", "confidence", "sensitivity")


@stats_app.callback()
def stats_default(
    ctx: typer.Context,
    by: str = typer.Option(None, "--by", help=f"One of {', '.join(_STATS_BY_AXES)}. Omit for the full default report."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if by is not None:
        if by not in _STATS_BY_AXES:
            _fail(brain_mod.BrainError(f"--by must be one of {', '.join(_STATS_BY_AXES)}, got {by!r}"))
        with _conn() as (conn, _config):
            counts = brain_mod.stats_by(conn, by)
        table = Table(title=f"by {by}")
        table.add_column("name")
        table.add_column("count", justify="right")
        for k, v in counts.items():
            table.add_row(str(k), str(v))
        console.print(table)
        return
    with _conn() as (conn, _config):
        s = brain_mod.stats(conn)
    console.print(f"active: {s['total_active']}   archived: {s['total_archived']}")
    for label, counts in [("by kind", s["by_kind"]), ("by confidence", s["by_confidence"]), ("by tag", s["by_tag"]), ("by collection", s["by_collection"])]:
        table = Table(title=label)
        table.add_column("name")
        table.add_column("count", justify="right")
        for k, v in counts.items():
            table.add_row(str(k), str(v))
        console.print(table)


@stats_app.command("edits")
def stats_edits_cmd() -> None:
    """Edit counts grouped by origin (cli/editor/import/detected) — how much
    of the Brain's edit history came from you typing vs. `verify` catching
    an outside file change. Exploratory, no arguments. Prints a table."""
    with _conn() as (conn, _config):
        s = brain_mod.stats_edits(conn)
    table = Table(title="edits by origin")
    table.add_column("origin")
    table.add_column("count", justify="right")
    for k, v in s["by_origin"].items():
        table.add_row(k, str(v))
    console.print(table)


@stats_app.command("usage")
def stats_usage_cmd() -> None:
    """Most- and least-used entries by `show`/`pick` count. Exploratory, no
    arguments — the least-used list is a good starting point for `stale`. Prints two tables."""
    with _conn() as (conn, _config):
        s = brain_mod.stats_usage(conn)
    for label, rows in [("most used", s["most_used"]), ("least used", s["least_used"])]:
        table = Table(title=label)
        table.add_column("title")
        table.add_column("uses", justify="right")
        for r in rows:
            table.add_row(r["title"], str(r["use_count"]))
        console.print(table)


# docs/51_SPEC_brain_parity_and_naming.md §2.1 `doctor` row.
@brain_app.command("doctor", rich_help_panel="Review & Maintenance")
def doctor_cmd() -> None:
    """Environment preflight for the Brain: brain.sqlite integrity, pending
    migrations, orphaned FTS rows, the current `item_links` edge count
    against the ~200-edge Neo4j graph threshold (`brain graph` itself is a
    later batch — this just reports the number), and Ollama reachability
    (the same endpoint `summarise conversation`/`summarise entry` use, but
    a lightweight /api/tags ping — not a full generation). Scripted: no AI
    model is used for the check itself. Prints a status table."""
    from chatvault.brain import doctor as brain_doctor_mod

    with _conn() as (conn, config):
        results = brain_doctor_mod.run_all(conn)
    table = Table(title="brain doctor")
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
    console.print(f"brain: {config.brain_path}")
    if fatal_failed:
        console.print("[red]One or more fatal checks failed.[/red]")
        raise typer.Exit(code=1)


# docs/51_SPEC_brain_parity_and_naming.md §2.1 `ledger` row.
@brain_app.command("ledger", rich_help_panel="Review & Maintenance")
def ledger_cmd() -> None:
    """Provenance report: how many items came from where, read from
    `items.origin_detail` — stamped by `copy conversation`/`copy stashed`
    (`copy:chatvault`) and `summarise conversation`/`summarise entry`
    (`summarise:<model>`). Items from a code path that doesn't stamp
    origin_detail yet (`add entry`, `jot`, `import`) report as `(unknown)`
    rather than being silently omitted. Scripted: no AI model is used.
    Exploratory, no arguments. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.ledger_mod.report(conn)
    table = Table(title="brain ledger")
    table.add_column("origin")
    table.add_column("count", justify="right")
    for r in rows:
        table.add_row(r["origin"], str(r["count"]))
    console.print(table)


@brain_app.command("verify", rich_help_panel="Review & Maintenance")
def verify_cmd() -> None:
    """Dangling refs (aliases/tags/links/collections/parents pointing at a
    gone item), plus drift detection: re-hashes any file-backed body and
    resyncs it if the file changed outside the DB. Self-healing — running it
    fixes what it finds, not just reports it. Exploratory, no arguments. Prints status lines."""
    with _conn() as (conn, _config):
        report = brain_mod.verify(conn)
    total_dangling = sum(len(v) for k, v in report.items() if k.startswith("dangling"))
    console.print(f"dangling references: {total_dangling}")
    for k, v in report.items():
        if k.startswith("dangling") and v:
            console.print(f"  [yellow]{k}[/yellow]: {len(v)}")
    if report["drifted"]:
        console.print(f"[yellow]drifted (resynced from disk):[/yellow] {len(report['drifted'])}")
        for d in report["drifted"]:
            console.print(f"  - {d['title']}: {d['issue']}")
    if not total_dangling and not report["drifted"]:
        console.print("[green]clean[/green]")


@brain_app.command("backup", rich_help_panel="Backup & Export")
def backup_cmd(label: str = typer.Option(None, "--label")) -> None:
    """Compressed snapshot of the whole brain.sqlite via SQLite's Online
    Backup API (safe against a concurrent writer). The Brain has no raw/ to
    rebuild from — do this often, and always before an import or a
    migration. Prints one status line."""
    from chatvault.brain.backup import create_brain_backup

    config = load_config()
    info = create_brain_backup(config, label=label)
    console.print(f"[green]backed up[/green] {info.item_count} item(s) -> {info.path}")


@brain_app.command("export", rich_help_panel="Backup & Export")
def export_cmd(
    format: str = typer.Option("json", "--format", help="json | markdown | yaml"),
    collection: str = typer.Option(None, "--collection"),
    include_sensitive: bool = typer.Option(False, "--include-sensitive"),
    tree: str = typer.Option(None, "--tree", help="A tag or prefix (e.g. 'tech/**') — writes every matching item into a nested folder structure mirroring the tag tree, under --out/--dest. An item matching more than one tag in the tree appears under each."),
    out: Path = typer.Option(None, "--out", help="Exact output path. Without --tree: the export file itself. With --tree: the output directory files are nested under. Overrides --dest."),
    dest: str = typer.Option(None, "--dest", help="Where to create the dated export folder when --out isn't given: 'desktop' or 'here' (current directory). Only takes effect when given explicitly — omitting both --out and --dest keeps printing to stdout, unchanged from before this option existed."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the planned file path(s) without writing anything or printing the export body."),
) -> None:
    """Dumps every active entry to stdout (json/markdown/yaml), or to disk
    with --out/--dest/--tree. Excludes sensitivity != 'normal' unless
    --include-sensitive. Exploratory, no required arguments — pipe to a file
    or `pbcopy`; this is the read half of the Brain's own backup discipline
    (pair with `backup` for the DB itself).

    With none of --out/--dest/--dry-run/--tree, behaves exactly as always:
    prints the whole result set to stdout, nothing written to disk. Passing
    --out or --dest writes a single combined file instead (there is no
    natural per-row split the way `chatvault export` has one file per
    conversation — Brain items aren't modeled that way). --tree writes one
    file per matched item into nested folders mirroring the tag tree,
    mirroring `chatvault export --tree`."""
    if dest is not None and dest not in ("desktop", "here"):
        console.print(f"[red]--dest must be 'desktop' or 'here', got {dest!r}[/red]")
        raise typer.Exit(code=1)

    ext = brain_mod.EXPORT_EXTENSIONS.get(format, format)

    def _dest_root() -> Path:
        return Path.cwd() if dest == "here" else Path.home() / "Desktop"

    with _conn() as (conn, _config):
        if tree is not None:
            try:
                rows = brain_mod.get_items_for_tree(conn, tree, include_sensitive=include_sensitive)
            except ValueError as exc:
                _fail(exc)
            if not rows:
                console.print(f"[yellow]no entries tagged under '{tree}'[/yellow]")
                raise typer.Exit(code=0)

            if out is not None:
                out_dir = out
            else:
                out_dir = _dest_root() / f"Chatvault_export_{date.today().isoformat()}"

            planned = []
            for row in rows:
                item = dict(row)
                fname = f"{export_mod.slugify(item['title'])}-{item['id']}.{ext}"
                target_dir = out_dir / export_mod.tree_relative_dir(tree, row["matched_tag"])
                planned.append((target_dir / fname, item))

            if dry_run:
                for path, _item in planned:
                    console.print(f"[dim]would write[/dim] {path}")
                console.print(f"[dim]{len(planned)} file(s) planned under {out_dir}[/dim]")
                return

            for path, item in planned:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(brain_mod.render_rows([item], format))
            console.print(f"wrote {len(planned)} file(s) under {out_dir} (tag tree '{tree}')")
            return

        try:
            text = brain_mod.export_items(conn, fmt=format, collection=collection, include_sensitive=include_sensitive)
        except (brain_mod.BrainError, ValueError) as exc:
            _fail(exc)

    if out is None and dest is None and not dry_run:
        # Default, unchanged: pipe-friendly stdout, nothing written to disk.
        # Plain print, deliberately not console.print: rich word-wraps to
        # terminal width by default, which inserts line breaks inside
        # JSON/YAML string values and corrupts machine-readable output.
        print(text)
        return

    if out is not None:
        # Literal target file, matching --out's docstring ("write ... to a
        # file at that path") — unlike chatvault export's --out, which is a
        # directory. Brain's export is always a single combined file, so
        # there's no directory structure to put underneath one.
        path = out
    else:
        path = _dest_root() / f"Chatvault_export_{date.today().isoformat()}" / f"brain-export.{ext}"

    if dry_run:
        console.print(f"[dim]would write[/dim] {path}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    console.print(f"wrote {path}")


# ═══════════════════════════════ import ═══════════════════════════════


@import_app.command("markdown")
def import_markdown_cmd(directory: Path = typer.Argument(...), kind: str = typer.Option("note", "--kind"), tag: str = typer.Option(None, "--tag")) -> None:
    """Bulk-imports every .md file under `directory` as one entry each —
    front-matter becomes `meta`, the H1 becomes the title, the folder path
    becomes hierarchical tags. This is how an existing pile of notes gets
    in without retyping any of it. Each file's `--why` is auto-generated
    ('imported from' plus the file's relative path); edit afterward if you want something better. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            results = brain_mod.import_markdown(conn, directory, kind=kind, extra_tag=tag)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]imported[/green] {len(results)} file(s) from {directory}")


@import_app.command("yaml")
def import_yaml_cmd(file: Path = typer.Argument(...), map_: Path = typer.Option(..., "--map")) -> None:
    """Bulk-imports a YAML list/mapping of records, one entry per record.
    --map is a small YAML file describing which source keys become which
    entry fields (`fields`/`defaults`/`tag_field`/`tag_delimiter`) — write
    that once per source shape, reuse it every time. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            results = brain_mod.import_yaml(conn, file, map_)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]imported[/green] {len(results)} record(s) from {file}")


@import_app.command("csv")
def import_csv_cmd(file: Path = typer.Argument(...), map_: Path = typer.Option(..., "--map")) -> None:
    """Bulk-imports a CSV, one entry per row — same `--map` mapping format
    as `import yaml`. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            results = brain_mod.import_csv(conn, file, map_)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]imported[/green] {len(results)} record(s) from {file}")


# ═══════════════════════════════ dedupe ═══════════════════════════════
# docs/51_SPEC_brain_parity_and_naming.md §2.1 `dedupe` row, §8 Q "auto-
# merge?" -> never (D7). `brain dedupe` alone reports candidates and
# changes nothing; only `--confirm <a> <b>` acts, on that one explicit pair.


@brain_app.command("dedupe", rich_help_panel="Review & Maintenance")
def dedupe_cmd(
    confirm: bool = typer.Option(False, "--confirm", help="Actually merge --a/--b. Without this, dedupe only reports candidates."),
    a: str = typer.Option(None, "--a", help="First entry ref/alias/row-number of the pair to merge (with --confirm)."),
    b: str = typer.Option(None, "--b", help="Second entry ref/alias/row-number of the pair to merge (with --confirm)."),
) -> None:
    """Reports candidate duplicate entries — exact body matches, same
    title+kind, or same source vault conversation. Scripted: no AI model is
    used. **Never merges anything on its own** (D7): running `brain dedupe`
    with no flags modifies nothing. To act on one specific pair you've
    reviewed, pass `--confirm --a <ref> --b <ref>` — the lower-id entry is
    kept, the other's tags/links/collection memberships are copied onto it,
    then it's archived (not hard-deleted; `restore entry` undoes it).
    Prints a table of candidates, or one status line when merging."""
    with _conn() as (conn, _config):
        if confirm:
            if a is None or b is None:
                console.print("[red]--confirm requires both --a and --b[/red]")
                raise typer.Exit(code=1)
            try:
                result = brain_mod.dedupe_mod.merge_pair(conn, a, b, confirm=True)
            except brain_mod.BrainError as exc:
                _fail(exc)
            console.print(
                f"[green]merged[/green] {result['archived_title']!r} (id {result['archived']}) "
                f"into {result['kept_title']!r} (id {result['kept']})"
            )
            return

        candidates = brain_mod.dedupe_mod.scan_candidates(conn)
    if not candidates:
        console.print("[dim]no duplicate candidates found[/dim]")
        return
    table = Table(title="brain dedupe candidates (reported only — never auto-merged)")
    table.add_column("a", justify="right")
    table.add_column("title a")
    table.add_column("b", justify="right")
    table.add_column("title b")
    table.add_column("reason")
    table.add_column("confidence", justify="right")
    for c in candidates:
        table.add_row(
            str(c["item_id_a"]), c["title_a"], str(c["item_id_b"]), c["title_b"], c["reason"], f"{c['confidence']:.1f}",
        )
    console.print(table)
    console.print("[dim]nothing changed — pass `--confirm --a <id> --b <id>` to merge one specific pair[/dim]")


# ═══════════════════════════════ redact / purge ═══════════════════════════════
# docs/51_SPEC_brain_parity_and_naming.md §2.1 `redact`/`purge` row — same
# --confirm gate as `remove entry`.


@brain_app.command("redact", rich_help_panel="Review & Maintenance")
def redact_cmd(ref: str = typer.Argument(...), confirm: bool = typer.Option(False, "--confirm")) -> None:
    """Wipes an entry's body, keeps the row (title/why/tags/links/history)
    — for a personal store, sometimes you want to forget only what was
    written, not that the entry existed. Irreversible; requires --confirm,
    same bar as `remove entry`. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.redact_entry(conn, ref, confirm=confirm)
        except brain_mod.BrainError as exc:
            _fail(exc)
    if result["action"] == "already_empty":
        console.print(f"[dim]entry {result['item_id']} already has no body[/dim]")
    else:
        console.print(f"[green]redacted[/green] entry {result['item_id']} — {result['title']}")


@brain_app.command("purge", rich_help_panel="Review & Maintenance")
def purge_cmd(ref: str = typer.Argument(...), confirm: bool = typer.Option(False, "--confirm")) -> None:
    """Hard-deletes an entry — the same operation as `remove entry`, under
    the name this spec's parity table asks for. Backs up brain.sqlite
    first, refuses on an item with children, requires --confirm. Prints one status line."""
    with _conn() as (conn, config):
        try:
            result = brain_mod.purge_entry(conn, ref, confirm=confirm, config=config)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]purged[/green] entry {result['item_id']} — {result['title']}")


# ═══════════════════════════════ view (saved queries) ═══════════════════════════════


@view_app.command("save")
def view_save_cmd(
    name: str = typer.Argument(...),
    query: str = typer.Option(..., "--query", help="SQL to run later against brain.sqlite."),
    description: str = typer.Option(None, "--description"),
) -> None:
    """Names a SQL query so it can be re-run by name later (`view run`) —
    same shape as ChatVault's own `view save`. Stores the query text only;
    nothing is executed at save time. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.views_mod.save_view(conn, name, query, description=description)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]saved[/green] view {name!r}")


@view_app.command("run")
def view_run_cmd(name: str = typer.Argument(...)) -> None:
    """Executes a saved view's SQL and prints the raw result rows. Prints a table."""
    with _conn() as (conn, _config):
        try:
            rows = brain_mod.views_mod.run_view(conn, name)
        except brain_mod.BrainError as exc:
            _fail(exc)
    if not rows:
        console.print("[dim]no rows[/dim]")
        return
    table = Table(title=f"view: {name}")
    for col in rows[0].keys():
        table.add_column(col)
    for r in rows:
        table.add_row(*[str(v) for v in tuple(r)])
    console.print(table)


@view_app.command("list")
def view_list_cmd() -> None:
    """Every saved view and its description. Exploratory, no arguments. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.views_mod.list_views(conn)
    table = Table(title="brain views")
    table.add_column("name")
    table.add_column("description")
    for r in rows:
        table.add_row(r["name"], r["description"] or "")
    console.print(table)


@view_app.command("remove")
def view_remove_cmd(name: str = typer.Argument(...)) -> None:
    """Deletes a saved view definition. Doesn't touch anything it ever
    returned. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            brain_mod.views_mod.remove_view(conn, name)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]removed[/green] view {name!r}")


# ═══════════════════════════════ tag rule ═══════════════════════════════


@tag_rule_app.command("add")
def tag_rule_add_cmd(
    tag: str = typer.Argument(...),
    pattern: str = typer.Option(..., "--pattern", help="Plain, case-insensitive substring to match."),
    field: str = typer.Option("title", "--field", help="title | body | why"),
) -> None:
    """Stores a rule: any entry whose --field contains --pattern gets
    tagged automatically. Applied on every future `add entry`/`accept`
    (source='rule'); run `tag rule apply` to backfill existing entries too.
    Always reversible: `untag entry <ref> --source rule`. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.tag_rules_mod.add_rule(conn, pattern, tag, field=field)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]{result['action']}[/green] rule #{result['rule_id']}: tag {tag!r} where {field} contains {pattern!r}")


@tag_rule_app.command("list")
def tag_rule_list_cmd() -> None:
    """Every stored auto-tag rule. Exploratory, no arguments. Prints a table."""
    with _conn() as (conn, _config):
        rows = brain_mod.tag_rules_mod.list_rules(conn)
    table = Table(title="brain tag rules")
    table.add_column("id")
    table.add_column("tag")
    table.add_column("field")
    table.add_column("pattern")
    for r in rows:
        table.add_row(str(r["id"]), r["tag"], r["field"], r["pattern"])
    console.print(table)


@tag_rule_app.command("remove")
def tag_rule_remove_cmd(rule_id: int = typer.Argument(...)) -> None:
    """Deletes the rule definition. Does not retroactively untag anything —
    use `untag entry <ref> --source rule` for that. Prints one status line."""
    with _conn() as (conn, _config):
        ok = brain_mod.tag_rules_mod.remove_rule(conn, rule_id)
    if not ok:
        console.print(f"[red]no rule #{rule_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]removed[/green] rule #{rule_id}")


@tag_rule_app.command("apply")
def tag_rule_apply_cmd() -> None:
    """Backfill: (re-)evaluate every rule against every active entry, not
    just new ones. Idempotent. Prints one status line."""
    with _conn() as (conn, _config):
        n = brain_mod.tag_rules_mod.apply_rules_all(conn)
    console.print(f"[green]applied[/green] {n} new tag(s)")


# ═══════════════════════════════ curation export/import/diff ═══════════════════════════════


def _print_curation_summary(summary, title: str) -> None:
    table = Table(title=title)
    table.add_column("entity")
    table.add_column("added", justify="right")
    table.add_column("updated", justify="right")
    table.add_column("unchanged", justify="right")
    for entity, c in summary.counts.items():
        table.add_row(entity, str(c.get("added", 0)), str(c.get("updated", 0)), str(c.get("unchanged", 0)))
    console.print(table)
    if summary.unresolved:
        table = Table(title="unresolved")
        table.add_column("entity")
        table.add_column("reason")
        table.add_column("target")
        for u in summary.unresolved:
            table.add_row(u["entity"], u["reason"], str(u["target"]))
        console.print(table)


@curation_app.command("export")
def curation_export_cmd(out: Path = typer.Option(None, "--out")) -> None:
    """Snapshots every tag/item-link/collection/note/exclusion/view to one
    portable JSON file — the curation-layer backup format specifically
    (pair with `backup` for the full DB). Mirrors ChatVault's own `curation
    export`. Writes a file."""
    with _conn() as (conn, config):
        data = brain_mod.curation_io_mod.export_curation(conn)
    out = out or (config.brain_path.parent / f"brain-curation-{data['exported_at'][:10]}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2))
    console.print(f"wrote {out} ({', '.join(f'{k}={v}' for k, v in data['counts'].items())})")


@curation_app.command("import")
def curation_import_cmd(
    file: Path = typer.Argument(...),
    on_conflict: str = typer.Option("skip", "--on-conflict", help="skip | overwrite"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Re-applies a curation snapshot (from `curation export`) — additive
    by default (--on-conflict skip), or --on-conflict overwrite to let the
    file's values win. --dry-run reports what would change without writing
    anything (identical to `curation diff`). Prints a summary table."""
    data = json.loads(file.read_text())
    with _conn() as (conn, _config):
        try:
            summary = brain_mod.curation_io_mod.import_curation(conn, data, on_conflict=on_conflict, dry_run=dry_run)
        except ValueError as exc:
            _fail(exc)
    _print_curation_summary(summary, title="curation import" + (" (dry run)" if dry_run else ""))


@curation_app.command("diff")
def curation_diff_cmd(file: Path = typer.Argument(...)) -> None:
    """Compares a curation snapshot against the Brain's current state and
    reports what would change — a dry-run import, changing nothing. Prints a summary table."""
    data = json.loads(file.read_text())
    with _conn() as (conn, _config):
        summary = brain_mod.curation_io_mod.diff_curation(conn, data)
    _print_curation_summary(summary, title="curation diff")


# ═══════════════════════════════ attachments ═══════════════════════════════


@attachments_app.command("add")
def attachments_add_cmd(
    ref: str = typer.Argument(..., help="Entry ref, alias, or row number."),
    path: Path = typer.Argument(...),
    no_store_content: bool = typer.Option(False, "--no-store-content", help="Record metadata/hash only, don't embed the file's text in the DB."),
) -> None:
    """Attaches a local file to an entry — records its hash/size/mime, and
    (for text files, unless --no-store-content) the file's own content, so
    the attachment survives the original file moving or disappearing.
    Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.attachments_mod.add_attachment(conn, ref, path, store_content=not no_store_content)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]attached[/green] {result['filename']} ({result['bytes']} bytes) to entry {result['item_id']} as attachment {result['attachment_id']}")


@attachments_app.command("list")
def attachments_list_cmd(ref: str = typer.Argument(..., help="Entry ref, alias, or row number.")) -> None:
    """Every attachment on one entry. Prints a table."""
    with _conn() as (conn, _config):
        try:
            rows = brain_mod.attachments_mod.list_attachments(conn, ref)
        except brain_mod.BrainError as exc:
            _fail(exc)
    if not rows:
        console.print("[dim]no attachments[/dim]")
        return
    table = Table(title=f"attachments on {ref}")
    table.add_column("id", justify="right")
    table.add_column("filename")
    table.add_column("mime")
    table.add_column("bytes", justify="right")
    table.add_column("added")
    for r in rows:
        table.add_row(str(r["id"]), r["filename"] or "-", r["mime"] or "-", str(r["bytes"] or 0), r["added_at"][:10])
    console.print(table)


@attachments_app.command("show")
def attachments_show_cmd(attachment_id: int = typer.Argument(...)) -> None:
    """Full metadata (and content, if stored) for one attachment. Pages the result."""
    with _conn() as (conn, _config):
        try:
            row = brain_mod.attachments_mod.show_attachment(conn, attachment_id)
        except brain_mod.BrainError as exc:
            _fail(exc)
    parts = [
        f"filename: {row['filename']}   mime: {row['mime'] or '-'}   bytes: {row['bytes']}",
        f"path: {row['path'] or '-'}   sha256: {row['sha256']}",
        f"added: {row['added_at']}",
        "-" * 40,
        row["content"] if row["content"] is not None else "(binary or not stored — see path)",
    ]
    _page("\n".join(parts))


@attachments_app.command("remove")
def attachments_remove_cmd(attachment_id: int = typer.Argument(...)) -> None:
    """Removes one attachment record. The entry itself is untouched. Prints one status line."""
    with _conn() as (conn, _config):
        try:
            result = brain_mod.attachments_mod.remove_attachment(conn, attachment_id)
        except brain_mod.BrainError as exc:
            _fail(exc)
    console.print(f"[green]removed[/green] attachment {result['attachment_id']} ({result['filename']})")


# ═══════════════════════════════ graph (built, not run — §7 non-goal) ═══════════════════════════════


@graph_app.command("rebuild")
def graph_rebuild_cmd(
    driver: bool = typer.Option(False, "--i-know-this-connects-to-neo4j", help="Required acknowledgement — this command opens a real Neo4j connection."),
) -> None:
    """Projects the Brain (items as nodes, item_links as edges, tags as
    TAGGED edges) into Neo4j. Built per docs/51_SPEC_brain_parity_and_naming.md
    §2.1/§7: exists so it's ready once `item_links` crosses the ~200-edge
    threshold `brain doctor` reports, but this spec explicitly does not run
    it for real — pass the acknowledgement flag if you mean to. Prints status lines with counts."""
    if not driver:
        console.print(
            "[yellow]scaffolding only — this would open a real Neo4j connection. "
            "Pass --i-know-this-connects-to-neo4j to proceed.[/yellow]"
        )
        raise typer.Exit(code=1)
    config = load_config()
    with _conn() as (conn, _config2):
        neo4j_driver = brain_mod.graph_mod.get_driver(config)
        try:
            counts = brain_mod.graph_mod.rebuild(conn, neo4j_driver)
        finally:
            neo4j_driver.close()
    for k, v in counts.items():
        console.print(f"{k}: {v}")


@graph_app.command("query")
def graph_query_cmd(
    cypher: str = typer.Argument(...),
    driver: bool = typer.Option(False, "--i-know-this-connects-to-neo4j"),
) -> None:
    """Runs an arbitrary Cypher query against Neo4j and prints the rows —
    same scaffolding-only status as `graph rebuild`. Prints a table."""
    if not driver:
        console.print(
            "[yellow]scaffolding only — this would open a real Neo4j connection. "
            "Pass --i-know-this-connects-to-neo4j to proceed.[/yellow]"
        )
        raise typer.Exit(code=1)
    config = load_config()
    neo4j_driver = brain_mod.graph_mod.get_driver(config)
    try:
        rows = brain_mod.graph_mod.run_query(neo4j_driver, cypher)
    finally:
        neo4j_driver.close()
    if not rows:
        console.print("[dim]no rows[/dim]")
        return
    table = Table()
    for col in rows[0].keys():
        table.add_column(col)
    for r in rows:
        table.add_row(*[str(v) for v in r.values()])
    console.print(table)

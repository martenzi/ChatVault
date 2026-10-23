# Decisions — why ChatVault is built this way

**Written:** 2026-08-12
**Purpose:** stop settled questions being relitigated. One entry per contested decision: what was chosen, what was rejected, and the reason.

**If you're starting a new AI thread about ChatVault, this is the file to read first.**

---

## D1 — SQLite is the system of record, not Neo4j

**Chosen:** SQLite holds the truth. Neo4j is a one-way, disposable projection.

**Rejected:** Neo4j as primary; a dual-write setup.

**Why:** the queries that matter are 0–1 hop — filter, count, aggregate, full-text. Graph databases earn their cost through *index-free adjacency*, which only pays at 2+ hops. Neo4j offered no speed advantage and added an operations burden.

**Consequence:** `graph rebuild` can be run, wiped, and re-run freely. Nothing in Neo4j is ever lost, because nothing originates there.

---

## D2 — Blocks are the atomic unit

**Chosen:** conversations → messages → **blocks**, where a block is a single text/tool_use/tool_result/thinking element.

**Rejected:** storing messages as single text fields.

**Why:** it makes individual tool calls addressable — countable in stats, strippable from exports, taggable on their own. Nearly every capability that turned out to matter (`lean` export profile, block-level dedupe, tool statistics) depends on this.

**Cost:** ~511k rows. Irrelevant to SQLite.

---

## D3 — Never delete; filter at the view layer

**Chosen:** `excluded`, `superseded_by`, `orphaned` flags, hidden by default through `v_*` views.

**Rejected:** actual `DELETE`.

**Why:** cleanup is an opinion, and opinions should be reversible.

**The one violation proved the rule:** `embedding_meta` was the only table without a `v_*` view, so a count forgot to filter and misreported. **A convention without a mechanism decays.** Every table gets its view.

---

## D4 — RAW exports are immutable; everything else is derived

**Chosen:** the Lightroom catalog model. `raw/` is write-once. The database is a rebuildable catalog. Markdown, Neo4j, embeddings and the attachment store are all disposable renders.

**Rejected:** editing JSON in place; treating markdown as the archive.

**Why:** you can be reckless downstream because upstream is safe.

**Exception worth knowing:** your **tags, annotations and exclusions are *not* rebuildable**. That's why `curation export` exists and why backups matter once you start tagging.

---

## D5 — Tags, not folders

**Chosen:** hierarchical tags, unlimited axes, many per conversation.

**Rejected:** a folder tree; one canonical taxonomy.

**Why:** a folder is a tree — one item, one location, you must choose. A tag is a graph — one item, many locations, you never choose. An npm conversation is *simultaneously* about macOS package managers, Docker, troubleshooting, and an unresolved decision. Folders force a lie.

**Consequence:** folders come back as *output* (`export --tree`), generated in as many arrangements as you like, disposable.

---

## D6 — Project label ≠ tag

**Chosen:** `conversations.project` / `project_id` records **where a conversation came from** (fact, one value, set at ingest). Tags record **what you think about it** (judgement, many values, set whenever).

**Why:** conflating them means either losing provenance or polluting your taxonomy with facts you didn't choose.

See `10_CONCEPTS_label_tag_project.md`.

---

## D7 — Flag, don't guess

**Chosen:** where identity or quality is ambiguous, mark it with a `review/*` tag and let the human decide.

**Rejected:** heuristic auto-resolution; silently dropping odd records.

**Why:** stated user preference, and it turned out to be a good general rule — *"files that stand out but are marked as standing out are easily found again."*

**Applied to:** ambiguous project names, Cursor scratch sessions, Windsurf filename-projects, missing timezones, dedupe candidates.

---

## D8 — Keyword and semantic search both stay

**Chosen:** FTS5 for exact strings, `sqlite-vec` + local embeddings for meaning.

**Rejected:** replacing one with the other.

**Why:** they answer different questions. Keyword finds an error message you can quote. Semantic finds the conversation you can't name — the npm problem that never says "npm."

**Working rule:** **search is for discovery, tags are for memory.** When a search takes real effort, tag the result so it never takes effort again.

---

## D9 — Datasette for reading, Textual for writing

**Chosen:** Datasette as the browse/filter/stats surface. A custom Textual screen for tagging.

**Rejected:** building a Datasette clone; using Datasette for tagging.

**Why:** Datasette's faceting, URL-as-state and export are better than anything worth rebuilding — **but it is read-only by design.** No off-the-shelf tool does bulk tagging against your own schema.

**Corrected on 2026-08-09:** an earlier document claimed Datasette could serve the tagging need. It can't.

---

## D10 — Views for shape, filters for values

**Chosen:** one good SQL view (`v_browse`) with column order and readable dates. Then filter it interactively.

**Rejected:** a view per project; canned queries as the default browsing surface.

**Why:** thirty views is thirty things to maintain. And **Datasette canned queries lose faceting** — the feature that makes Datasette worth using. A bookmarked filtered URL *is* a saved view, costs nothing, deletes by closing a tab.

---

## D11 — Store dates as the source wrote them

**Chosen:** keep all six timestamp formats. Fix presentation in views.

**Rejected:** normalising stored values.

**Why:** the format is itself a data-quality signal about the source. ISO-8601 sorts correctly as text regardless. Presentation is free; rewriting history isn't.

**Open:** 67 conversations have no timezone marker — to be flagged, not fixed.

---

## D12 — Coverage is not the goal

**Chosen:** tag when retrieval hurt. Expect a few hundred conversations tagged, ever.

**Rejected:** systematically tagging the whole corpus; AI auto-tagging everything.

**Why:** a tag that comes from a real retrieval failure is guaranteed useful. A tag invented in advance is a guess. Chasing coverage turns the system into a chore, which is how it dies.

---

## D13 — Specs carry acceptance criteria

**Chosen:** every spec ends with testable conditions.

**Why:** three genuine design flaws surfaced *only* because someone tried to write the test — the impossible `lean`-vs-`read` size assertion, the undefined `dedupe --apply`, the unplaced `raw_exclusions/`. Vague specification hides in prose and dies in a test.

---

## D14 — Verify against data, never against a summary

**Chosen:** query the database before making a claim about the database.

**Why:** this failure recurred three times — Windsurf declared unrecoverable after testing the wrong corpus; project linkage declared missing after reading one adapter; `v1.x` names implying supersession that never existed. An earlier project log recorded the lesson in July and it still happened again in August.

**Every correct conclusion in the audit phase came from running a query. Every wrong one came from reading code and extrapolating.**

---

## D15 — Only import what's in `pyproject.toml`

**Chosen:** stdlib or declared dependencies for terminal plumbing (pager, editor) — `pydoc.pager`, `tempfile`+`subprocess` — never a transitive dep like `click`.

**Rejected:** `import click` for `echo_via_pager`/`edit`, on the assumption typer always drags it in.

**Why:** it did, until it didn't — typer 0.27 vendored Click internally, so `uv.lock` never contained `click` and both `show`/`stash show` pager branches crashed with `ModuleNotFoundError` the first time they ran on a real TTY (2026-10-09). `brain_cli` had already hit the same wall and documented it; the fix unified both files on the stdlib pattern.

**Rule of thumb:** if it's not listed in `pyproject.toml`'s `dependencies`, it doesn't exist — no matter what transitively happens to be installed today.

---

## D16 — Verify write paths against a scratch vault, never production

**Chosen:** any manual check of a command that writes (`view save`, `tag add`, `stash add`, `note`, `exclude`, `ingest`, …) runs against a scratch vault, selected with one environment variable: `CHATVAULT_ROOT`.

**Rejected:** testing against production and cleaning up afterwards.

**Why:** while testing `view run` (2026-10-19) two saved views were created in the production database. They were noticed, deleted and disclosed, and no harm followed — but nothing prevented it, and the next stray write might not be reversible. Ingest already had this habit (backup, scratch copy, then production); it was never stated for the small CLI write commands.

**How:** see "Scratch vault" in `INDEX.md`.

---

## D17 — `raw/` shows status by location, and "ingested" is decided by content, never by path

**Chosen:** `raw/` is organised as `00_inbox` → `10_ingested/<platform>/<date>` / `20_needs_adapter` / `30_excluded/<reason>` / `40_archive`, so the folder itself says what is ingested. `chatvault intake` moves a file only after its `source_files` row is `ok`, and updates the recorded path in the same step. Whether something is already ingested is decided by sha256, then by looking real text up in the full-text index — never by folder name, file name or a title match.

**Rejected:** trusting paths (the user reorganises `raw/` by hand and a path match is meaningless afterwards); a daemon watching the folder (no daemon infrastructure exists, and run-on-command is what was asked for).

**Why:** four sources (OpenWebUI, Lawline, Codex, two single-chat exports) went unnoticed for weeks because `audit` always failed and nothing recorded sources that were never ingested. A title match once produced a false "Lawline is done"; a text-mention search once mistook mentions for provenance. `reconcile` is move-aware (same bytes elsewhere = `moved`, only vanished bytes = `missing`) so a reorganisation can never read as data loss.

**How:** `src/chatvault/intake.py`, `source_registry.py`, `docs/source_registry.md`; `chatvault sources status` exits non-zero for an unexplained chat-shaped file.

---

## D18 — Derive facts, declare intentions

**Chosen:** a *fact* is computable from data (where a file was found, when it was ingested). An *intention* is not (which places to re-scan, what is out of scope, which duplicates are acceptable). Intentions are declared in an explicit registry; they are never inferred from facts. `ingest --live` reads the `live_roots` table, not `GROUP BY root_dir` over `source_files`, and `source_files.root_dir` is provenance only.

**Rejected:** keep computing live roots from `source_files` and filter out the bad ones — filtering treats symptoms, and the next accident in how a file was ingested creates a new phantom root.

**Why:** the derived list produced three defects at once. A staged snapshot marked `live` made `--live` re-read an extract that can never change, so Devin was never refreshed. A wrong-case path (`Chatvault` for `ChatVault`; APFS accepts both) became a second root, and a single `live` row under it made `--live` scan the whole `raw/` tree. And about 70 nested project folders each registered as their own root. The same pattern is why four sources sat un-ingested for months before `source_registry.md` declared them.

**How:** `src/chatvault/live_roots.py`, migration 016, `chatvault live-roots`. A source is *direct* (a directory that grows) or *extracted* (a SQLite store snapshotted by an `extract_cmd` into `raw/00_inbox/<source>/`, then ingested and filed by `intake`). A staged snapshot is `frozen` by nature; `live` belongs to the store it came from. Staleness is measured against the live store. Every stored path goes through `paths.canonical()`, and a trigger rejects a `root_dir` that differs from an existing one only by letter case.

---

## D19 — A spec's claim about existing code or schema is a hypothesis until verified

**Chosen:** a spec that says an existing field "already holds what we need" must state what that field was created to mean and confirm the overlap for every case. Claims about current behaviour are marked as hypotheses and checked before building on them — the same discipline D14 applies to claims about data.

**Rejected:** rely on review to catch it — the same mistake happened twice.

**Why:** `source_files.kind='live'` was built to answer "does this go stale?" and was reused as the input to "where do I read from?". The two coincide for direct sources and diverge for extracted ones. Separately, a spec once asserted that a bare `find` returned 50 rows cheaply when it loaded the whole table.

**How:** specs list "claims to verify" with the command that checks each; a claim that fails is reported, not worked around.

---

## Deliberately not decided

Open by choice, not oversight:

| Question | Status |
|---|---|
| Web interface | Deferred — try Textual + Datasette + Obsidian SQL plugin first |
| Memory files (v1.2 Part B) | Specced, unbuilt, waiting on you to want it |
| Concept extraction & the second brain | The thing that finally makes Neo4j earn its place. Not yet. |
| Adapters for DeepSeek / Grok / Gemini | Only if you actually have history worth recovering |
| Antigravity recovery | The last unrecovered editor. Unscoped. |

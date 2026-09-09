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

## Deliberately not decided

Open by choice, not oversight:

| Question | Status |
|---|---|
| Web interface | Deferred — try Textual + Datasette + Obsidian SQL plugin first |
| Memory files (v1.2 Part B) | Specced, unbuilt, waiting on you to want it |
| Concept extraction & the second brain | The thing that finally makes Neo4j earn its place. Not yet. |
| Adapters for DeepSeek / Grok / Gemini | Only if you actually have history worth recovering |
| Antigravity recovery | The last unrecovered editor. Unscoped. |

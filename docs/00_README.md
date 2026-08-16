# ChatVault Documentation

**Updated:** 2026-08-12

---

## Read in this order

| # | Document | What it answers | Read when |
|---|---|---|---|
| **01** | `01_STATE_OF_PLAY.md` | What exists, what's broken, what's next | **Always first** |
| **02** | `02_DECISIONS.md` | Why it's built this way | Before proposing changes |
| **03** | `03_INDEX_themes_and_domains.md` | Structural map — what lives where | When orienting or hunting |
| **04** | `04_HISTORY_how_we_got_here_2026-08-09.md` | The narrative, with timeline | For context, not for decisions |
| **05** | `05_INGESTION_LEDGER.md` | **What's ingested, from where, when — and what's missing** | Before any ingest, or when asking "do we have X?" |

**Starting a new AI thread?** *"Read `docs/01_STATE_OF_PLAY.md` and `docs/02_DECISIONS.md`, then [your question]."* That's the whole handoff.

---

## Folder layout

```
docs/
├── 00–05   Orientation      ← start here, numbered = reading order
├── 10–11   Concepts         ← reference; how to think about a thing
├── 20–22   Open fixes        ← work not yet done
├── 40–42   Next-phase specs  ← Second Brain, markdown reconciliation, Brain DB
├── 30      Dated notes      ← Q&A snapshots, freshness matters
├── specs/                   ← historical build specs (all delivered)
├── archive/                 ← superseded, do not act on
├── User Guided Docs/        ← YOUR guides, written for you not for AI
└── datasette_plugins/       ← plugin notes
```

**Numbering means reading order, not importance.** Dates in filenames mean the content is a snapshot — check freshness before trusting it.

---

## Current documents

### Orientation
- `01_STATE_OF_PLAY.md` — status, open work, daily setup
- `02_DECISIONS.md` — 14 decisions with their rejected alternatives
- `03_INDEX_themes_and_domains.md` — by workspace, by editor, by subsystem; solved vs open
- `04_HISTORY_how_we_got_here_2026-08-09.md` — timeline from recovery to working system
- `05_INGESTION_LEDGER.md` — **the ingestion journal.** Sources, dates, counts, gaps. Regenerate rather than edit.

### Concepts
- `10_CONCEPTS_label_tag_project.md` — project label vs tag vs `project_id`. **Read before tagging.**
- `11_CONCEPTS_neo4j_and_interfaces.md` — which interface when; what Neo4j is actually for

### Open fixes
- `41_SPEC_markdown_reconciliation.md` *(2026-08-14)* — `md-scan`: find which scattered `.md` files are already in the vault. ~4–5h, Sonnet mid.

Previously open, now done. The four that lived here (`20_FIX_project_identity.md`, `21_FIX_scope_consistency.md`, `22_FIX_tag_browsing_ux.md`, `20_FIX_naming_project_kinds.md`) are done — moved to `archive/` with resolution notes. `20_FIX_naming_project_kinds.md` (2026-08-12) renamed `projects.kind` vocabulary (`workspace`→`code_workspace`, `desktop`→`claude_project`, `scratch`→`no_project`), renamed the `claude_web` platform to `claude_desktop`, deleted the `not_found` tag, and added `v_model_coverage`. See `01_STATE_OF_PLAY.md` for what's still genuinely open (the `~/.claude/projects` ingestion question is real and unresolved, but it's a data-source gap, not a code fix, so it isn't a `FIX_*` doc).

### Next-phase specs
- `40_SPEC_second_brain_v0.md` — stash / brief / extract. **Parts A+B built 2026-08-16** (`chatvault stash`, `chatvault brief`); Parts C/C2/D/E remain a deliberate "use it for two weeks first" decision, not started.
- `41_SPEC_markdown_reconciliation.md` — `md-scan`, four verdicts, non-destructive.
- `42_SPEC_brain_db.md` — **the architecture decision.** Two layers: stash in ChatVault, a separate `brain.sqlite`. Revises `40_SPEC` §0. **Layer 2 schema + `promote`/`add`/`list`/`show`/`search` built 2026-08-16** (`chatvault brain ...`); `--extract`/`relate`/`supersede`/`confidence` and the Datasette/MCP/launcher interfaces not started.

### Notes
- `30_NOTES_2026-08-10.md` — id vs uid, Datasette display, tagging workflow, venvs, missing platforms

---

## User Guided Docs — a different audience

Written **for you**, in plain language, cheat-sheet style. Useful to an AI but not the project's technical documentation.

- `CHATVAULT_FIRST_SESSION.md` — a guided hour at the keyboard
- `CHATVAULT_CHEATSHEET.md` — how to think about the workflow
- `CHATVAULT_KEYWORD_SEARCH_GUIDE.md` · `CHATVAULT_SEMANTIC_SEARCH_GUIDE.md`
- `CHATVAULT_TAGGING_GUIDE.md` · `CHATVAULT_EXPORT_GUIDE.md`
- `CHATVAULT_DATASETTE_GUIDE.md` — views, columns, dates, plugins
- `CHATVAULT_LAUNCHERS_AND_ENVIRONMENT.md`
- `CHEATSHEET_ADDENDUM_effort_levels.md` — which AI model at what effort
- `Username_Topical_Areas.md` — your taxonomy seed
- `CHATVAULT_FIRST_RUN-not-updated.md` — ⚠ stale, contains placeholder numbers

---

## What changed on 2026-08-12

**Archived** *(superseded, kept for provenance)*
- `ATTACHMENTS_STRATEGY.md` — attachments are built; questions settled
- `CHATVAULT_BUILD_ORDER.md` — every step complete
- `archive/CHATVAULT_INTERFACE_STRATEGY.md` — contained a since-corrected error claiming Datasette could serve as the tagging surface

**Moved to `specs/`** — all eight build specs. Delivered; kept as the record.

**Moved** — `01_STATE_OF_PLAY.md` from *User Guided Docs* to root. It's the primary orientation document for both you and any AI, not a user guide.

**Renamed** — the old `SYNTHESIS_2026-08-09.md` is now `04_HISTORY_how_we_got_here_2026-08-09.md`, because "synthesis" didn't say what was inside it.

**New** — `00_README.md`, `02_DECISIONS.md`.

**Archived, later the same day** — `20_FIX_project_identity.md`, `21_FIX_scope_consistency.md`, `22_FIX_tag_browsing_ux.md`. All three were done (two of them well before today), but this doc set hadn't been updated to say so — an AI working from `docs/` alone had no way to know that without checking git history or the live code. Each archived file now carries a resolution banner explaining what actually landed and when. This inconsistency is why this pass happened; see `01_STATE_OF_PLAY.md` for the standing rule it prompted.

---

## Elsewhere

`~/ChatVault/STATUS.md` — Claude Code's build log. Technical, chronological, separate from these.

`~/Desktop/history-r/_LEFTOFF.md` — the recovery workspace. **ChatVault holds the results; history-r holds the method.** Check there before concluding how any data came to exist.

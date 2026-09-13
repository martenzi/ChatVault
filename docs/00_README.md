# ChatVault Documentation

**Updated:** 2026-09-09

> **Subset of the working docs.** This `docs/` tree is the design-rationale core of
> ChatVault — the specs, concepts, and decisions that explain *why* the system is
> built the way it is. A few specs reference internal working documents inline
> (e.g. a build log or ingestion ledger) that aren't part of this tree; the
> documents below are self-contained enough to stand alone.

---

## Read in this order

| # | Document | What it answers |
|---|---|---|
| **02** | `02_DECISIONS.md` | Why it's built this way — 14 decisions with their rejected alternatives |
| **10** | `10_CONCEPTS_label_tag_project.md` | Project label vs tag vs `project_id` — read before tagging |
| **11** | `11_CONCEPTS_neo4j_and_interfaces.md` | Which interface when; what Neo4j is actually for |
| **40** | `40_SPEC_second_brain_v0.md` | Stash / brief / extract — the first Brain spec |
| **41** | `41_SPEC_markdown_reconciliation.md` | `md-scan`: find which scattered `.md` files are already in the vault |
| **42** | `42_SPEC_brain_db.md` | Brain DB schema + `promote`/`add`/`list`/`show`/`search` |
| **43** | `43_SPEC_cv_content_and_brain.md` | ChatVault content vs Brain content — the boundary |
| **44** | `44_BRAIN_options_and_neo4j.md` | Brain options analysis and Neo4j's role |
| **45** | `45_SPEC_brain_v2_full.md` | Brain v2 — the full personal knowledge base spec |
| **50** | `50_SPEC_ingestion_fidelity_v1.md` | Porting the vetted JSON→Markdown identifier map into ChatVault |
| **51** | `51_SPEC_brain_parity_and_naming.md` | Brain parity and command-naming vocabulary |

**Starting a new AI thread?** *"Read `docs/02_DECISIONS.md`, then [your question]."*

---

## Folder layout

```
docs/
├── 00_README.md            ← this file
├── 02_DECISIONS.md         ← design decisions and rejected alternatives
├── 10–11   Concepts        ← reference; how to think about a thing
├── 40–51   Specs           ← next-phase and delivered build specs
├── specs/                  ← historical build specs (the original architecture)
└── datasette_plugins/      ← notes on the Datasette plugins in use
```

**Numbering means reading order, not importance.** Dates in filenames mean the
content is a snapshot — check freshness before trusting it.

---

## Concepts

- `10_CONCEPTS_label_tag_project.md` — project label vs tag vs `project_id`.
- `11_CONCEPTS_neo4j_and_interfaces.md` — which interface when; what Neo4j is actually for.

## Specs (next-phase and delivered)

- `40_SPEC_second_brain_v0.md` — stash / brief / extract. Parts A+B built.
- `41_SPEC_markdown_reconciliation.md` — `md-scan`, four verdicts, non-destructive.
- `42_SPEC_brain_db.md` — Brain DB schema + commands. *Superseded by v2 (45).*
- `43_SPEC_cv_content_and_brain.md` — the ChatVault-content / Brain-content boundary.
- `44_BRAIN_options_and_neo4j.md` — analysis, not a build target.
- `45_SPEC_brain_v2_full.md` — the full Brain v2 spec; all 6 phases built.
- `50_SPEC_ingestion_fidelity_v1.md` — ingestion fidelity against the vetted extraction tool.
- `51_SPEC_brain_parity_and_naming.md` — Brain parity, command naming, vocabulary.

## Historical specs (`specs/`)

The original architecture and the additive v1.1–v1.5 deliveries that built the
core system. `v1.x` names are **additive deliveries, not versions** — nothing
supersedes anything, except `v1.4_REVISED` which fully replaces the original v1.4.

- `CHATVAULT_ARCHITECTURE.md` — the original architecture.
- `CHATVAULT_SPEC_ADDENDUM.md` / `…_v1.1.1_PATCH.md` — addenda.
- `CHATVAULT_SPEC_v1.2_PRESENCE_AND_MEMORY.md` — presence tracking.
- `CHATVAULT_SPEC_v1.3_BROWSE_AND_EXPORT.md` — browse and export.
- `CHATVAULT_SPEC_v1.4_REVISED.md` — the revised taxonomy pass.
- `CHATVAULT_SPEC_v1.5_TAXONOMY.md` — taxonomy.
- `CHATVAULT_TAGGING_WORKFLOW.md` — the tagging workflow.

## Datasette plugins (`datasette_plugins/`)

Notes on the Datasette plugins the browse surface relies on: copyable, json-html,
pretty-json, render-markdown, and search-all.

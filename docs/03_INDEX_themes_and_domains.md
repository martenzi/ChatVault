# Index — Themes, Domains & Central Problems

**Written:** 2026-08-09
**Purpose:** structural map. What belongs where, what depends on what, what's solved and what isn't.
**Companion:** `04_HISTORY_how_we_got_here_2026-08-09.md` for the narrative

---

## 0. The two workspaces

```
~/Desktop/history-r                     ~/ChatVault
├── RECOVERY & METHOD                   ├── SYSTEM & RESULTS
├── How data was extracted              ├── What the data became
├── Editor forensics, decryption        ├── Storage, search, curation
├── Provenance of every label           ├── Consumes history-r's output
└── Mostly finished, kept as record     └── Live, in daily use
```

**The rule that keeps being forgotten:** ChatVault holds the *results*, history-r holds the *method*. A project label like `latex` exists in ChatVault with no record of how it was derived. Check history-r before concluding anything about origin.

---

# PART A — `history-r` · Recovery, by editor

## A1. Cursor
| | |
|---|---|
| **Storage** | `workspaceStorage/<hash>/state.vscdb`, composers in `globalStorage` |
| **Identity** | `workspace.json` per hash → `{"folder": …}` or `{"workspace": …*.code-workspace}` |
| **Tools** | `audit_cursor_bodies.py`, `map_cursor_composer_workspaces.py` |
| **Artefacts** | `corpus/cursor_legacy_full/` (35), `composer_workspace_map.csv` (121 rows) |
| **Known issue** | Detached composers — bodies present, absent from every workspace index |
| **Status** | ✅ recovered & ingested |

## A2. VS Code
| | |
|---|---|
| **Storage** | Same `state.vscdb` shape as Cursor |
| **Artefacts** | `corpus/vscode_legacy_full/` (132) · `export_vscode.py` |
| **Status** | ✅ recovered & ingested — best project coverage of any platform (132/132) |

## A3. Copilot (embedded in Cursor)
| | |
|---|---|
| **Artefacts** | `corpus/cursor_legacy_copilot_full/` (64 exported, 43 live) |
| **Status** | ✅ recovered & ingested. 3-conversation discrepancy unexplained, low priority |

## A4. Windsurf / Cascade
| | |
|---|---|
| **Storage** | Encrypted `.pb` protobuf |
| **Tools** | Own AES-256-GCM decryptor · `pbwire.py` + `cascade_parse.py` |
| **Bugs found** | Silent 400-turn export cap · Cascade mtime date bug (~5 months) · thin-vs-full corpus mix-up |
| **Artefacts** | `windsurf_legacy_full/` (122; 89 with model recovered) |
| **Status** | ✅ recovered & ingested. 3 conversations still have no model |

## A5. Antigravity
| | |
|---|---|
| **Storage** | Raw `.pb` (44 files, 54.3 MB) — confirmed genuinely encrypted 2026-08-14 (37% printable, no readable strings, same signature Cascade had pre-decryption). No key ever found or searched for, unlike Windsurf. |
| **Also found 2026-08-14** | 7 of 30 `brain/<uuid>` session folders carry a separate, plaintext `.system_generated/logs/overview.txt` — a full session log Antigravity itself writes. 3 already converted to readable transcripts by the user's own tool; **4 more have the same raw material sitting unconverted** — the cheapest real recovery path available, no new tooling needed. |
| **Status** | ⬜ **OPEN** — the only editor never completed. Full investigation: `history-r/2026-08-14_ANTIGRAVITY_STATUS.md` |

## A6. Cross-editor: workspace identity
| | |
|---|---|
| **The finding** | One folder ⇒ **two** workspace hashes (folder identity vs `.code-workspace` identity) that **do not share chat history** |
| **Artefact** | `Archive/Workspace_Identity_Drift_Report.md` |
| **Significance** | **This is the root cause of the original data loss.** The entire project descends from it. |
| **Status** | ✅ diagnosed · ✅ collapse-to-one-project done 2026-08-12 (`archive/20_FIX_project_identity.md` §5) |

---

# PART B — `ChatVault` · System, by subsystem

## B1. Ingest & adapters
Eleven adapters, one canonical shape (CIR). `claude_desktop`, `chatgpt`, `chatgpt_legacy_recovered`, `claude_code`, `cursor_legacy`, `cursor_legacy_copilot`, `vscode_legacy`, `windsurf_legacy`, `openclaw`, `hermes`, `generic_jsonl`.

- Idempotent — re-ingest is safe by design
- Four-level dedupe: file hash → conversation uid → continuation → block boilerplate
- Presence tracking (`reconcile`) detects vanished sources
- **⚠ `claude_code` has 2 conversations — `~/.claude/projects` confirmed never ingested (2026-08-12), 16 session files on disk. Open, not yet actioned.**

## B2. Storage & schema
`conversations` → `messages` → `blocks`, every row keeping `raw_json`. Plus `tags`/`tag_links`, `annotations`, `attachments`, `projects`, `embedding_meta`, `source_files`, `dedupe_candidates`.

- **Blocks are the atomic unit** — individual tool calls are addressable, countable, strippable
- **Never delete; filter through `v_*` views**
- `embedding_meta` got its `v_embedding_meta` view 2026-08-06 (`archive/21_FIX_scope_consistency.md`) after being the one exception that broke. The convention still needs re-checking against every *new* count, not just the ones that existed then — it recurred in `projects.backfill()`/`tags.tag_tree()` and was fixed again 2026-08-12.

## B3. Search
| Kind | Mechanism | Best for |
|---|---|---|
| Keyword | FTS5 | exact strings, error messages, filenames |
| Semantic | `sqlite-vec` + Ollama | topics, vague recollection, unnameable things |

Neither replaces the other. Semantic wants a sentence, not a keyword.

## B4. Curation
Tags (hierarchical, many-per-conversation), annotations, exclusion tiers, saved views, `curation export/import`.

**Backups protect the irreplaceable half:** content is rebuildable from `raw/`; your judgement is not.

## B5. Interfaces
`.command` menus · CLI · Textual UI (`chatvault screen`) · Datasette · MCP · markdown export.

Four thin surfaces, each doing one thing well.

## B6. Neo4j
Derived, disposable, one-way from SQLite. **Currently off, correctly.** Earns its place once tagging is substantial — tag co-occurrence for taxonomy discovery is the first real use.

## B7. Portfolio analysis
Ported from `claude_data_workspace_content`. Logic kept, plumbing replaced: reads SQL, writes tags instead of stale JSON reports.

---

# PART C — Cross-cutting themes

| Theme | Where it shows up |
|---|---|
| **Immutable source, derived everything** | RAW → DB → markdown/graph/embeddings |
| **Never delete, filter instead** | `excluded`, `superseded_by`, `orphaned`, redact-vs-purge |
| **Flag, don't guess** | `review/*` tags, `ambiguous`, dedupe review pile |
| **Measure, don't derive** | the `embed --status` bug |
| **One thing, many places** | tags over folders — the central conceptual shift |
| **Discovery vs memory** | semantic search vs tagging |
| **Provenance or loss** | history-r ↔ ChatVault separation |
| **Belief-state vs history** | memory files & project docs need versioning; conversations don't |

---

# PART D — Central problems

## Solved
| Problem | Resolution |
|---|---|
| Editor upgrades destroy chat history | Extract everything to a vendor-independent store |
| Chats scattered across 11 platforms | One canonical schema, eleven adapters |
| Same conversation exported repeatedly | Four-level dedupe, idempotent ingest |
| Windsurf `.pb` encrypted | Own decryptor + protobuf parser |
| Which workspace did a chat belong to | Hash → `workspace.json` → path, resolved and carried into the vault |
| Markdown sprawl | Markdown is a render, generated on demand |
| Can't find things by keyword | Semantic search |
| Folders force one location | Hierarchical tags, unlimited axes |
| Claude project membership | Resolved manually on disk by the user |

## Open
| Problem | Where | Size |
|---|---|---|
| **`~/.claude/projects` never ingested — confirmed, still true 2026-08-12** | tracked in `01_STATE_OF_PLAY.md` §1 | small, but a deliberate data-ingestion decision, not a code fix |
| Antigravity never recovered | history-r | unscoped |
| `claude_desktop` project links | user-driven ingest+tag | user |

## Solved since this was written
| Problem | Resolution |
|---|---|
| `project_id` never populated | `archive/20_FIX_project_identity.md` — path-keyed backfill, done 2026-08-12 |
| Two naming rules split projects (`latex` / `Latex_VS_Code_Resume_Project`) | ditto §3 |
| 13 conversations labelled `workspace.json` (Cursor scratch) | ditto §4 — 25 in the live count once every editor platform was covered |
| Windsurf `project_path_guess` was often a file, not a workspace (58 conversations) | ditto §3b — walk-up-to-project-root, found and fixed same day |
| Clone-drive username drift (`olduser` vs `user`) | ditto §5 |
| Count scope inconsistency (`embed --status`, `projects list`) | `archive/21_FIX_scope_consistency.md`, done 2026-08-06; recurrence in code written afterward (`projects.backfill`, `tags.tag_tree`/`stats`) found and fixed 2026-08-12 |
| `find --tag` vs. browsing your tag vocabulary — verb/intent mismatch | `archive/22_FIX_tag_browsing_ux.md` — `tag tree`/`show`/`review`/`stats` |

## Parked deliberately
Memory files (v1.2 Part B) · project digests/synthesis · concept extraction & the second-brain graph · web interface · full-coverage tagging *(explicitly not a goal)*

---

# PART E — Document map

**Read first:** `01_STATE_OF_PLAY.md`

| Purpose | Documents |
|---|---|
| **Orientation** | `01_STATE_OF_PLAY.md` · `04_HISTORY_how_we_got_here_2026-08-09.md` · this file |
| **Concepts** | `10_CONCEPTS_label_tag_project.md` · `11_CONCEPTS_neo4j_and_interfaces.md` |
| **User guides** | `User Guided Docs/` — first session, cheatsheet, search, export, tagging, launchers |
| **Open work** | none — see `01_STATE_OF_PLAY.md` §1 for the one remaining data-source item |
| **Historical specs** | `CHATVAULT_ARCHITECTURE.md` · addenda v1.1–v1.5 · interface & build-order strategy |
| **In history-r** | `_LEFTOFF.md` · `Workspace_Identity_Drift_Report.md` · `cursor_diagnostics/` · `corpus/CORPUS_FULL_SOT_20260731.md` |

> `v1.x` names are **additive deliveries, not versions.** Nothing supersedes anything, except `v1.4_REVISED` which fully replaces the original v1.4.

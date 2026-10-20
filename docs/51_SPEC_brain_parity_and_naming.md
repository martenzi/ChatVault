# SPEC — Brain Parity & Command Naming v1

**Written:** 2026-09-07 · Claude (Opus 5) · **Status: not started**
**Audience:** a coding model executing this end to end. **This spec is written to be executed without asking further questions** — §8 pre-answers the design decisions a coding model would otherwise raise.
**Companions:** `docs/50_SPEC_ingestion_fidelity_v1.md` (ingestion), `docs/45_SPEC_brain_v2_full.md` (Brain schema/commands as built), `docs/User Guided Docs/2nd Brain/BRAIN_FAQ.md` (plain-language rationale).

---

## 0. What this spec does, and the two user instructions behind it

**Instruction 1 — parity.** The user wants Brain to have ChatVault's full capability, by default:

> "When I asked to mirror ChatVault for the 2nd Brain, as a human I find it obvious/natural to want
> the same things... I'd rather have all functionality in both DB tools than needing to be picky and
> selective when I don't foresee any downstream problems with it."

So the default answer to "should Brain have command X?" is **yes**, and the burden of proof is on
excluding it. Exclude only where a command is meaningless against Brain's schema — not where it is
merely unfamiliar or would take effort.

**Instruction 2 — honest command names.** The user cannot currently tell which commands invoke an
AI model and which are scripted text/row manipulation, and finds `promote`, `distill` and `brief`
semantically vague. That is a legitimate criticism: the names were chosen for what they *evoke*
rather than for what they *do*.

> "Clearly I want to separate any AI work from what is simply a scripted procedure."

Both are addressed here. §1 fixes naming, §2–§5 deliver parity.

**One verified bug is folded in** (§4.1): `promote` silently drops every ChatVault tag. The user
suspected this; it is confirmed in the code.

---

## 1. Naming — separate AI inference from scripted procedure

### 1.1 The audit: what actually runs a model today

Verified by reading the implementations, not the names.

| Command | What it really does | AI model involved? |
|---|---|---|
| `chatvault brief` | Calls `export.render_profile(profile='lean')` and wraps the output with the stash `why` + source reference. Pure string assembly. | **No** |
| `brain promote` | Reads a vault conversation, renders it with `render_profile(profile='lean')`, INSERTs a frozen copy into `brain.sqlite` with a `source_uid` pointer. Pure copy. | **No** |
| `brain distill` | POSTs the conversation body to a local Ollama `/api/generate` with an extraction prompt; the model writes the output. | **Yes** — `mistral:latest` by default |
| `chatvault search --semantic` / `brain embed` | Ollama `/api/embeddings` (`nomic-embed-text`). Vector maths, no text generation. | **Embedding only** |
| everything else | SQL and string handling | No |

So the user's deduction was correct on two of three: `brief` and `promote` are scripted. `distill`
is the one command that genuinely hands content to a language model and lets it write prose.

### 1.2 The naming rule this spec introduces

**Three tiers of honesty, enforced in the command surface:**

1. **Scripted commands** get verbs describing the *mechanical operation* — `copy`, `pack`, `render`.
   No verb that implies comprehension.
2. **Embedding-based commands** keep their existing names but their `--help` must say
   "uses a local embedding model (no text is generated)".
3. **Generative-AI commands** live in their own rich-help panel titled
   **"AI-assisted (a local language model writes the output)"**, must accept `--model`, must print
   the model name and the Ollama endpoint before running, and must never be the default path of any
   other command.

**No command may mix tiers.** A scripted command must never silently call a model, and a generative
command must never be reachable as a flag on a scripted one. (This is why `distill` was correctly
made a standalone command rather than `promote --extract` — that earlier decision stands and is now
a general rule.)

### 1.3 The renames

| Old | New | Rationale |
|---|---|---|
| `brain promote` | **`brain copy conversation`** | It copies a conversation from one database to another. "Promote" is a reward word implying evaluation the command does not perform. The user's objection to bare `copy` was that it is generic and confusable with system copy — solved by the mandatory noun in the `<verb> <noun>` grammar: `copy conversation` cannot be mistaken for `cp`. |
| `brain promote --from-stash` | **`brain copy stashed`** | Same verb, different noun. Reads as English. |
| `brain distill` | **`brain summarise conversation`** (alias `summarize`) | It hands the conversation to a language model and asks it to write structured notes. That is a summary, produced by a model reading the content. "Distill" implies a systematic reduction procedure — which, as the user correctly noted, is not what happens; the model performs semantic analysis. Lives in the AI-assisted panel. |
| `chatvault brief` | **`chatvault pack`** | It assembles a self-contained context pack (why + source + rendered body) for pasting into another AI or reading standalone. "Brief" is ambiguous between the noun (a document) and the adjective (short), and implies summarisation it does not do. `pack` names the artefact. |

**Deliberately NOT renamed:**

- `stash` — means "set aside for later," which is exactly what it does. The user never flagged it,
  and it is the one metaphor here that is accurate.
- `jot` / `inbox` / `accept` / `reject` — all concrete and unambiguous.
- `pick` / `variants` / `tree` — describe retrieval shapes accurately.

**The user may still want to change any of these.** §1.4 makes that cheap.

### 1.4 Migration: aliases, deprecation, no breakage

- Register the old names as **hidden aliases** that execute the new command and print, to stderr:
  `note: 'brain promote' is now 'brain copy conversation' — the old name still works for now.`
- Aliases are hidden from `--help` so the surface reads clean, but nothing the user has typed before
  or scripted breaks.
- Keep them for one release cycle; remove only on the user's explicit say-so. Record the removal
  date as unset in STATUS.md rather than guessing one.
- **Implement renames as a thin CLI-layer concern.** The underlying functions in
  `brain/entries.py` (`promote`, `promote_from_stash`) and `brief.py` may keep their internal names
  or be renamed with module-level aliases — but do not rename them in a way that requires touching
  the 46 existing brain tests. Naming is a user-facing decision, not a refactor mandate.

### 1.5 `--help` text each renamed command must carry

Write these substantially as given; they are the fix for the user not knowing what a command does.

- **`brain copy conversation`** — "Copy a conversation from ChatVault into the Brain as a frozen
  snapshot. Scripted: no AI model is used. The Brain keeps its own copy of the text, plus a pointer
  back to the vault, so the Brain still works if the vault is unavailable."
- **`brain copy stashed`** — "Copy every conversation you've stashed in ChatVault that isn't in the
  Brain yet. Scripted: no AI model is used."
- **`brain summarise conversation`** — "Ask a local language model to read a conversation and write
  structured notes from it (facts, decisions, methods). **This runs an AI model** — it will say
  which one before it starts. Output is the model's writing, not an extract of yours; review it."
- **`chatvault pack`** — "Assemble a self-contained context pack for one or more stashed
  conversations: your `why`, the source reference, and the rendered conversation. Scripted: no AI
  model is used. Intended for pasting into another AI or reading on its own."

---

## 2. Parity — the mirror gap, decided

Full surface comparison, verified by reading both CLIs.

**ChatVault top-level:** `doctor · init · backup · ledger · brief · inventory · ingest · reconcile · verify · audit · find · export · note · exclude · restore · dedupe · search · stats · show · screen · embed · portfolio-analyze`
plus sub-typers `tag (+rule) · view · graph · sources · curation · attachments · projects · stash · brain`

**Brain today:** verb groups `add · edit · show · list · remove · archive · restore · tag · untag · rename · merge · link · unlink · review · stats · import`, plus `find · search · embed · tree · pick · variants · promote · distill · due · done · stale · orphans · jot · inbox · accept · reject · verify · backup · export`

### 2.1 Decision table

Default is **build it**. "Skip" requires a stated structural reason.

| ChatVault capability | Brain today | Decision | Notes |
|---|---|---|---|
| `doctor` | ✗ | **Build** | Checks Ollama reachability, DB integrity, migration state, orphaned FTS rows. Brain has more moving parts than ChatVault here (two DBs, one optional). |
| `note` (annotations) | ✗ | **Build** as `brain add note <ref>` | Brain has `why` (one, mandatory, at creation) and `history` (edit log) but no free-form running commentary. Needs a `notes` table — migration `003_brain_parity.sql`. |
| `exclude` / `restore` (soft-hide, D3) | partial | **Build** | Brain has `archive`/`restore` (lifecycle) but not `exclude` (hide from views without asserting the entry is finished). Add `items.excluded` + a `v_items` predicate, mirroring ChatVault exactly. |
| `dedupe` | ✗ | **Build** | Real need: `copy conversation` guards on `source_uid`, but hand-added entries, jots, and model-written summaries can duplicate freely. Port ChatVault's content-hash comparison; **surface candidates only, never auto-merge** (D7). |
| `screen` (Textual TUI) | ✗ | **Build** | The user's stated preferred surface. Port `screen_state.py`'s pattern: business logic unit-tested without Textual. This is the largest single item; it is Phase 4. |
| `view` (saved queries) | ✗ | **Build** | Directly useful for a curated store — "my CV variants", "unreviewed methods". |
| `stats --by <axis>` | partial (`edits`, `usage`) | **Build** | Add `--by kind/tag/collection/status/confidence/sensitivity`. |
| `curation export/import/diff` | partial (`export`) | **Build** | Round-trippable backup of tags/links/collections, matching ChatVault's verified export→wipe→re-import cycle. |
| `attachments` | table exists, no CLI | **Build** | Migration 002 created `attachments`; nothing writes to it. Close the half-built feature — the same class of gap `44_SPEC` flagged for tags. |
| `redact` / `purge` | ✗ | **Build** | Surgical removal for a *personal* store matters more than for an archive. Must obey the same "only with `--confirm`" rule as `remove entry`. |
| `tag rule` (auto-apply) | ✗ | **Build** | Apply on `add entry` / `accept`. Consistent with ChatVault; reversible via `untag --source rule`. |
| `graph` (Neo4j projection) | ✗ | **Build the command, do not run it** | `44_SPEC` set the threshold at ~200+ `item_links` edges. Build `brain graph rebuild/query` so it exists when the threshold is crossed; `doctor` reports current edge count against the threshold. |
| `find --pick` numbered cache | different | **Unify** | ChatVault caches numbered results with content-based invalidation (`PRAGMA data_version`); Brain recomputes row numbers per call. Keep Brain's behaviour (it is deliberate and avoids the cache-invalidation bug found on 2026-08-16) but **document the difference in both `--help` texts** so the user is never surprised. This is the one place parity is deliberately not literal. |
| `ingest` / `inventory` / `sources` / `reconcile` / `ledger` | `import markdown/yaml/csv` | **Skip, with one addition** | These manage *discovered source files on disk*. Brain's content arrives by deliberate act, not by scanning. **But add `brain ledger`** — a provenance table of where entries came from (`chatvault` copy, `summarise`, `import markdown`, `jot`, manual), which is the genuinely useful half. |
| `portfolio-analyze` | ✗ | **Skip** | Keyword-scoring over conversation corpora. Brain entries are already curated; running a scorer over them inverts the "AI proposes, you dispose" principle. |
| `export --tree` | ✗ | **Build** | Tag-tree → nested folders. Reuses the same logic; cheap. |
| `embed` | ✓ | — | Already at parity. |

### 2.2 What Brain has that ChatVault does not

Note for symmetry; **no action required**, these are Brain-specific by design: `kinds`, `aliases`,
`collections`, `rel_types`/`link --as`, `pick`/`variants`, `due`/`done`/`stale`/`orphans`,
`sensitivity`, `history`/`edit_count`.

---

## 3. Migration `003_brain_parity.sql`

One migration covering every schema need above. Follow `002_brain_v2.sql`'s established pattern
(own `schema_migrations` table, idempotent, dry-run against a scratch copy first).

```sql
-- Soft exclusion (D3 parity with ChatVault)
ALTER TABLE items ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0;
-- v_items must gain: AND excluded = 0

-- Free-form running commentary, distinct from the single mandatory `why`
CREATE TABLE IF NOT EXISTS notes (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  text       TEXT    NOT NULL,
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notes_item ON notes(item_id);

-- Saved queries
CREATE TABLE IF NOT EXISTS views (
  id         INTEGER PRIMARY KEY,
  name       TEXT    NOT NULL UNIQUE,
  query      TEXT    NOT NULL,
  description TEXT,
  created_at TEXT    NOT NULL
);

-- Auto-tag rules (mirrors ChatVault migration 007)
CREATE TABLE IF NOT EXISTS tag_rules (
  id         INTEGER PRIMARY KEY,
  pattern    TEXT    NOT NULL,
  tag        TEXT    NOT NULL,
  field      TEXT    NOT NULL DEFAULT 'title',   -- title | body | why
  created_at TEXT    NOT NULL,
  UNIQUE(pattern, tag, field)
);

-- Provenance ledger
ALTER TABLE items ADD COLUMN origin_detail TEXT;  -- e.g. 'copy:chatvault', 'summarise:mistral:latest', 'import:markdown', 'jot'
```

**`v_items` must be recreated** to add `excluded = 0`; do it explicitly, do not assume the view
updates itself. Verify by counting before and after with a deliberately-excluded row.

---

## 4. The tag-carryover bug — verified, and the fix

### 4.1 What is actually broken

`brain/entries.py::promote()` INSERTs `kind, title, why, body, source_kind, source_uid,
source_meta, status, created_at`. **It never reads the vault conversation's tags and never writes
`tag_links` in the Brain.** Confirmed by reading the function; this is the silent failure the user
suspected.

Consequence today: a conversation carefully tagged `tech/sqlite`, `project/chatvault`,
`review/model-unknown` in the vault arrives in the Brain with **zero tags**, so
`brain list entries --tag tech/sqlite` cannot find it. Nothing errors. Nothing warns.

### 4.2 Fix

`copy conversation` (née `promote`) must:

1. Read the source conversation's tags from the vault.
2. For each, ensure a Brain tag of the same name exists (create if absent), and link it.
3. Stamp each carried link `source='chatvault'` so `untag --source chatvault` reverses the whole
   carryover in one command, exactly like ChatVault's `untag --source rule|llm|folder_import`.
4. Apply the same to `copy stashed`.
5. Add `--no-tags` to opt out, and `--tag <t>` to add extra tags at copy time.

### 4.3 The tag-vocabulary mismatch, and the user's instruction on it

Some vault tags are meaningless in the Brain — `review/model-unknown`, `review/project-guessed`,
`agent-app:*`, `chatgpt-account:h1` describe *ingestion provenance*, not knowledge.

The user's instruction here is explicit and permissive:

> "If there may be an issue with model tags or similar that is not currently active or used in
> 2nd Brain, then you either make up a clever workaround and use first principles to apply a fix.
> It is not the end of the world if 2nd Brain DB has chats that is missing a particular tag that
> was set in ChatVault."

**First-principles resolution: carry everything, namespace what doesn't belong, delete nothing.**

- Tags in the vault's **curation namespaces** — `review/*`, `agent-app:*`, `chatgpt-account:*`,
  `stash/*` — are carried in but prefixed `vault/` (e.g. `vault/review/model-unknown`).
- All other tags carry across **unchanged**, because they are the user's own knowledge taxonomy and
  the whole point is that it is shared.
- The `vault/` prefix means provenance tags are visible when wanted, sort together, are trivially
  removable (`brain untag entry <ref> --prefix vault/`), and can never collide with a Brain tag the
  user creates.
- **No tag is ever silently dropped.** If a tag cannot be carried for any reason, the command
  prints it. Silent loss is the failure mode this whole section exists to remove.

### 4.4 Also fix while here: `copy conversation` hardcodes `profile="lean"`

`promote()` calls `render_profile(..., profile="lean")` with no way to change it. Under spec 50's
corrected tier definitions, `lean` means dialogue + thinking + tool calls + failures — a good
default for a knowledge store. But add `--profile read|lean|full` so the user can copy a stripped
or a forensic version deliberately. Record which profile was used in `source_meta`.

---

## 5. Acceptance criteria

Verified against the real `brain.sqlite` and a scratch copy of the real `chatvault.sqlite`, per D14.

| # | Criterion | Proof |
|---|---|---|
| 5.1 | Old names still work | `brain promote --uid X --why "..."` succeeds and prints the deprecation note to stderr. |
| 5.2 | AI/scripted separation is visible | `brain --help` shows `summarise conversation` under an "AI-assisted" panel and `copy conversation` outside it. `brain summarise conversation` prints the model name and endpoint before generating. |
| 5.3 | No scripted command touches a model | With Ollama **stopped**, every command except `summarise`, `embed` and `search --semantic` completes normally. Run the whole surface; assert no network call. |
| 5.4 | Tags carry across | Tag a real vault conversation with 3 tags including one `review/*`; `copy conversation`; assert the Brain entry has all 3, that the `review/*` one is `vault/`-prefixed, and that `untag --source chatvault` removes exactly those 3 and nothing else. |
| 5.5 | Nothing is silently dropped | Any tag not carried is printed. Assert with a deliberately unmappable tag. |
| 5.6 | `--profile` on copy | `copy conversation --profile full` produces a strictly larger body than `--profile read` for the same conversation. |
| 5.7 | Soft exclusion works | An `excluded` entry disappears from `list entries`/`find`/`export` and reappears via `restore`; row count unchanged throughout. |
| 5.8 | Parity surface exists | Every "Build" row in §2.1 has a working command with `--help`. |
| 5.9 | Dedupe proposes, never disposes | `brain dedupe` on a Brain containing two identical entries reports both and modifies nothing without `--confirm`. |
| 5.10 | Integrity | `PRAGMA integrity_check` = `ok` on both DBs; `chatvault stats` on the vault byte-identical before/after every Brain operation; full test suite green. |

---

## 6. Build order

| # | Phase | Contents | Gate |
|---|---|---|---|
| 1 | **Naming + tag carryover** | §1.3–§1.5 renames with hidden aliases, §4 tag carryover + `--profile` | 5.1, 5.2, 5.4, 5.5, 5.6 |
| 2 | **Migration `003_brain_parity.sql`** | §3, applied to scratch then production, backup first | 5.7, 5.10 |
| 3 | **Parity commands, batch A** | `doctor`, `add note`, `exclude`/`restore`, `stats --by`, `ledger`, `export --tree` | 5.8 partial |
| 4 | **Parity commands, batch B** | `dedupe`, `view`, `curation export/import/diff`, `attachments`, `redact`/`purge`, `tag rule`, `graph` (built, not run) | 5.8, 5.9 |
| 5 | **`brain screen`** | Textual TUI, business logic in a testable `brain_screen_state.py` | 5.8 complete |

Phases 1 and 2 are the ones that fix real breakage; 3–5 are capability. **Ship phase 1 before
starting spec 50's work** — it is small, and it stops the tag loss immediately.

---

## 7. Non-goals

- Do not merge the two databases. The two-layer architecture (`42_SPEC_brain_db.md` §0) stands:
  ChatVault is the exhaustive archive, Brain is the curated, portable, irreplaceable store.
- Do not make Brain depend on the vault at runtime for anything except `copy`/`summarise`.
  Portability — Brain working with `chatvault.sqlite` absent — is an existing verified guarantee.
- Do not add AI auto-classification anywhere. "AI proposes, you dispose" (`46_AUDIT` §6).
- Do not run `brain graph` or register MCP without the user's explicit go-ahead (MCP mutates
  `~/.claude.json`, outside this repo).

---

## 8. Pre-answered questions — read before asking the user anything

These are the decisions a coding model would reasonably stop and ask about. They are decided.

**Q: Rename the Python functions too, or only the CLI?**
CLI only. `brain/entries.py::promote()` may keep its name with a module-level alias. Renaming
internals would churn 46 passing tests for zero user-visible benefit.

**Q: `summarise` or `summarize`?**
Primary spelling `summarise`; register `summarize` as a visible alias. The user writes British-ish
English; both must work.

**Q: What if the user later dislikes these names?**
The alias mechanism in §1.4 makes any further rename a one-line change. Do not treat these names as
load-bearing. If you find a clearly better name while implementing, note it in STATUS.md — do not
adopt it unilaterally.

**Q: Should `summarise conversation` also work on Brain entries, not just vault conversations?**
Yes — `brain summarise entry <ref>` as well. Same AI panel, same `--model`, same disclosure. It is
the obvious next request and costs almost nothing once the first exists.

**Q: Which Ollama model should be the default?**
Keep `mistral:latest` (the existing default). Do not change it as part of this spec. Add `--model`
everywhere and have `doctor` list locally available models so the user can choose informedly.

**Q: What if a vault tag name collides with an existing Brain tag of different meaning?**
Non-curation tags carry through unchanged and merge with the existing Brain tag. This is intended —
a shared taxonomy is the point. Curation-namespace tags get the `vault/` prefix and therefore cannot
collide. If a collision would occur anyway, print it and carry it prefixed.

**Q: Should `exclude` and `archive` be merged since they overlap?**
No. `archive` asserts "this is done"; `exclude` asserts "hide this from my views for now" without
that claim. ChatVault keeps them separate for the same reason. Both filter through `v_items`.

**Q: `brain dedupe` — auto-merge duplicates?**
Never. Report candidates; act only under `--confirm` on an explicit pair. D7.

**Q: Does `brain screen` need to reach the vault?**
No. It operates on `brain.sqlite` alone. Copying from the vault stays a CLI act.

**Q: Migration number — is 003 free?**
**Check `schema_migrations` in the real `brain.sqlite` first.** A prior spec of mine quoted a
migration number that was already taken; the lesson is recorded in STATUS.md. Use the next actually-
free number and correct this document if it is not 003.

**Q: The spec says "build the graph command but don't run it" — build against what?**
Build `brain graph rebuild/query` mirroring `chatvault graph`, with `item_links` as edges and
`items` as nodes. Verify against a scratch Brain populated with synthetic links. Report the real
`item_links` count against the ~200-edge threshold in `doctor`, and stop there.

**Q: Order relative to spec 50?**
Phase 1 of this spec first (small, fixes active data loss). Then spec 50 in full. Then phases 2–5
here. Spec 50's Phase 1 changes `render_profile`'s block filtering, which `copy conversation`
consumes — so if phases interleave, re-verify 5.6 after spec 50's Phase 1 lands.

---

## 9. Update these docs when done

- `STATUS.md` — build-log row per phase; close the naming/parity open items.
- `docs/45_SPEC_brain_v2_full.md` — update the command surface to the new names.
- `docs/User Guided Docs/2nd Brain/BRAIN_FAQ.md` — already updated by
  `docs/User Guided Docs/2nd Brain/COMMANDS_WHAT_THEY_ACTUALLY_DO.md`; cross-link the two.
- `docs/02_DECISIONS.md` — add a decision recording the AI-vs-scripted naming rule (§1.2), with
  rejected alternatives (evocative names; a global `--ai` flag; a separate `chatvault-ai` binary).

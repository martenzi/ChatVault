# Spec — Second Brain v0 (Stash & Brief)

**For:** Claude Code · **Sonnet, mid effort** · ~1 day total, buildable in three independent parts
**Written:** 2026-08-12
**Read first:** `02_DECISIONS.md`, `10_CONCEPTS_label_tag_project.md`

> **Update, 2026-08-16 — Parts A and B built, exactly as scoped in §7, and stopped there.** `chatvault stash add/list/show/drop` and `chatvault brief --pick` are live. No new table (as specified) — verified `chatvault init` reports "schema already up to date" against a scratch copy of production. Full detail: STATUS.md build log for 2026-08-16. Parts C/C2/D/E, `42_SPEC_brain_db.md`'s Brain DB, and `41_SPEC_markdown_reconciliation.md` remain explicitly not built, per this doc's own §7 sequencing ("use A+B for two weeks" before deciding).

---

## 0. The design question, answered

> **ChatVault is exhaustive. Second Brain is selective.**
> ChatVault answers *"where did I discuss X?"* Second Brain answers *"what did I decide, and can I pick it up again?"*

Those are different intents, but **not different databases.**

### Same database, separate face — and why

| Approach | Verdict |
|---|---|
| **Separate DB** | ✗ Golden conversations live in ChatVault. A second DB means either duplicating them (sync problem forever) or storing cross-database pointers (fragile). |
| **Separate tool** | ✗ Rebuilds search, tags, FTS, embeddings, export — all of which already exist and work. |
| **Same DB, separate namespace + views + commands** | ✓ Zero duplication, everything reused, and it still *feels* like a distinct place because the commands and views are its own. |

> ### ⚠ REVISED 2026-08-14 — read `42_SPEC_brain_db.md` before acting on this section
>
> The table above was under-argued. "Cross-database pointers are fragile" was **overstated** — SQLite `ATTACH` makes cross-DB joins trivial, and keying on `uid` (stable) rather than rowid removes the fragility entirely. The duplication objection only holds if the copy is meant to stay in sync; it isn't.
>
> **The decisive argument runs the other way:** ChatVault is rebuildable from `raw/`; the Brain is not. Keeping irreplaceable curation inside a disposable, frequently-migrated database exposes it to every migration and re-ingest.
>
> **Current architecture: two layers.** Layer 1 (`stash` + `why`) stays in ChatVault and is specced below — build it as written, it's correct and unaffected. Layer 2 is a separate `brain.sqlite`, specced in `42_SPEC_brain_db.md`.

**Superseded decision (kept for the record):** one `chatvault.sqlite`, two faces.

---

## 1. What Second Brain adds that ChatVault lacks

ChatVault can already find, filter and export. Three things are genuinely missing:

1. **A deliberate keep gesture** — "this one is gold, don't lose it" — distinct from tagging by topic
2. **The reason it's gold**, captured at the moment of saving, in the user's own words
3. **Branching** — take a saved conversation and use it as *context to start a new one*. This is the feature that makes it a brain rather than a shelf.

Point 3 is the whole point. The user described it exactly: *"pick that conversation and save it… then duplicate and branch out a new conversation by supplying the context."*

---

## 2. Part A — Stash (the keep gesture)

### Storage

No new table for conversations. A reserved tag namespace plus one annotation:

```
stash/                     everything kept
stash/method               a way of doing something worth reusing
stash/decision             a conclusion reached, worth not re-deriving
stash/reference            facts or explanations worth returning to
stash/thread               a conversation worth continuing later
```

**The `why` is mandatory** and goes in `annotations` with `target_type='conversation'`. Storing *why* matters more than which sub-tag — six months later the note is what makes it findable and reusable.

### Commands

```bash
chatvault stash add --pick 3 --why "the protobuf breakthrough — walking the mapping tree" [--kind method]
chatvault stash list [--kind method] [--since DATE]
chatvault stash show <n>            # the why + the conversation
chatvault stash drop <n>            # removes stash tag + note; conversation untouched
```

`stash list` output — **the `why` is the primary column**, not the title:

```
 #   SAVED       KIND       WHY                                          SOURCE
 1   2026-08-12  method     protobuf breakthrough — walking mapping tree  windsurf · 2026-02-15
 2   2026-08-11  decision   why SQLite over Neo4j, with the reasoning     claude_desktop · 2026-07-30
 3   2026-08-09  thread     ADHD medication strategy — unfinished         claude_desktop · 2026-05-02
```

`stash add` must refuse an empty `--why`. A stash entry without a reason is a bookmark, and bookmarks rot.

---

## 3. Part B — Brief (the branching gesture) ← the important one

**`brief` turns a saved conversation into a context pack you can hand to a fresh AI conversation.**

```bash
chatvault brief --pick 1                       # to stdout, ready to paste
chatvault brief --pick 1 --out ~/Desktop/      # to a file, ready to attach
chatvault brief --pick 1,4 --out ~/Desktop/    # combine several
chatvault brief --pick 1 --summarise           # optional AI condensation (see §3.2)
```

### 3.1 Output format

```markdown
# Context: protobuf breakthrough — walking the mapping tree

**Why this was saved:** figured out that Windsurf's .pb files need the mapping
tree walked from root to current_node, not the flat list.

**Source:** windsurf · 2026-02-15 · project: history-r · 34 messages
**Vault reference:** windsurf:0ca82ccc-503e-49e9-949a-153a91fde0cc

---

[lean-profile conversation body — thinking dropped, long tool payloads trimmed,
 user/assistant prose complete and verbatim]

---
*Context pack generated from ChatVault 2026-08-12. Reply as if continuing this work.*
```

**Reuse the existing `lean` export profile.** Do not write a new renderer — `lean` was built for exactly this (feeding an AI, prose intact, machinery trimmed).

The header is the new part, and it is what makes the pack usable: without *why*, a fresh model has the transcript but not the point.

### 3.2 `--summarise` (optional, build last)

Calls a local or API model to produce a 200–400 word condensation, inserted above the body under **"What was concluded."** Cache it in the `digests` table (§4) so it's generated once.

**Not required for v0.** The raw pack works; this is convenience.

---

## 3c. `stash extract` — distil the facts (the third gesture)

**Added 2026-08-14.** The user identified a case that neither `stash` nor `brief` covers:

> *"When I review chats seeking content, I discover that 1–2 out of 5–8 found contain the factual gold I want to persist… in Second Brain I want to focus on factual content and reduce noise plus a different kind of cognitive organisation."*

`stash add` keeps a **pointer**. `brief` produces a **context pack** for continuing the work. Neither produces **knowledge separated from the dialogue that generated it** — which is what a second brain is actually made of.

```bash
chatvault stash extract --pick 3 --why "npm/docker interaction that broke my PATH"
chatvault stash extract --pick 3,7 --merge --why "everything I know about workspace hashes"
```

**What it does:** feeds the `lean` export to a model with a strict extraction prompt, and stores the result as a `stash_item` (§4) with `source_conv_id` pointing back at the conversation.

### The extraction prompt — strict, and the strictness is the point

```
From this conversation, extract only durable factual content:

- Facts established (with the reasoning if it isn't obvious)
- Decisions made, and what was rejected
- Methods that worked — enough detail to repeat them
- Things that turned out to be false or dead ends

Exclude entirely: pleasantries, restatements, the model's process narration,
anything superseded later in the same conversation, and anything that is only
true about that moment.

Write as standalone notes. A reader must not need the conversation to
understand them. If a claim depends on context, state the context.
If nothing durable is present, say so — do not manufacture content.
```

That last line matters. **Most conversations contain no durable facts, and the extractor must be allowed to say so** rather than producing filler because it was asked to produce something.

### The resulting shape

| | ChatVault conversation | Second Brain item |
|---|---|---|
| Is | *how you got there* | *what you know* |
| Contains | dialogue, dead ends, process | facts, decisions, methods |
| Length | 5,000 words | 300 words |
| Read for | reconstructing the reasoning | using the conclusion |
| Written by | you and a model, live | a model, deliberately, afterwards |

**`source_conv_id` keeps them joined.** The item is the knowledge; the conversation stays available underneath as the full derivation. Nothing is lost by distilling, which is what makes distilling safe.

### Ordering note

`extract` **depends on Part C** (`stash_items`), so it can't ship with Parts A and B. That's fine — A and B are the loop that proves the habit. Build `extract` when you find yourself wanting the distillation rather than the pointer.

**Cheap first approximation available immediately:** `brief --pick 3 | pbcopy`, paste into a chat, ask for the extraction, save the answer. If that manual loop feels worth automating after a few tries, build `extract`. If it doesn't, you've learnt something for free.

---

## 4. Part C — Non-chat items (build only if wanted)

The user described the Second Brain as broader than chats — *"an expanded life library… a hybrid toolbox for things."* One table covers it:

```sql
CREATE TABLE stash_items (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,          -- note | link | snippet | file | method
  title       TEXT NOT NULL,
  why         TEXT NOT NULL,          -- mandatory, same rule as conversations
  body        TEXT,                   -- markdown
  url         TEXT,
  source_conv_id INTEGER REFERENCES conversations(id),   -- if it came from a chat
  created_at  TEXT NOT NULL,
  updated_at  TEXT,
  excluded    INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE stash_items_fts USING fts5(
  title, why, body, content='stash_items', content_rowid='id',
  tokenize='porter unicode61');

CREATE VIEW v_stash_items AS SELECT * FROM stash_items WHERE excluded = 0;

-- optional, for §3.2 and future project synthesis
CREATE TABLE digests (
  id          INTEGER PRIMARY KEY,
  scope       TEXT NOT NULL,          -- 'conversation' | 'project' | 'tag'
  scope_ref   TEXT NOT NULL,
  text        TEXT NOT NULL,
  model       TEXT,
  created_at  TEXT NOT NULL,
  superseded_by INTEGER REFERENCES digests(id)
);
```

`tag_links` and `annotations` accept `target_type='stash_item'` — already polymorphic, no change needed.

```bash
chatvault stash note "title" --why "..." [--body-file f.md] [--from-conv <n>]
chatvault stash link <url> --why "..."
chatvault stash search "<query>"      # FTS across conversations AND items
```

**`--from-conv` is the valuable one:** it captures a conclusion *extracted from* a conversation while keeping the pointer back to where it came from. That's how a chat becomes a durable note without losing provenance.

---

## 5. A separate face, not a separate tool

The Second Brain should *feel* distinct even though it shares a database:

- **`chatvault brain`** — its own Textual screen: stash list, why-first, `b` to brief, `n` to add a note. **Not** the tagging screen.
- **Datasette:** a `v_stash` view (why · kind · saved · source · title) set as a facetable table
- **`second-brain.command`** — its own launcher, so it's a different door

**One database. Two doors.** Nothing syncs, nothing duplicates, and the vault stays exhaustive while the brain stays selective.

---

## 6. Explicitly out of scope for v0

Named so nobody builds them speculatively:

- Concept extraction / auto-linking between stash items
- The Neo4j second-brain graph — **this is where it finally earns its place**, but only once there are enough items to traverse
- Semantic search over stash items *(inherited free once they're embedded — no work)*
- Obsidian sync
- Any privacy-tiered personal-knowledge layer

---

## 7. Build order

| Part | What | Effort | Model | Status |
|---|---|---|---|---|
| **A** | `stash add/list/show/drop` — tags + mandatory why | ~2h | Sonnet, low–mid | ✅ Built 2026-08-16 |
| **B** | `brief` — context pack from `lean` + header | ~2h | Sonnet, mid | ✅ Built 2026-08-16 |
| — | *use it for two weeks* | — | — | ⏳ starts now |
| **C** | `stash_items` table + note/link/search | ~3h | Sonnet, mid |
| **C2** | `stash extract` — distil facts into an item (§3c) | ~2h | Sonnet, mid |
| **D** | `chatvault brain` screen + `v_stash` + launcher | ~3h | Sonnet, mid |
| **E** | `--summarise` + `digests` | ~2h | Sonnet, mid |

**Build A and B, then stop.** They are ~4 hours and deliver the entire loop the user described. Whether C–E are worth building depends on whether the stash habit takes — and that is unknowable in advance.

---

## 8. Acceptance criteria

| Test | Passes when | Status |
|---|---|---|
| Stash requires a reason | `stash add` with no `--why` is refused | ✅ Verified 2026-08-16 (empty/whitespace/None all refused, `StashError`, CLI exit 1) |
| Non-destructive | `stash drop` removes tag + note; the conversation is untouched | ✅ Verified — conversation row byte-identical before/after, unrelated annotations untouched |
| Brief is self-contained | Output carries why, source, vault reference, and full prose — readable with no access to the vault | ✅ Verified against real production data in a scratch copy |
| Brief reuses `lean` | No second renderer exists; `brief` calls the export profile | ✅ Verified — brief's body is byte-identical to a direct `export.render_profile(profile='lean')` call |
| Round trip | `stash add` → `brief` → paste into a fresh AI chat → the model can continue the work without further explanation | ✅ Verified end-to-end against real data (see STATUS.md 2026-08-16) |
| Vault unchanged | `chatvault stats` conversation counts are identical before and after any stash operation | ✅ Verified — 3,101 before, during, and after a full add/list/brief/drop cycle |
| Search spans both | `stash search` returns hits from conversations *and* `stash_items` | ⬜ Not built — depends on Part C (`stash_items`), out of scope for this pass |
| Extract is honest | A conversation with no durable content yields "nothing durable found", not filler | ⬜ Not built — Part C2, depends on Part C |
| Extract stays linked | Every extracted item carries a working `source_conv_id` | ⬜ Not built — Part C2, depends on Part C |

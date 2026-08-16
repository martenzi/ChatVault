# Spec — Brain DB (separate database) + the two-layer architecture

**For:** Claude Code · **Sonnet, mid effort** · Layer 1 ~4h · Layer 2 ~6h
**Written:** 2026-08-14
**Revises:** `40_SPEC_second_brain_v0.md` §0 — the "same database" recommendation was under-argued

> **Update, 2026-08-16 — schema + `promote`/`add`/`list`/`show`/`search` built, by explicit user decision to skip the "two weeks first" wait in §7 (irrelevant to this user's actual workflow).** `brain.sqlite` is live: `chatvault brain promote [--uid/--pick/--from-stash]`, `brain add`, `brain list`, `brain show`, `brain search`. Not built, still: `--extract`, `relate`, `supersede`, `confidence` workflows (§7 step 4), and the Datasette/MCP/launcher interfaces (§7 step 5). All §8 acceptance criteria relevant to this scope verified directly against a scratch copy of real production data — full detail: STATUS.md build log, 2026-08-16.

---

## 0. Correcting my earlier recommendation

I argued for one database on two grounds. **One was overstated and one missed the stronger counter-argument.**

| My claim | Honest assessment |
|---|---|
| *"Cross-database pointers are fragile"* | **Overstated.** SQLite has `ATTACH DATABASE`; joining across two files is one line. Fragility comes from pointing at **rowids**, which change. Point at `uid` — stable, survives a full re-ingest — and there's nothing fragile about it. |
| *"A separate DB means duplication and sync problems"* | **True only if the copy is meant to stay in sync.** It isn't. See §2 — the copy is *deliberately frozen*, which dissolves the objection entirely. |

**And your counter-argument is stronger than either:**

> *"I'm concerned about having my second brain as a factual golden goto while I store my entire chat collection in the same db. That adds risk in having dual intentions content in the same place."*

That's correct, and there's a technical reason underneath the intuition that I should have raised myself:

> **ChatVault is rebuildable from `raw/`. The Brain is not.**

They have *opposite* recoverability. Putting irreplaceable curation inside a disposable, frequently-migrated, 3,000-conversation database means every migration, re-ingest and experiment is a small risk to the one thing that can't be regenerated. **Separating them means a vault catastrophe cannot touch the brain.**

That alone justifies two files.

---

## 1. The architecture: two layers, not two options

Build **both**. They're complementary, not competing.

```
┌─ chatvault.sqlite ────────────────┐      ┌─ brain.sqlite ──────────────┐
│  3,000 conversations              │      │  ~100 items                 │
│  exhaustive · rebuildable         │ ───► │  curated · irreplaceable    │
│                                   │ promote                            │
│  LAYER 1: stash/ tag + why        │      │  LAYER 2: frozen snapshots  │
│  = the marking gesture            │      │  + facts + notes + links    │
└───────────────────────────────────┘      └─────────────────────────────┘
```

**Layer 1 (in ChatVault):** you're browsing, you recognise gold, you mark it. `stash add --why "..."`. Fast, in-place, no decision about what to do with it yet.

**Layer 2 (Brain DB):** deliberate. Promoting into the brain is a second, considered act — and the stash tag is the queue of candidates.

**This is the workflow you described**, split correctly: *recognition* is instant and cheap; *curation* is slow and deliberate. Forcing them into one gesture makes one of them wrong.

---

## 2. The key design decision: snapshot **and** pointer

The brain stores **a frozen copy of the content plus a pointer back to the source.**

```sql
source_uid   TEXT    -- 'windsurf:0ca82ccc-…'  — stable, survives vault re-ingest
body         TEXT    -- the full lean-rendered conversation, frozen at promote time
```

This dissolves every objection to two databases:

| Concern | Resolved by |
|---|---|
| Dangling pointer if the vault changes | The body is already here. The pointer is a **convenience**, not a dependency. |
| Sync drift between copies | **There is no sync.** The snapshot is what you saved, at the moment you saved it. That's a feature — you kept *that* version. |
| Brain unusable without the vault | It's fully self-sufficient. `brain.sqlite` opens anywhere, alone. |
| No cross-DB foreign keys | Nothing depends on the link. `brain verify` reports unresolvable pointers as informational. |

**The brain is portable by construction.** One file, ~10 MB, no dependencies. Sync it to iCloud, carry it on a stick, open it on another machine — the vault stays a 400 MB local-only thing.

---

## 3. Brain schema

`brain.sqlite` — a new, small database. Deliberately not a clone of ChatVault.

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE items (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,      -- conversation | fact | method | decision | note | link | snippet
  title         TEXT NOT NULL,
  why           TEXT NOT NULL,      -- MANDATORY. your words, why this is here
  body          TEXT,               -- frozen content (markdown)
  url           TEXT,

  -- provenance, all optional, none load-bearing
  source_kind   TEXT,               -- chatvault | markdown | manual | web
  source_uid    TEXT,               -- 'windsurf:0ca82ccc-…' — stable vault key
  source_meta   TEXT,               -- JSON: platform, project, date, model, msg count
  source_path   TEXT,               -- if it came from a file

  status        TEXT NOT NULL DEFAULT 'active',   -- active | superseded | archived
  superseded_by INTEGER REFERENCES items(id),
  confidence    TEXT,               -- verified | probable | unverified
  created_at    TEXT NOT NULL,
  updated_at    TEXT
);

CREATE TABLE item_links (            -- items relate to each other
  from_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  to_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  rel      TEXT NOT NULL,            -- derived_from | contradicts | extends | see_also
  note     TEXT,
  PRIMARY KEY (from_id, to_id, rel)
);

CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE tag_links (
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  PRIMARY KEY (tag_id, item_id)
);

CREATE VIRTUAL TABLE items_fts USING fts5(
  title, why, body, content='items', content_rowid='id',
  tokenize='porter unicode61');

CREATE VIEW v_items AS SELECT * FROM items WHERE status = 'active';
```

**Three things ChatVault's schema doesn't have, because the brain needs them and the vault doesn't:**

- **`item_links`** — knowledge relates to knowledge. `contradicts` is the valuable one: it lets you record that a later finding overturned an earlier one *without deleting either*.
- **`confidence`** — a fact you verified and a fact an AI extracted are not the same thing, and six months on you won't remember which was which.
- **`status='superseded'`** — you learn better. The old item stays as history.

---

## 4. Commands

Own binary, own database, own vocabulary — so it *feels* separate:

```bash
brain promote --uid windsurf:0ca82ccc --why "…" [--kind method] [--extract]
brain promote --from-stash              # everything tagged stash/ not yet promoted
brain add "title" --why "…" [--body-file f.md] [--kind fact]
brain link <url> --why "…"

brain list [--kind method] [--tag tech/**] [--confidence unverified]
brain show <n>
brain search "<query>"
brain edit <n>                          # opens $EDITOR on the body

brain relate <a> <b> --rel contradicts --note "…"
brain supersede <old> --by <new>

brain verify                            # unresolvable source_uids, orphan links
brain backup
```

### `brain promote` — the transfer, in detail

1. Read the conversation from `chatvault.sqlite` via `ATTACH` (read-only)
2. Render with the existing **`lean`** profile
3. Store as `body`; store `source_uid` + `source_meta`
4. Mark the vault conversation `stash/promoted` so it isn't offered twice
5. `--extract` runs the strict extraction prompt (`40_SPEC` §3c) and stores the **distillation** as body, with the transcript kept as a second linked item (`derived_from`)

**`--from-stash` is the batching path**: mark freely while browsing, promote in one deliberate session.

---

## 5. The third method: subtractive curation

You raised this and it's a real third path:

> *"I could take my Chatvault and just remove all things noisy and the golden content will be left."*

**It works — but only for noise that is systematically identifiable**, and it's a ChatVault operation, not a brain one.

| Method | Works when | Cost |
|---|---|---|
| **Additive** (`stash add`) | Gold is a judgement call, one at a time | One gesture per item |
| **Subtractive** (`exclude --where`) | Noise is a *class* — a model you abandoned, a date range, boilerplate blocks | One command per class |

**They're complementary, not alternatives.** Subtractive can't find gold, because that requires reading everything; additive can't remove 500 obsolete conversations, because that's 500 gestures.

Practical sequence: **subtract the classes first** (`exclude --where "model LIKE 'gpt-5-mini%'"`), which shrinks what you browse. Then add individually from what's left. All exclusions are reversible and logged — nothing is destroyed.

---

## 6. Interfaces

- **`brain.command`** — its own launcher
- **Datasette** — point it at `brain.sqlite`, port 8002. Small enough to browse whole; facets on kind/tag/confidence
- **Textual** — a `brain` screen later, only if the CLI proves insufficient
- **MCP** — register `brain.sqlite` as a second read-only server, so an AI can consult *"what do I already know about X?"* without wading through 3,000 conversations. **This may be the single highest-value use.**

---

## 7. Build order — keeps every option open

| # | What | Effort | Why this order | Status |
|---|---|---|---|---|
| 1 | **Layer 1: `stash` + `why`** (`40_SPEC` Parts A) | ~2h | Cheap. Start marking immediately, whatever you decide later. | ✅ Built 2026-08-16 |
| 2 | `brief` (`40_SPEC` Part B) | ~2h | The branching loop; works with or without a brain DB | ✅ Built 2026-08-16 |
| — | ~~use it two weeks~~ | — | Marking is free; you learn what you actually mark | ⏭ Skipped by explicit user decision — irrelevant to this workflow |
| 3 | Brain DB schema + `promote` + `list`/`show`/`search` | ~4h | Now you have a real stash queue to promote *from* | ✅ Built 2026-08-16 |
| 4 | `--extract`, `relate`, `supersede`, `confidence` | ~3h | Only once you have enough items for relationships to matter | ⬜ Not built |
| 5 | Datasette + MCP + launcher | ~2h | Interfaces last | ⬜ Not built |

**Nothing in steps 1–2 is wasted if you later decide one database was right after all** — `stash` tags and `why` annotations are the same either way. The choice stays open until step 3, by which point you'll have evidence instead of speculation.

---

## 8. Acceptance criteria

| Test | Passes when | Status |
|---|---|---|
| Brain is self-sufficient | Rename `chatvault.sqlite`; every `brain` command still works, `verify` reports unresolvable pointers as *informational* | ✅ Verified 2026-08-16 — `brain list`/`show`/`search` all confirmed working with `chatvault.sqlite` renamed away entirely. (No `brain verify` command built this round — nothing dereferences `source_uid` at read time in `list`/`show`/`search` in the first place, so there's no unresolvable-pointer path to report on yet; deferred with `--extract`/`relate`/etc.) |
| Snapshot is frozen | Edit the source conversation in the vault; the brain item is unchanged | ✅ Verified — body hash identical after editing the source conversation's title in a scratch copy of real production data |
| Pointer is stable | Wipe and re-ingest the vault; `source_uid` still resolves | ⬜ Not verified this round — `source_uid` is the vault's own stable `uid` (`platform:native_id`), unaffected by re-ingest by construction, but no live wipe-and-re-ingest round trip was run against a promoted item |
| Promote is idempotent | Promoting the same conversation twice creates one item | ✅ Verified — both a direct `--uid` promote called twice and `--from-stash` run twice; second call is a no-op (`already_promoted`) |
| `why` is mandatory | `promote` and `add` refuse an empty `--why` | ✅ Verified — both commands, empty string and omitted flag |
| Portability | Copy `brain.sqlite` alone to another machine; it opens and searches | ✅ Verified — copied the file alone into an empty directory with no vault, no `raw/`, nothing else present; `brain list`/`search` both worked |
| Vault untouched | `chatvault stats` identical before and after any brain operation | ✅ Verified — conversation count identical (3,101) before, during, and after promote/list/show/search against a scratch copy of the real 208MB production DB |
| Extract is honest | A conversation with no durable content yields "nothing durable found", not filler | ⬜ Not built — `--extract` is explicitly out of scope this round |

---

## 9. What this leaves open, deliberately

You said you'd rather have options than verdicts. Everything here is reversible:

- **Merging later** — `brain.sqlite` can be `ATTACH`ed and copied into ChatVault in one script, if two files turns out to be a nuisance
- **Splitting later** — likewise the reverse
- **Never promoting anything** — Layer 1 alone is a working system
- **Neo4j** — the brain's `item_links` graph is small, meaningful, and hand-curated. **This is finally the dataset where a graph database earns its place** — but only once there are enough links to traverse, which is many months away

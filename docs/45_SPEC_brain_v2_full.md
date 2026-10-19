# Spec — Brain v2: full personal knowledge base

**For:** Claude Code · **Sonnet, mid effort** · ~2 days total, 5 independent phases
**Written:** 2026-08-26
**Supersedes:** `42_SPEC_brain_db.md` §3 (schema) and §4 (commands). `44_BRAIN_options_and_neo4j.md` is analysis, not a build target.
**Companion:** `BRAIN_FAQ` (internal, not in the public release) — terminology and rationale

**Build all of it.** This spec is deliberately complete rather than incremental; the user has decided on the tool and wants full functionality from the start.

> **Update, 2026-08-29 — all 6 phases built and verified against real production `brain.sqlite`.** Migration `002_brain_v2.sql` applied for real (backed up first, dry-run tested against a scratch copy first); full `brain <verb> <noun>` grammar live (`src/chatvault/brain_cli.py`, `src/chatvault/brain/` package); every §7 acceptance criterion verified directly, not assumed — see the updated table in §7 below and STATUS.md's build log for the full verification detail (commands actually run, real output). `chatvault stats` on production `chatvault.sqlite` confirmed byte-identical before/after the migration and after every brain operation exercised. One real bug found and fixed during verification: hard-deleting a parent with children raised a raw `sqlite3.IntegrityError` instead of a clear message (items.parent_id has no ON DELETE action, by design — CASCADE would silently destroy children) — `remove_entry` now checks for children first and raises a clear `BrainError`. MCP registration (§4) is written up but **not yet run** — it mutates `~/.claude.json`, a persistent config file outside this repo, so it needs the user's own go-ahead rather than being run silently; the exact command is in STATUS.md's build-log entry.

---

## 0. Naming decisions — read before writing code

Earlier drafts used jargon. These are the final names:

| Old | New | Why |
|---|---|---|
| `slug` | **`ref`** | "Slug" is web jargon for a URL fragment. `ref` = a short stable handle you type instead of an ID. |
| `attrs` | **`meta`** | Shorter, unambiguous, everyone knows it |
| `len` | **`meta.length`** | Never abbreviate. `len` reads as "lens". |
| `relate` | **`link`** | With an explicit `--as <relation>`, so the meaning is on the command line |
| `edit` | **`edit entry`** | Every command names its noun. See §3. |
| *(new)* | **`vetted_at`** | The date *you* reviewed/approved it — distinct from `created_at` |

**Command grammar, applied without exception:**

```
brain <verb> <noun> [target] [options]
```

`brain add entry`, `brain add tag`, `brain edit entry`, `brain tag entry`, `brain list entries`.
**Never a bare `brain add`.** Ambiguity here is what makes a CLI unpleasant.

---

## 1. Schema

`brain.sqlite`, migration `002_brain_v2.sql`. Additive — nothing existing is dropped.

### 1.1 Core table

```sql
ALTER TABLE items ADD COLUMN ref          TEXT;     -- stable human handle: 'cv_intuicell_ai'
ALTER TABLE items ADD COLUMN parent_id    INTEGER REFERENCES items(id);
ALTER TABLE items ADD COLUMN rank         INTEGER;  -- preference among SIBLINGS. 1 = first choice.
ALTER TABLE items ADD COLUMN meta         TEXT;     -- JSON. open-ended, user-defined.
ALTER TABLE items ADD COLUMN body_format  TEXT NOT NULL DEFAULT 'markdown';  -- markdown|text|code|yaml|json
ALTER TABLE items ADD COLUMN sensitivity  TEXT NOT NULL DEFAULT 'normal';    -- normal|personal|secret
ALTER TABLE items ADD COLUMN vetted_at    TEXT;     -- when YOU approved it
ALTER TABLE items ADD COLUMN last_used_at TEXT;     -- last time you read/exported it
ALTER TABLE items ADD COLUMN use_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE items ADD COLUMN edit_count   INTEGER NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX ix_items_ref ON items(ref) WHERE ref IS NOT NULL;
CREATE INDEX ix_items_parent  ON items(parent_id);
CREATE INDEX ix_items_rank    ON items(parent_id, rank);
CREATE INDEX ix_items_sens    ON items(sensitivity);
CREATE INDEX ix_items_used    ON items(last_used_at);

-- filterable JSON fields, without fixing the vocabulary
ALTER TABLE items ADD COLUMN length   TEXT GENERATED ALWAYS AS (json_extract(meta,'$.length'))   VIRTUAL;
ALTER TABLE items ADD COLUMN lang     TEXT GENERATED ALWAYS AS (json_extract(meta,'$.lang'))     VIRTUAL;
ALTER TABLE items ADD COLUMN audience TEXT GENERATED ALWAYS AS (json_extract(meta,'$.audience')) VIRTUAL;
CREATE INDEX ix_items_length ON items(length);
CREATE INDEX ix_items_lang   ON items(lang);
```

**`rank` has a defined meaning and it must be documented in `--help`:**

> **`rank` is your preference order among items sharing the same parent. 1 = first choice.**
> It is not a quality score, not a priority, not out of 10. Siblings only.

Without that definition it becomes meaningless, which was a fair criticism of the earlier draft.

### 1.2 Kinds — user-editable, not hardcoded

```sql
CREATE TABLE kinds (
  name        TEXT PRIMARY KEY,
  description TEXT,
  sort_order  INTEGER NOT NULL DEFAULT 100,
  builtin     INTEGER NOT NULL DEFAULT 0
);

INSERT INTO kinds (name, description, sort_order, builtin) VALUES
  ('entity',       'A company, person, tool, place — something claims attach to', 10, 1),
  ('claim',        'A point you make about an entity',                            20, 1),
  ('variant',      'One phrasing of a claim',                                     30, 1),
  ('fact',         'Something established to be true',                            40, 1),
  ('method',       'A way of doing something, repeatable',                        50, 1),
  ('decision',     'A conclusion reached, with what was rejected',                60, 1),
  ('note',         'Free-form thinking',                                          70, 1),
  ('link',         'An external URL worth keeping',                               80, 1),
  ('snippet',      'Code or command worth reusing',                               90, 1),
  ('conversation', 'A promoted ChatVault conversation',                          100, 1),
  ('list',         'A container whose children are the entries',                 110, 1);
```

**One kind per entry, and here's why** (you asked): a single `kind` keeps every query predictable — `WHERE kind='method'` always means the same thing. Multi-kind would force every query to handle sets. **Tags carry the cross-classification instead**, unlimited and hierarchical. So a code snippet that's also a decision is `kind='snippet'` + tag `type/decision`.

**Kinds are yours to change:** `brain add kind`, `brain edit kind`, `brain remove kind` (refuses if in use, offers `--reassign-to`).

### 1.3 Aliases — your idea, and a good one

```sql
CREATE TABLE aliases (
  id      INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  alias   TEXT NOT NULL,
  note    TEXT,
  UNIQUE(item_id, alias)
);
CREATE INDEX ix_aliases_alias ON aliases(alias COLLATE NOCASE);
```

**This directly solves "where did I put that thing I call X but also sometimes Y."** One item, many names, all searchable. `alias` also feeds FTS, so `brain find "the npm thing"` works if you once recorded that alias.

### 1.4 Collections — ordered curated sets

```sql
CREATE TABLE collections (
  id         INTEGER PRIMARY KEY,
  ref        TEXT UNIQUE,
  name       TEXT NOT NULL,
  why        TEXT NOT NULL,
  meta       TEXT,
  created_at TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE collection_items (
  collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  position      INTEGER NOT NULL,
  note          TEXT,               -- why THIS item is in THIS collection
  PRIMARY KEY (collection_id, item_id)
);
```

**Three different mechanisms, three different jobs** — this distinction matters and should be in `--help`:

| Mechanism | Answers | Cardinality |
|---|---|---|
| `parent_id` | *"what does this belong to?"* | one parent |
| tags | *"what is this about?"* | many, unordered |
| **collections** | *"what set did I gather this into, and in what order?"* | many, **ordered** |

Collections are for *"CV — AI engineering profile"*, *"macOS toolbox"*, *"interview prep, in the order I want to revise them"*. A tag can't hold order; a parent can't hold membership in five sets.

### 1.5 Links — with a user-editable vocabulary

```sql
CREATE TABLE rel_types (
  name        TEXT PRIMARY KEY,
  description TEXT,
  symmetric   INTEGER NOT NULL DEFAULT 0,   -- if 1, A→B implies B→A
  inverse     TEXT                          -- e.g. 'supersedes' ⇄ 'superseded_by'
);

INSERT INTO rel_types (name, description, symmetric, inverse) VALUES
  ('extends',       'B adds to A',                       0, 'extended_by'),
  ('contradicts',   'B conflicts with A',                1, NULL),
  ('supersedes',    'B replaces A',                      0, 'superseded_by'),
  ('derived_from',  'A was extracted from B',            0, 'source_of'),
  ('see_also',      'related, no direction',             1, NULL),
  ('example_of',    'A is an instance of B',             0, 'has_example'),
  ('depends_on',    'A requires B',                      0, 'required_by'),
  ('variant_of',    'A is another phrasing of B',        0, 'has_variant');
```

Reuse the existing `item_links` table, adding `created_at` and a FK to `rel_types`.

**Why `link` and not `relate`:** the command reads `brain link entry cv_ai --to cv_ai_short --as variant_of`, which says exactly what happens. "Relate" leaves the relationship implicit.

### 1.6 History — the edit trail, including your inside/outside idea

```sql
CREATE TABLE history (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  changed_at TEXT NOT NULL,
  field      TEXT NOT NULL,
  old_value  TEXT,
  new_value  TEXT,
  origin     TEXT NOT NULL DEFAULT 'cli',   -- cli | editor | import | api | detected
  note       TEXT
);
CREATE INDEX ix_history_item ON history(item_id, changed_at);
```

A trigger increments `items.edit_count` and updates `updated_at` on every change.

**On inside vs outside edits.** Edits made through `brain` commands are `origin='cli'` or `'editor'`. **Edits made to a file outside the database can't be observed directly** — nothing tells SQLite a file changed. But when a body came from a file, store `body_sha256` and `source_path`; then `brain verify` re-hashes and records `origin='detected'` for anything that drifted. That's the honest version of your idea: inside edits are tracked exactly, outside edits are *detected on demand*, and the distinction is visible in the stats.

```bash
brain stats edits          # per item: cli / editor / import / detected, and totals
```

### 1.7 Reviews — resurfacing, aimed at your actual problem

```sql
CREATE TABLE reviews (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  review_at  TEXT NOT NULL,
  reason     TEXT,
  done_at    TEXT,
  outcome    TEXT           -- kept | edited | archived | superseded
);
CREATE INDEX ix_reviews_due ON reviews(review_at) WHERE done_at IS NULL;
```

**This targets "I forget the things because they're in nested folders."** `brain due` shows what you asked to be reminded of. Combined with `last_used_at`, `brain stale` shows what you saved and never looked at again — which is either dead weight to archive, or something you forgot you had. Both are useful to know.

### 1.8 Attachments

```sql
CREATE TABLE attachments (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  path       TEXT,
  sha256     TEXT,
  filename   TEXT,
  mime       TEXT,
  bytes      INTEGER,
  content    TEXT,          -- extracted text, if any
  added_at   TEXT NOT NULL
);
```

Same principle as ChatVault: text extracted into the DB and searchable, bytes left on disk.

### 1.9 Search

```sql
DROP TABLE IF EXISTS items_fts;
CREATE VIRTUAL TABLE items_fts USING fts5(
  title, why, body, ref, aliases, tags,
  content='', tokenize='porter unicode61'
);
```

External-content FTS, rebuilt by trigger, **including aliases and tag names** — so a search hits whether you remember the title, your own nickname for it, or how you tagged it.

### 1.10 Views

```sql
CREATE VIEW v_items AS SELECT * FROM items WHERE status = 'active';

CREATE VIEW v_browse AS
SELECT i.id, i.ref, i.kind, i.title,
       substr(i.why,1,60) AS why,
       (SELECT title FROM items p WHERE p.id = i.parent_id) AS parent,
       i.rank, i.length, i.confidence, i.sensitivity,
       (SELECT group_concat(t.name,', ') FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id=i.id) AS tags,
       (SELECT group_concat(a.alias,', ') FROM aliases a WHERE a.item_id=i.id) AS aliases,
       date(i.created_at) AS created, date(i.vetted_at) AS vetted,
       i.use_count, i.edit_count
FROM items i WHERE i.status='active'
ORDER BY i.kind, i.parent_id, i.rank;

CREATE VIEW v_orphans AS       -- no parent, no tag, no collection: probably lost
SELECT * FROM v_items i WHERE i.parent_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM item_tags WHERE item_id=i.id)
  AND NOT EXISTS (SELECT 1 FROM collection_items WHERE item_id=i.id);

CREATE VIEW v_stale AS         -- saved, never used
SELECT * FROM v_items WHERE use_count = 0 AND created_at < datetime('now','-90 days');
```

---

## 2. On separate tables per domain — the answer to your question

**No separate CV table. No separate snippets table.** One `items` table, typed by `kind`, shaped by `parent_id`, described by `meta`.

**Why**, since you asked directly:

| With one table | With per-domain tables |
|---|---|
| One search covers everything | Search each table separately, merge results |
| One `list`, one `show`, one `tag` | N sets of near-identical commands |
| A CV variant can link to a method | Cross-table links need special handling |
| New domain = new `kind` row, zero code | New domain = migration + commands + tests |

**The one thing per-domain tables give you is domain-specific *columns*** — and `meta` JSON provides that without the cost. A CV variant carries `{"length":"short","audience":["ats"]}`; a code snippet carries `{"lang":"bash","shell":"zsh"}`. Same table, different shape, one search.

**This is also the answer to your folder-structure problem.** You've never settled on one hierarchy because *no single hierarchy is correct* — that's the tree-versus-graph limit again. Here you get containment (`parent_id`), classification (tags), curated sets (collections), and named relationships (links) **simultaneously**, so you never have to choose the one true structure.

---

## 3. Commands — `brain <verb> <noun>`

### Entries

```bash
brain add entry "<title>" --why "<why>" [options]
    --kind K --ref cv_ai --parent <ref|id> --rank 1
    --body "text" | --body-file F | --body-editor
    --tag a --tag b --alias "the npm thing"
    --meta '{"length":"short"}'  |  --set length=short --set lang=bash
    --collection interview_prep
    --confidence verified --sensitivity personal
    --url https://…  --vetted            # --vetted sets vetted_at = now

brain edit entry <ref|n> [--title|--why|--body-editor|--rank|--set k=v|--kind]
brain show entry <ref|n> [--with children|links|history|attachments]
brain list entries [--kind|--tag|--parent|--collection|--length|--lang|--confidence|--sensitivity|--rank]
brain find "<query>" [--kind K] [--tag T]        # FTS over title, why, body, ref, aliases, tags
brain tree <ref>                                  # entry + descendants, ranked
brain pick <ref> [--length short] [--audience ats]   # ONE result: best match by rank
brain variants <ref>                              # all children side by side, with why + rank
brain archive entry <ref>  ·  brain restore entry <ref>
brain remove entry <ref> --confirm                # the only hard delete; backs up first
```

**`brain pick` is the command that answers "give me the shortest version"** — one line, one sentence out, no reading.

### Tags · aliases · collections · links · kinds

```bash
brain add tag <name> [--description]        ·  brain tag entry <ref> <tag>...
brain untag entry <ref> <tag>               ·  brain list tags   ·  brain tree tags
brain rename tag <old> <new>                ·  brain merge tags <a> <b>

brain add alias <ref> "<alias>"             ·  brain remove alias <ref> "<alias>"
brain list aliases

brain add collection <name> --why "…" [--ref X]
brain add to collection <coll> <ref>... [--position N] [--note "…"]
brain remove from collection <coll> <ref>
brain list collections  ·  brain show collection <ref>
brain reorder collection <ref>              # interactive

brain link entry <a> --to <b> --as extends [--note "…"]
brain unlink entry <a> --to <b> --as extends
brain list links [--as contradicts]
brain add rel <name> --description "…" [--symmetric] [--inverse X]

brain add kind <name> --description "…"     ·  brain list kinds
brain remove kind <name> [--reassign-to K]
```

### Review · stats · maintenance

```bash
brain review entry <ref> --in 3months --reason "check if still true"
brain due                        # reviews now due
brain done <ref> --outcome kept
brain stale                      # saved, never used
brain orphans                    # no parent, no tag, no collection

brain stats                      # counts by kind, tag, collection, confidence
brain stats edits                # cli / editor / import / detected, per item and total
brain stats usage                # most and least used
brain verify                     # dangling refs, unresolvable source_uid, drifted file bodies
brain backup  ·  brain export --format json|markdown|yaml [--collection X]
```

### Import

```bash
brain import markdown <dir> --kind note --tag imported/2026-08
brain import yaml <file> --map <mapping.yaml>
brain import csv <file> --map <mapping.yaml>
brain promote --uid <chatvault-uid> --why "…"     # existing, unchanged
```

**`brain import markdown` is how the hoard gets in.** Front-matter becomes `meta`, the H1 becomes `title`, the folder path becomes tags. That single command is what makes "shitload of useful stuff in nested folders" tractable.

---

## 4. Interfaces

- **Datasette** on `brain.sqlite`, port 8002, `v_browse` as default table, facets on kind/tag/collection/confidence/length. **~15 min, do it first** — it's the browsing surface the user actually prefers.
- **MCP** — register read-only. Then any AI can answer *"what do I already know about X?"* This is arguably the highest-value interface of all.
- **`brain.command`** launcher, mirroring `chatvault.command`.
- **Textual screen** — last, only if the CLI proves insufficient.

---

## 5. Neo4j

**Supplement, not alternative.** SQLite stays the source; Neo4j is a disposable projection, exactly as with ChatVault.

Its value here is `links` — once there are enough edges, *"what connects A to B through paths I didn't specify"* becomes answerable. `brain graph push` when wanted, ignore otherwise. **Nothing in this spec depends on it.**

---

## 6. Build phases

| # | Phase | Effort | Notes | Status |
|---|---|---|---|---|
| 1 | Migration `002_brain_v2.sql` — all tables, columns, views, FTS | ~3h | Do it in one go; partial schemas cause churn | **Done 2026-08-29** — applied to real `brain.sqlite`, backed up + dry-run tested first |
| 2 | Entry commands: add/edit/show/list/find/tree/pick/variants/archive | ~5h | The daily surface | **Done 2026-08-29**, incl. restore/remove |
| 3 | Tags, aliases, collections, links, kinds | ~4h | Organisation | **Done 2026-08-29** |
| 4 | Review, stats, verify, backup, export | ~3h | Maintenance | **Done 2026-08-29** |
| 5 | Import (markdown/yaml/csv) | ~3h | Feeding the hoard in | **Done 2026-08-29** |
| 6 | Datasette + MCP + launcher | ~2h | | **Datasette + launcher done 2026-08-29; MCP registration command written up, not yet run (needs user go-ahead — mutates `~/.claude.json`)** |

**Phases 1–2 are the minimum usable system. Phases 3–5 are what make it a knowledge base rather than a list.**

---

## 7. Acceptance criteria

| Test | Passes when | Verified |
|---|---|---|
| Command grammar | Every command is `brain <verb> <noun>`; no bare `brain add` exists | ✅ `chatvault brain --help` and every noun sub-Typer checked directly |
| `rank` documented | `--help` states "preference among siblings, 1 = first choice" | ✅ `brain add entry --help` / `brain edit entry --help` |
| Ref resolution | Every command taking `<ref>` also accepts a row number and an alias | ✅ `test_ref_resolution_by_ref_alias_and_row_number` + live CLI round trip |
| Alias search | `brain find` locates an item by an alias never in its title or body | ✅ `test_find_by_alias_never_in_title_or_body` + live `brain find "the ai bullet"` |
| Pick | `brain pick <ref> --length short` returns exactly one entry | ✅ `test_pick_returns_exactly_one_by_length` + live CLI |
| Collections hold order | Reordering a collection changes `show collection` output, and nothing else | ✅ `test_reordering_collection_changes_only_its_own_output` |
| Kinds are editable | `brain add kind` then using it in `add entry` works with no code change | ✅ `test_kinds_are_editable_with_zero_code_change` + live `add kind recipe` |
| Edit trail | Three edits produce three `history` rows and `edit_count = 3` | ✅ `test_edit_entry_writes_history_and_edit_count` + live CLI (3 edits → `edit_count=3`) |
| Drift detection | Change a file body outside the DB → `brain verify` reports `origin='detected'` | ✅ `test_verify_detects_body_drift_outside_the_db` |
| Sensitivity | `brain export` excludes `sensitivity != 'normal'` unless `--include-sensitive` | ✅ `test_export_excludes_non_normal_sensitivity_by_default` + live CLI |
| One hard delete | Only `remove entry --confirm` deletes; everything else archives | ✅ `test_remove_entry_is_the_only_hard_delete` (also added: refuses to delete a parent with children) |
| Vault untouched | `chatvault stats` identical before and after every brain operation | ✅ diffed byte-for-byte after the migration and after every phase of live CLI testing |

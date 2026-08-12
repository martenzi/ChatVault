# ChatVault — Spec v1.2: Presence Tracking & Memory Files

**Extends:** v1.0 architecture, v1.1 addendum, v1.1.1 patch
**Adds:** file-presence reconciliation (all sources) + memory files as a first-class entity

Two independent features. **Part A is small, useful immediately, and applies to everything. Part B depends on A.** Build A first; B is optional.

---

# Part A — Presence tracking & `reconcile`

## A.1 The problem

`ingest` is a **push**: disk → DB. It can only observe what exists. A deleted file is not an event it can see. Detecting disappearance requires a **diff**: compare the set of files the DB believes in against what's actually on disk.

This is a general capability, not a memory-file concern. It answers "is the original still there?" for every source in the vault.

## A.2 Migration 003

```sql
ALTER TABLE source_files ADD COLUMN first_seen_at  TEXT;
ALTER TABLE source_files ADD COLUMN presence       TEXT NOT NULL DEFAULT 'present';
       -- present | missing | retired
ALTER TABLE source_files ADD COLUMN missing_since  TEXT;
ALTER TABLE source_files ADD COLUMN presence_note  TEXT;

CREATE INDEX ix_srcfiles_presence ON source_files(presence);

CREATE TABLE reconcile_runs (
  id            INTEGER PRIMARY KEY,
  run_at        TEXT NOT NULL,
  roots_json    TEXT NOT NULL,
  checked       INTEGER NOT NULL,
  still_present INTEGER NOT NULL,
  newly_missing INTEGER NOT NULL,
  reappeared    INTEGER NOT NULL,
  report_json   TEXT NOT NULL
);
```

**Presence states:**

| State | Meaning |
|---|---|
| `present` | Found on disk at last reconcile |
| `missing` | Tracked, expected, **not found** — needs attention |
| `retired` | Deliberately gone (archived to external drive, source decommissioned). Expected absence. Never reported as a problem. |

## A.3 `chatvault reconcile`

```
chatvault reconcile [--root PATH ...] [--kind live|frozen|all] [--dry-run] [--json]
chatvault reconcile --retire <root-or-path> --reason "moved to /mnt/archive"
chatvault reconcile --status
```

**Default scope is `--kind live` and this matters.** Frozen sources get archived to an external drive on purpose — their files *should* vanish. Flagging them as missing would produce permanent false alarms and train you to ignore the report. This is the payoff from the live/frozen distinction added in v1.1.

Algorithm:
1. For each tracked `root_dir` in scope, walk the filesystem.
2. Match by `sha256` first, then by `path` (catches "same file, moved" vs "content changed in place").
3. Found → `presence='present'`, bump `last_seen_at`.
4. Not found → `presence='missing'`, set `missing_since` (only on first detection; don't overwrite).
5. Previously `missing`, now found → back to `present`, clear `missing_since`, count as `reappeared`.
6. **Never delete a row. Never delete ingested content.** Presence is metadata about the source, not about the data.
7. Persist the run to `reconcile_runs`.

`--retire` is the explicit "yes, I moved this on purpose" acknowledgement. Retired sources are skipped by future reconciles and shown separately in the manifest.

## A.4 Wiring

- **`ingest` on a live root automatically runs `reconcile` for that root afterwards.** This is what makes it self-maintaining rather than something you must remember — your hunch that "a new ingest might resolve this on its own" becomes true *by design*.
- `audit` gains a check: any `presence='missing'` → WARN, listed with `missing_since`.
- `sources list` and `export-manifest` show presence state.
- `sources export-manifest` gains a **"Missing — tracked but not found on disk"** section.

---

# Part B — Memory files as a first-class entity

## B.1 Why not just ingest them as conversations

Three reasons, each sufficient on its own:

1. **No turn structure.** No roles, no messages, no ordering. Forcing them into `conversations`/`messages`/`blocks` means fabricating structure that isn't there.
2. **Different lifecycle.** Conversations are immutable facts. Memory files are mutable belief-state — they get edited, pruned, clobbered. Same path, different content, over and over.
3. **Stats pollution.** "4,812 conversations" must mean conversations. If memory files are in that count, every metric you build is subtly wrong forever.

They get their own table, but reuse the polymorphic tagging, annotation and FTS machinery — so they cost little and gain everything.

## B.2 Migration 004

```sql
CREATE TABLE memory_snapshots (
  id             INTEGER PRIMARY KEY,
  platform       TEXT NOT NULL,          -- openclaw | hermes | claude-code | ...
  scope          TEXT,                   -- 'global' | project name | agent name
  path           TEXT NOT NULL,          -- absolute source path
  format         TEXT NOT NULL,          -- markdown | json | jsonl | text
  text           TEXT NOT NULL DEFAULT '',
  raw_json       TEXT,
  content_hash   TEXT NOT NULL,
  char_count     INTEGER NOT NULL DEFAULT 0,

  captured_at    TEXT NOT NULL,          -- when ChatVault saw this version
  file_mtime     TEXT,                   -- filesystem mtime, if available

  status         TEXT NOT NULL DEFAULT 'current',
       -- current | superseded | orphaned
  superseded_by  INTEGER REFERENCES memory_snapshots(id),
  orphaned_at    TEXT,

  sensitivity    TEXT NOT NULL DEFAULT 'normal',   -- normal | sensitive
  source_file_id INTEGER REFERENCES source_files(id),
  excluded       INTEGER NOT NULL DEFAULT 0,

  UNIQUE(path, content_hash)
);

CREATE INDEX ix_mem_status   ON memory_snapshots(status);
CREATE INDEX ix_mem_platform ON memory_snapshots(platform);
CREATE INDEX ix_mem_path     ON memory_snapshots(path);
CREATE INDEX ix_mem_captured ON memory_snapshots(captured_at);

CREATE VIRTUAL TABLE memory_fts USING fts5(
  text, content='memory_snapshots', content_rowid='id',
  tokenize='porter unicode61'
);
-- triggers mirroring blocks_fts

CREATE VIEW v_memory AS
  SELECT * FROM memory_snapshots
  WHERE status = 'current' AND excluded = 0;
```

`tag_links.target_type` and `annotations.target_type` accept `'memory_snapshot'`. No schema change needed — already polymorphic.

## B.3 Ingest semantics — versioning for free

`chatvault memory ingest <path-or-dir> --platform X [--scope Y] [--sensitive]`

For each memory file:

```
hash = sha256(normalised content)

if a snapshot exists for (path, hash):
    → no-op. bump source_files.last_seen_at. Nothing changed.

elif a 'current' snapshot exists for this path with a different hash:
    → the file changed.
      insert new snapshot as 'current'
      old one: status='superseded', superseded_by=<new id>

else:
    → first time seeing this path. insert as 'current'.
```

**The side effect is the best part: you get complete version history of every memory file, automatically.** No tool gives you that. You can diff what OpenClaw believed last month against what it believes now, and see exactly when a fact appeared or vanished.

## B.4 Deletion handling — the thing you actually asked for

Extend `reconcile` (Part A) to memory:

- Memory file gone from disk → its `current` snapshot becomes **`status='orphaned'`**, `orphaned_at` set. Content is retained in full.
- File reappears with the same hash → back to `current`.
- File reappears with different content → new snapshot; the orphaned one stays as history.

**Your ghost-data worry is solved by view semantics, not by discipline.** `v_memory` returns only `current`. So:

- `chatvault memory list` → what your tools actually believe *right now*
- `chatvault search "..."` → searches current memory only, by default
- Nothing you deleted upstream ever haunts a default query
- But `--include-orphaned` or `chatvault memory history <path>` shows the full record

This is exactly the pattern already used for `excluded` and `superseded_by`. Same philosophy, applied consistently: **you never have to remember to clean up, because stale things are hidden by default and destroyed never.**

## B.5 Commands

```
chatvault memory ingest <path|dir> --platform X [--scope Y] [--sensitive]
chatvault memory list [--platform X] [--include-orphaned]
chatvault memory show <id|path>
chatvault memory history <path>            # every version, with captured_at
chatvault memory diff <path> [--from <id>] [--to <id>]   # unified diff between versions
chatvault memory orphans                   # what vanished upstream, and when
chatvault memory purge --path <path> --confirm    # the ONLY hard delete; backs up first
```

`memory diff` is the highest-value command in this section. "What changed in my OpenClaw memory between June and now" is a question you currently cannot ask of any tool.

## B.6 Privacy

Memory files frequently contain personal facts — that's their purpose. Given your stated concerns about what leaves the machine:

- `--sensitive` sets `sensitivity='sensitive'`
- **Sensitive snapshots are excluded from `graph push` by default** (Neo4j is the layer most likely to get pointed at an external tool). Override requires an explicit `--include-sensitive`.
- `curation export` includes tags/annotations on memory but **never snapshot text**.
- `audit` reports a count of sensitive snapshots so it stays visible.

## B.7 What NOT to build yet

Tempting and premature — write it down, don't build it:

- Cross-tool memory reconciliation ("what does Hermes know that OpenClaw doesn't")
- Extracting atomic facts from memory files into `Concept` nodes
- Feeding ChatVault memory *back into* tools as a unified memory source

All three are Phase 7 territory and depend on concept extraction that doesn't exist yet. **Ingest, version, and search first. Live with it. Let real friction pick what comes next.**

---

# Acceptance criteria

| Test | Passes when |
|---|---|
| Presence: missing | Ingest a live root, delete a file, `reconcile` → that source_file is `missing` with `missing_since` set; **conversation rows are untouched and still queryable**. |
| Presence: reappear | Restore the file, `reconcile` → `present`, `missing_since` cleared, counted as `reappeared`. |
| Presence: retire | `reconcile --retire <root>` then delete files → reported as retired, **not** as missing. |
| Presence: frozen scope | Default `reconcile` does not flag archived frozen sources. |
| Memory: versioning | Ingest memory file, edit it, re-ingest → two snapshots, old is `superseded`, new is `current`, `v_memory` returns one row. |
| Memory: no-op | Re-ingest unchanged file → zero new rows. |
| Memory: orphaning | Delete the file, `reconcile` → snapshot is `orphaned`, absent from `v_memory` and from default search, still present in `memory history`. |
| Memory: stats isolation | `chatvault stats` conversation counts are **unchanged** by memory ingestion. |
| Memory: privacy | `graph push` with a `sensitive` snapshot present → snapshot is not in Neo4j unless `--include-sensitive`. |

# Build order

1. Migration 003 + `reconcile` + audit/manifest wiring **(do this regardless — it's small and it applies to every source)**
2. Migration 004 + `memory ingest` / `list` / `history`
3. Reconcile→memory orphaning
4. `memory diff`
5. Privacy flags

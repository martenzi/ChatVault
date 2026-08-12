# ChatVault — Spec Addendum v1.1

**Extends:** CHATVAULT_ARCHITECTURE.md v1.0
**For:** implementing model (Sonnet-class)
**Adds:** `audit`, `backup`, `curation export/import`, `sources` — plus migration 002

Same non-negotiables as v1.0 apply. In particular: **nothing writes back to `raw/`**, and **nothing is DELETEd**.

---

## Migration 002

```sql
-- source classification: exports are photographs, session dirs are rivers
ALTER TABLE source_files ADD COLUMN kind TEXT NOT NULL DEFAULT 'frozen';  -- frozen | live
ALTER TABLE source_files ADD COLUMN last_seen_at TEXT;
ALTER TABLE source_files ADD COLUMN root_dir TEXT;   -- the ingest root this file came from

-- audit history, so you can see drift over time
CREATE TABLE audit_runs (
  id          INTEGER PRIMARY KEY,
  run_at      TEXT NOT NULL,
  verdict     TEXT NOT NULL,        -- pass | warn | fail
  report_json TEXT NOT NULL
);

CREATE INDEX ix_srcfiles_kind ON source_files(kind);
CREATE INDEX ix_srcfiles_root ON source_files(root_dir);
```

---

## 1. `chatvault audit`

The question this answers: **"Did everything in the files end up in the database, in the right place?"**

Distinct from `verify` (which only checks internal DB consistency). Audit compares DB state against *disk* and against *expectations*.

```
chatvault audit [--raw-dir PATH] [--platform X] [--json] [--markdown FILE] [--sample] [--strict]
```

Exit code: `0` pass, `1` warn, `2` fail. Must be scriptable.

### Check groups

**A. Ingest completeness**
| Check | Fail condition |
|---|---|
| `source_files.status` all `ok` | any `failed` |
| Unclaimed files: walk `--raw-dir`, hash each, compare to `source_files.sha256` | any file on disk not in DB |
| Ignore list | skip `raw_exclusions/`, `.DS_Store`, `*.zip`, `_manifest.jsonl` |

The unclaimed-files check is the most important one in this command. It catches files that `inventory` saw but `ingest` silently walked past — the classic silent-loss failure mode.

**B. Structural integrity**
- Orphan `messages` (no parent conversation) → FAIL if > 0
- Orphan `blocks` → FAIL if > 0
- Conversations with `message_count = 0` → FAIL if > 0
- Messages with zero blocks → WARN
- `messages.idx` must be contiguous `0..n-1` per conversation → FAIL on gaps
- `COUNT(blocks_fts) == COUNT(blocks)` → FAIL on mismatch (means triggers drifted)

**C. Metadata coverage** — per platform, report percentages:
- assistant messages with NULL `model` → WARN > 5%, FAIL > 20%
- messages with NULL `created_at` → WARN > 5%
- conversations with NULL `title` → WARN > 10%
- conversations with NULL `started_at` → WARN > 5%

*This is the check that catches the Windsurf-style failure: content arrived, metadata didn't.*

**D. Content coverage** — per platform:
- `blocks.type = 'unknown'` as % of all blocks → WARN > 1%, FAIL > 5%
- blocks with `type='text'` and `text = ''` → WARN if > 1%
- `char_count` distribution: min / p25 / median / p95 / max per platform
- Conversations in the bottom 1% by `char_count` → list them (likely truncation)

**E. Dedupe health**
- count `superseded_by IS NOT NULL`
- count flagged-for-review (from dedupe L3 ambiguous bucket)
- top 20 `blocks.content_hash` clusters by frequency, with a text preview — this is the boilerplate-detection surface

**F. Sample manifest** (`--sample`)

Emit a **stratified edge-case sample**, 5 per platform, plus ready-to-paste export commands. Selection rules — deliberately *not* random:

1. Longest conversation by `char_count`
2. Highest `tool_use` block count
3. Most `unknown` blocks (0 if none exist — pick highest anyway)
4. Any conversation with an `image` or `attachment` block; else highest `block_count`
5. One genuine random pick as control

Rationale to preserve in a code comment: *bugs live at the edges, so sampling the edges is worth ~100× sampling the middle.*

### Output

Rich terminal table, grouped by check group, colour-coded PASS/WARN/FAIL, with a one-line verdict banner at the end and an explicit **"Safe to archive sources: YES / NO"** line. Persist the run to `audit_runs`. `--markdown FILE` writes the same report for the record.

---

## 2. `chatvault backup`

```
chatvault backup [--out DIR] [--label TEXT]
chatvault backup --list
chatvault backup --restore <file> [--to PATH] [--force]
chatvault backup --prune --keep N
```

**Implementation requirements:**
- Use the SQLite **Online Backup API** (`sqlite3.Connection.backup()`), **not** a file copy. The DB is in WAL mode; `cp` can capture a torn state.
- Compress with `zstd` (via `subprocess` on the CLI tool, or fall back to `gzip` if unavailable).
- Filename: `chatvault-YYYYMMDD-HHMM[-label].sqlite.zst`
- Default `--out` is `$CHATVAULT_ROOT/db/backups/`
- `--restore` refuses to overwrite an existing DB without `--force`, and always backs up the current DB first.
- `--list` shows filename, size, timestamp, and the conversation/tag counts recorded in a sidecar `.json` written alongside each backup.

**Automatic backup hooks** — call `backup(label=...)` before:
- `migrate`
- `dedupe --apply`
- `exclude` / `restore` affecting more than 100 targets
- `curation import`

---

## 3. `chatvault curation export` / `import`

This is the insurance policy. Content is re-derivable from `raw/`; **judgement is not.**

```
chatvault curation export [--out FILE]
chatvault curation import <FILE> [--dry-run] [--on-conflict skip|overwrite]
chatvault curation diff <FILE>
```

### Format

Human-readable, git-friendly JSON. **Targets are addressed by stable natural keys — never by rowid** — so the file survives a complete wipe-and-re-ingest.

```json
{
  "schema": 1,
  "exported_at": "2026-07-31T09:00:00Z",
  "counts": { "tags": 24, "tag_links": 480, "annotations": 61, "exclusions": 3120, "saved_views": 12 },
  "tags": [
    { "name": "project/second-brain", "colour": "#4c8", "created_at": "..." }
  ],
  "tag_links": [
    { "tag": "project/second-brain",
      "target": { "kind": "conversation", "conv_uid": "chatgpt:6f2a..." },
      "source": "manual", "created_at": "..." }
  ],
  "annotations": [
    { "target": { "kind": "block", "conv_uid": "claude-code:abc",
                  "msg_idx": 3, "block_idx": 0, "content_hash": "sha256:..." },
      "note": "the actual answer is here", "created_at": "..." }
  ],
  "exclusions": [
    { "target": { "kind": "block", "content_hash": "sha256:..." },
      "reason": "repeated system preamble", "rule": "content_hash = ?", "created_at": "..." }
  ],
  "saved_views": [
    { "name": "neo4j-threads", "description": "...", "sql": "SELECT ..." }
  ]
}
```

**Target address kinds:**
- `conversation` → `{conv_uid}`
- `message` → `{conv_uid, msg_idx, content_hash}`
- `block` → `{conv_uid, msg_idx, block_idx, content_hash}`
- `block_class` → `{content_hash}` only — applies to *every* block with that hash, wherever it appears. This is how bulk boilerplate exclusions survive re-ingest.

**Import resolution rules:**
1. Resolve by natural key.
2. If found but `content_hash` mismatches → record in `unresolved` with reason `content_drift`, do not apply.
3. If not found → record in `unresolved` with reason `missing_target`.
4. Never fail the whole import on unresolved items. Print a summary table and write `<FILE>.unresolved.json`.

`--dry-run` prints exactly what would be applied and what wouldn't, changing nothing.

---

## 4. `chatvault sources`

```
chatvault sources list [--kind live|frozen]
chatvault sources mark <path-or-root> --kind live|frozen
chatvault sources unclaimed <dir>
chatvault sources export-manifest [--out FILE]
```

**`unclaimed`** — walk a directory, hash every file, list those absent from `source_files`. Honours the same ignore list as `audit`. This is the standalone version of audit check A.

**`export-manifest`** — generate `MANIFEST.md` from the DB. Never hand-maintained; regenerate whenever you like.

```markdown
# ChatVault Source Manifest
Generated 2026-07-31 · 6 sources · 4,812 conversations · 218,440 messages

## Frozen (ingest once, archive)
| Source | Files | Conversations | Date range | SHA (short) | Ingested |
|---|---|---|---|---|---|
| chatgpt/2026-07-30 | 1 | 2,104 | 2023-04-01 → 2026-07-29 | a3f1c9 | 2026-07-30 |
| claude-web/2026-07-30 | 1 | 883 | 2024-02-11 → 2026-07-28 | 77bd02 | 2026-07-30 |

## Live (re-ingest periodically)
| Source | Files | Conversations | Last ingest | Days since |
|---|---|---|---|---|
| claude-code (~/.claude/projects) | 412 | 1,205 | 2026-07-31 | 0 |
| openclaw | 88 | 340 | 2026-07-31 | 0 |

## Rejected — seen and deliberately not ingested
| Path | Reason |
|---|---|
| raw_exclusions/index-dumps/ | index files, not conversations |
| raw_exclusions/*.log | tool logs, no chat content |
```

That last table matters: it records **negative knowledge**, so future-you doesn't re-investigate the same junk. Populate it from a `raw_exclusions/_why.md` file — a two-column markdown table the user maintains by hand. It is the *only* hand-maintained file in the system, and it exists purely so nothing gets re-litigated.

---

## 5. Acceptance criteria

| Command | Done when |
|---|---|
| `audit` | Deliberately corrupt a test vault (delete a block row, add an unclaimed file, null out a model column) → each is caught with the correct severity and exit code. `--sample` emits 5 pasteable export commands per platform. |
| `backup` | Backup during an open write transaction produces a valid, openable DB. `--restore` round-trips to byte-identical row counts. |
| `curation` | Export → wipe DB → full re-ingest → import → all tags, annotations, exclusions and views restored, with an unresolved count of 0 on unchanged data. **This is the single most important test in the addendum.** |
| `sources` | `unclaimed` correctly reports a file dropped into `raw/` post-ingest, and correctly ignores `raw_exclusions/`. |

## 6. Suggested build order

1. Migration 002 + `sources` (small, unblocks audit check A)
2. `backup` (small, and you want it before anything else touches data)
3. `audit` (the payoff)
4. `curation export/import` (do the wipe-and-restore test for real)

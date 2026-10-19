# ChatVault — Spec Patch v1.1.1

**Patches:** CHATVAULT_SPEC_ADDENDUM.md v1.1
**Reason:** three underspecified behaviours raised during implementation. All three are confirmed spec defects; this file is authoritative where it conflicts with v1.0 or v1.1.

---

## Patch 1 — `dedupe --apply` semantics (resolves the backup-hook contradiction)

**Verdict: `--apply` is a real bulk write. Keep the backup hook. Level 4 stays report-only.**

v1.0 said "`--apply` acts on levels 1–3." That was sloppy, because L1/L2/L3 all resolve *during ingest*. So the question "what is left for `--apply` to do?" was never answered. Here is the answer.

Ingest resolves the **unambiguous** cases inline. What it cannot resolve, it **defers** — it does not guess. `dedupe --apply` is the command that clears that deferred queue.

### Deferred queue

Add to migration 002:

```sql
CREATE TABLE dedupe_candidates (
  id            INTEGER PRIMARY KEY,
  conv_id_a     INTEGER NOT NULL REFERENCES conversations(id),
  conv_id_b     INTEGER NOT NULL REFERENCES conversations(id),
  reason        TEXT NOT NULL,      -- head_match_unverified | cross_source | near_identical
  confidence    REAL NOT NULL,      -- 0.0–1.0
  detail_json   TEXT,
  status        TEXT NOT NULL DEFAULT 'open',  -- open | applied | dismissed
  resolved_at   TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(conv_id_a, conv_id_b)
);
```

Ingest inserts here whenever `head_hash` matches but the strict-superset test does not conclusively pass, or when two conversations from *different* `native_id`s (or different platforms) share a `head_hash`.

### What `--apply` does

Acts **only** on candidates where **strict prefix containment is verified**:

> Conversation A is superseded by B if the ordered list of `messages.content_hash` for A is an exact prefix of that list for B, and `len(B) > len(A)`.

That test is deterministic and cheap. When it passes → set `A.superseded_by = B.id`, mark candidate `applied`. When it fails → leave `open`, print in report.

**`--apply` never touches `blocks`. It never sets `excluded`. It only ever writes `conversations.superseded_by`.**

### Command surface

```
chatvault dedupe --report                          # default; writes nothing
chatvault dedupe --apply                           # resolve all verified-containment candidates
chatvault dedupe --apply --candidate <id>          # resolve one
chatvault dedupe --supersede <uid-a> --by <uid-b>  # manual override, any pair, logged
chatvault dedupe --dismiss <id> --reason "..."     # mark not-a-duplicate, stops re-flagging
```

- Backup hook on `--apply`: **yes**, retained.
- Every write logs to `exclusion_log` with `action='supersede'` so it is reversible via `chatvault dedupe --undo <id>`.

### Level 4 (repeated block clusters) — unchanged, report-only

`--report` prints the top 20 `content_hash` clusters with previews and counts. It **never** auto-excludes at any threshold. Rationale: a block repeated 400 times might be a boilerplate preamble (noise) or a code snippet the user pasted into every session (signal). No threshold distinguishes those. The report ends with a ready-to-paste command:

```
chatvault exclude --target block --where "content_hash='sha256:...'" --reason "..."
```

That is the whole interaction. Sonnet's reading was correct.

---

## Patch 2 — `raw_exclusions/` location

**Verdict: sibling of `raw/`. Your reasoning is correct and is now the spec.**

```
$CHATVAULT_ROOT/
├── raw/                 # ingestable content ONLY
├── raw_exclusions/      # seen, judged, rejected
│   └── _why.md          # the only hand-maintained file in the system
├── db/
└── exports/
```

Required behaviours:

1. `inventory` and `ingest` default `--raw-dir` to `$CHATVAULT_ROOT/raw`. Because `raw_exclusions/` is a sibling, it is never walked. Good.
2. **Defensive guard:** if `--raw-dir` resolves to `$CHATVAULT_ROOT` itself (or any ancestor of `raw_exclusions/`), skip `raw_exclusions/` explicitly and print a one-line notice. Keep the path in the ignore list for exactly this case.
3. `inventory`/`ingest` pointed *directly* at `raw_exclusions/` → refuse with a clear error, overridable with `--force`. Someone will eventually do this by accident; make it loud.
4. `sources export-manifest` reads `raw_exclusions/_why.md`, parses the two-column markdown table, and renders it as the **"Rejected — seen and deliberately not ingested"** section. If the file is missing, emit a stub with a header row and a comment. If a subdirectory exists in `raw_exclusions/` with no matching row in `_why.md`, add a `⚠ undocumented` line to the manifest — gentle nag, not an error.
5. `audit` does **not** treat files in `raw_exclusions/` as unclaimed.

---

## Patch 3 — `curation import` conflict semantics for annotations

**Verdict: annotations are always additive-with-dedupe. `--on-conflict` does not apply to them.**

The deciding argument is Law 2: *nothing is deleted, only flagged.* "Delete all existing annotations on that target and replace with the imported set" would silently destroy notes written since the export — the exact class of loss this whole subsystem exists to prevent. It is never the right default, and it is too dangerous to be a flag value that a user might select for unrelated reasons.

So annotations get a **natural key** like every other entity, and then they stop being a special case:

```
annotation_key = (target_type, target_id, sha256(normalize(note)))
normalize(note) = note.strip(), internal whitespace collapsed to single spaces
```

Import behaviour:
- key exists → skip, count as `unchanged`
- key absent → insert, count as `added`
- **never update, never delete**

This is idempotent: importing the same file twice changes nothing. Two genuinely different notes on the same target both survive, which is the intended schema behaviour.

### `--on-conflict` applies to these, with these keys

| Entity | Natural key | `skip` | `overwrite` |
|---|---|---|---|
| `tags` | `name` | leave existing | update `colour` |
| `tag_links` | `(tag, target)` | leave | update `source` |
| `saved_views` | `name` | leave | replace `sql` + `description` |
| `exclusions` | `(target, rule)` | leave | update `reason` |
| `annotations` | `(target, note_hash)` | — | **n/a, always additive** |

Print this in the import summary so the asymmetry is visible rather than surprising:

```
annotations   61 imported →  58 added, 3 unchanged  (additive by design; never overwritten)
```

### Escape hatch

If a genuine replace is ever needed, it is a **separate, explicit, destructive command** — not a flag value:

```
chatvault curation replace-annotations <FILE> --target <uid> --confirm
```

Takes a backup first, logs every removal to `exclusion_log`, and refuses to run without `--confirm`. Build this **last, or not at all** — it exists to be documented, not used.

---

## Patch 4 — acceptance criteria additions

| Test | Passes when |
|---|---|
| Dedupe deferral | Ingest a conversation, then a variant with the same `head_hash` but a *divergent* message 3 → lands in `dedupe_candidates` as `open`, and `--apply` leaves it `open`. |
| Dedupe apply | Ingest a 5-msg conversation, then a 12-msg strict superset with a different `native_id` → candidate created, `--apply` sets `superseded_by`, `v_conversations` returns one row, `--undo` restores both. |
| Exclusions guard | `chatvault ingest $CHATVAULT_ROOT/raw_exclusions` exits non-zero without `--force`. |
| Manifest nag | A subdir in `raw_exclusions/` absent from `_why.md` produces a `⚠ undocumented` manifest line. |
| Annotation idempotency | `curation import` run twice → second run reports `0 added`, and total annotation count is unchanged. |
| Annotation preservation | Add a new annotation post-export, re-import the old file → the new annotation still exists. **This is the important one.** |

# Spec — CV content in the Brain (Brain as source, YAML as artifact)

**For:** Claude Code · **Sonnet, mid effort** · §2 migration ~1h · §3 commands ~3h · §4 export ~2h
**Written:** 2026-08-26 · **Rewritten same day** — the first version got the architecture backwards
**Relates to:** `cv-workflow-spec.md`, `42_SPEC_brain_db.md`

---

## 0. Correcting the previous version of this document

The earlier draft warned against putting CV content in the Brain, on the grounds that it would create two sources of truth and undo what the CV spec fixed.

**That warning was aimed at the wrong thing.** It confused *duplication* with *relocation*.

The CV spec's actual principle is:

> *"One source of truth. Content lives in YAML. Markdown, LaTeX, PDF are all build artifacts."*

The important half of that sentence is **"one source, everything else is generated."** Which layer holds the source is a separate question — and moving it up one level keeps the principle perfectly intact:

```
BEFORE:   master.yaml  →  Typst  →  PDF
AFTER:    brain.sqlite →  master.yaml  →  Typst  →  PDF
                          └── generated, committed, never hand-edited
```

Still one source. Still everything downstream generated. **No drift, because nothing is edited in two places.**

And the "claim vs evidence" split I proposed was the wrong distinction anyway. What you're preserving isn't a claim needing evidence — it's **the cognitive work of vetting sentences**. Those sentences *are* the asset. They belong in the place built to hold assets you can't regenerate.

**Brain becomes the source. YAML becomes a build artifact.**

---

## 1. Why this is actually the better architecture

| What you need | YAML | Brain |
|---|---|---|
| All versions of a bullet in one place | scattered across variant files | **one parent, N children** |
| "Give me the shortest version for company X" | grep + read + judge | **one query** |
| Rank your preference between versions | a comment | **a column** |
| Notes on when to use which | a comment | **a field** |
| Find a sentence you wrote 8 months ago | remember which file | **FTS** |
| Never lose a rejected version | delete or clutter | `status='archived'`, still searchable |
| Ask an AI to pick | paste files, hope | MCP query against one table |

The decisive one is the last: **there is one place to ask.** That's the same reason ChatVault exists, applied to the CV.

---

## 2. Schema — four generic columns, no CV special-casing

```sql
-- migration: brain 002
ALTER TABLE items ADD COLUMN parent_id INTEGER REFERENCES items(id);
ALTER TABLE items ADD COLUMN slug      TEXT;     -- stable human key: 'intuicell_ai'
ALTER TABLE items ADD COLUMN rank      INTEGER;  -- your preference, 1 = best
ALTER TABLE items ADD COLUMN attrs     TEXT;     -- JSON. anything you want.

CREATE INDEX ix_items_parent ON items(parent_id);
CREATE UNIQUE INDEX ix_items_slug ON items(slug) WHERE slug IS NOT NULL;

-- filterable without committing to a fixed vocabulary
ALTER TABLE items ADD COLUMN len TEXT
  GENERATED ALWAYS AS (json_extract(attrs,'$.len')) VIRTUAL;
CREATE INDEX ix_items_len ON items(len);
```

**None of this is CV-specific**, which is the point. `parent_id` serves every hierarchical collection you hoard — checklists with sub-items, reference lists with entries, methods with steps. You said you have "a shitload of useful stuff" in nested folders; this is the shape most of it has.

### New kinds

```
entity      an organisation, person, place — IntuiCell, a client, a course
claim       one CV bullet — the thing you're making a point about
variant     one phrasing of a claim
```

### The hierarchy

```
entity   IntuiCell (dates, role, location in attrs)
└── claim   intuicell_ai            "built AI research tooling"
    ├── variant  default    rank 1  len:medium   "…"
    ├── variant  short      rank 2  len:short    "…"
    └── variant  detailed   rank 3  len:long     "…"  attrs.aud:[technical]
```

`why` stays mandatory at every level — on a variant it's *"when would I use this one?"*, which is exactly the note you keep losing.

### Example row

```json
kind:      "variant"
parent_id: 42
title:     "intuicell_ai — short"
why:       "One-liner for the ATS-scanned version. Drops the methodology."
body:      "Built evaluation tooling for LLM-based research workflows."
rank:      2
attrs:     {"len":"short","chars":63,"aud":["recruiter","ats"],"vetted":"2026-03"}
```

`attrs` being open JSON is the "virtually any metadata I choose" you wanted. Add `char_count`, `used_in`, `last_reviewed`, whatever — no migration needed.

---

## 3. Commands

```bash
# structure
brain cv entity add "IntuiCell" --dates 2023-01/2023-12 --role "…" --why "…"
brain cv claim add --entity IntuiCell --slug intuicell_ai --why "…"
brain cv variant add --claim intuicell_ai --name short --len short --rank 2 \
                     --why "ATS version, drops methodology" --text "…"

# the retrieval you described
brain cv show IntuiCell                       # entity, claims, all variants, ranked
brain cv pick intuicell_ai --len short        # the one sentence
brain cv pick intuicell_ai                    # rank 1
brain cv variants intuicell_ai                # all of them, side by side, with why + rank

# import what already exists
brain cv import-yaml content/master.yaml      # one-time, from your current CV
brain cv import-md <file> --entity IntuiCell  # from the scattered markdown
```

**`brain cv variants intuicell_ai` is the command that fixes your actual pain** — *"where did we put top-ranked version z of company x?"* becomes one line, showing every version with its rank and its note, in one place.

---

## 4. Export — YAML as a build artifact

```bash
brain cv export --profile ai-engineering --out content/master.yaml
```

Emits exactly the shape `cv-workflow-spec.md` §3 defines, with `slug` becoming the bullet `id` — so **the Typst template and `build.sh` need no changes at all.**

```yaml
# GENERATED by `brain cv export` 2026-08-26 — do not hand-edit
experience:
  - id: exp_intuicell
    org: IntuiCell
    dates: 2023-01/2023-12
    bullets:
      - id: intuicell_ai
        text: "Built evaluation tooling for LLM-based research workflows."
        tags: [ai, research, systems]
```

A **profile** decides which claims and which variant of each — that's what `content/variants/*.yaml` did, now stored as a brain item of kind `cv_profile` holding a slug→variant map.

**Still commit the generated YAML.** Git then gives you a readable diff of what changed between applications, which the database alone doesn't. The brain is the source; git remains the changelog. Belt and braces, and free.

---

## 5. Migration path — non-destructive, reversible

1. `brain cv import-yaml content/master.yaml` — your current CV becomes entities/claims/variants (`default`, rank 1)
2. `brain cv import-md` over the scattered markdown — every alternate phrasing you've written becomes a variant, `status='unvetted'`
3. **Review in Datasette:** filter `kind=variant`, `status=unvetted`. Rank them, write the `why`, archive the dead ones.
4. `brain cv export` → compare against the existing `master.yaml`. **They should match.** That's the acceptance test.
5. Only then mark the YAML generated-only

**Nothing is deleted at any step.** The old `master.yaml` stays in git; rejected variants become `status='archived'` and remain searchable. If this turns out worse than YAML-by-hand, `brain cv export` still produces a valid file and you carry on as before, having lost nothing.

---

## 6. What this unlocks beyond the CV

The same three kinds cover most of what you're describing as scattered hoard:

| Your material | Shape |
|---|---|
| Reference lists in nested folders | `entity` → `claim` children |
| Command cheatsheets | `entity` (tool) → `claim` (task) → `variant` (one-liner vs explained) |
| Cover letter paragraphs | exactly the CV shape, different tag |
| Vetted prompts | `claim` (purpose) → `variant` (phrasings), ranked |

**This is the same pattern you already recognised for the CV**, which is a good sign it's the right abstraction rather than a CV feature wearing a general name.

---

## 7. Order

| # | What | Effort | Note |
|---|---|---|---|
| 1 | Migration: `parent_id`, `slug`, `rank`, `attrs`, `len` | ~1h | Generic. Useful regardless of CV. |
| 2 | `brain cv entity/claim/variant add`, `show`, `pick`, `variants` | ~3h | The daily surface |
| 3 | `import-yaml` + round-trip test against current `master.yaml` | ~2h | **Step 4 in §5 is the acceptance gate** |
| 4 | `export --profile` | ~2h | Then YAML is generated-only |
| 5 | `import-md` over the messy folder | ~2h | Do after `41_SPEC` md-scan, so you know what's there |

**Steps 1–2 are usable on their own.** You can start entering vetted sentences by hand today and get the retrieval benefit immediately, before any import or export exists.

---

## 8. Acceptance criteria

| Test | Passes when |
|---|---|
| Round trip | `import-yaml` then `export` reproduces the original `master.yaml` semantically |
| Template untouched | `cv.typ` and `build.sh` need **zero** changes |
| Retrieval | `brain cv pick intuicell_ai --len short` returns exactly one sentence |
| Side by side | `brain cv variants <slug>` shows every version with rank and why |
| Nothing lost | Archived variants remain findable via `brain search` |
| Generic | `parent_id` hierarchies work for a non-CV list with no CV code involved |
| Vault untouched | `chatvault stats` unchanged by every operation here |

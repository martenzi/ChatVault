# Brain — what you can do today, what's missing, and where Neo4j fits

**Written:** 2026-08-26 · verified against the live `db/brain.sqlite` (currently **0 items**)

---

## 1. What exists right now

**Four commands. That's the whole surface.**

```bash
chatvault brain add "<title>" --why "<why>" [--kind K] [--body-file FILE]
chatvault brain list [--kind K] [--tag T] [--confidence C]
chatvault brain show <n>
chatvault brain search "<query>"
chatvault brain promote --uid/--pick/--from-stash --why "..."
```

`kinds`: `conversation · fact · method · decision · note · link · snippet`

**Columns available:** `kind · title · why · body · url · source_*  · status · superseded_by · confidence · created_at`

---

## 2. What's missing — measured, not guessed

### 2a. Gaps that block the CV case

| Missing | Consequence |
|---|---|
| `parent_id` | **No nesting.** Variants can't sit under a claim, claims can't sit under a company. Everything is flat. |
| `rank` | No preference ordering between variants |
| `attrs` (JSON) | Nowhere for `len`, `audience`, `char_count`, `vetted_date` |
| `slug` | No stable key, so nothing can join to a CV bullet ID |

### 2b. Gaps that block *everything*, including non-CV use

| Missing | Consequence |
|---|---|
| **`brain tag` command** | `tags` and `tag_links` tables exist, and `list --tag` filters on them — **but nothing can write a tag.** The filter is unreachable. |
| `--body` inline | Only `--body-file`. Adding a one-sentence item means creating a temp file. |
| `brain edit` | Can't revise an item after adding it |
| `brain relate` | `item_links` exists, nothing writes to it |
| `brain set` | Can't change `confidence` or `status` after the fact |

**The tag gap is the important one.** Tagging is your primary organising instinct everywhere else in this system, and in the brain it currently isn't reachable at all.

---

## 3. Test it today — the ugly way, in five minutes

You can feel the shape right now without any new code. Encode structure in the title:

```bash
# create three variants of one claim as flat items
printf 'Built evaluation tooling for LLM-based research workflows.' > /tmp/v1.md
chatvault brain add "IntuiCell :: intuicell_ai :: short" \
  --kind snippet --why "ATS version. Drops methodology. rank 2. len=short, 63 chars." \
  --body-file /tmp/v1.md

printf 'Designed and built evaluation tooling for LLM-based research workflows, including comparison harnesses across model families.' > /tmp/v2.md
chatvault brain add "IntuiCell :: intuicell_ai :: default" \
  --kind snippet --why "Default. Balanced. rank 1. len=medium." \
  --body-file /tmp/v2.md

printf 'Designed and built evaluation tooling for LLM-based research workflows — comparison harnesses across model families, reproducible prompt/eval configs, and result analysis feeding directly into research decisions.' > /tmp/v3.md
chatvault brain add "IntuiCell :: intuicell_ai :: detailed" \
  --kind snippet --why "Technical audience / research roles. rank 3. len=long." \
  --body-file /tmp/v3.md
```

Then:

```bash
chatvault brain list --kind snippet
chatvault brain search "IntuiCell"
chatvault brain search "evaluation tooling"
chatvault brain show 2
```

**What you'll notice — and this is the point of the exercise:**

- ✅ Search finds them. Three vetted sentences in one place already beats three markdown files.
- ✅ The `why` field genuinely carries the "when do I use this?" note.
- ❌ `rank 2` inside prose is not sortable. You can read it; you can't query it.
- ❌ `len=short` in prose is not filterable. *"Give me the shortest"* still means reading all three.
- ❌ Nothing connects these to the *company* except a string convention you have to keep typing.
- ❌ A temp file per sentence is friction you'd never tolerate at scale.

**That's the honest state.** Useful as a scratchpad, not yet a source of truth. The gaps in §2 are exactly what those four ❌ lines cost.

---

## 4. The migration that fixes it

Two migrations, both small. **Do 4a even if you never touch the CV** — it unblocks tagging, which everything needs.

### 4a. Make the existing schema reachable (~2h)

```bash
chatvault brain tag <n> <tag> [<tag>...]     # writes tags — currently impossible
chatvault brain untag <n> <tag>
chatvault brain tags                          # tree view with counts, like `chatvault tag tree`
chatvault brain add ... --body "inline text"  # no temp file
chatvault brain add ... --tag cv/intuicell    # tag at creation
chatvault brain edit <n>                      # $EDITOR on the body
chatvault brain set <n> --confidence verified --status archived
chatvault brain relate <a> <b> --rel extends --note "..."
```

No schema change at all — every table already exists.

### 4b. Hierarchy and sortable metadata (~1h migration + ~3h commands)

```sql
-- brain migration 002
ALTER TABLE items ADD COLUMN parent_id INTEGER REFERENCES items(id);
ALTER TABLE items ADD COLUMN slug      TEXT;
ALTER TABLE items ADD COLUMN rank      INTEGER;
ALTER TABLE items ADD COLUMN attrs     TEXT;      -- JSON, open-ended

ALTER TABLE items ADD COLUMN len TEXT
  GENERATED ALWAYS AS (json_extract(attrs,'$.len')) VIRTUAL;

CREATE INDEX ix_items_parent ON items(parent_id);
CREATE INDEX ix_items_len    ON items(len);
CREATE UNIQUE INDEX ix_items_slug ON items(slug) WHERE slug IS NOT NULL;
```

Then the same three entries become:

```bash
chatvault brain add "IntuiCell" --kind entity --slug intuicell \
  --why "AI research role 2023" \
  --attrs '{"dates":"2023-01/2023-12","role":"Research Engineer","location":"Lund"}'

chatvault brain add "AI research tooling" --kind claim --slug intuicell_ai \
  --parent intuicell --why "Core technical claim for this role"

chatvault brain add "short" --kind variant --parent intuicell_ai --rank 2 \
  --why "ATS version. Drops methodology." \
  --attrs '{"len":"short","chars":63,"aud":["recruiter","ats"]}' \
  --body "Built evaluation tooling for LLM-based research workflows."

chatvault brain add "default" --kind variant --parent intuicell_ai --rank 1 \
  --why "Balanced. Use unless there's a reason not to." \
  --attrs '{"len":"medium"}' --body "Designed and built…"

chatvault brain add "detailed" --kind variant --parent intuicell_ai --rank 3 \
  --why "Technical / research audience." \
  --attrs '{"len":"long","aud":["technical","research"]}' --body "Designed and built…"
```

And the retrieval you actually wanted:

```bash
chatvault brain tree intuicell           # entity → claims → variants, ranked
chatvault brain pick intuicell_ai --len short     # exactly one sentence
chatvault brain pick intuicell_ai                 # rank 1
chatvault brain variants intuicell_ai             # all, side by side, with why + rank
```

**`--attrs` being open JSON is the "any metadata I choose" you asked for.** Add `used_in`, `last_reviewed`, `source_conv` — no migration. Only fields you want to *filter* on need a generated column, and that's one line each.

---

## 5. Neo4j — the concrete answer

### For this CV case: no, and here's why in one comparison

Your data is `entity → claim → variant`. **Depth 2, fixed shape, known in advance.** That's a tree, not a graph.

**Same question, both databases —** *"give me the short variant of every IntuiCell claim"*:

```sql
-- SQLite
SELECT c.slug, v.body
FROM items e
JOIN items c ON c.parent_id = e.id AND c.kind = 'claim'
JOIN items v ON v.parent_id = c.id AND v.kind = 'variant'
WHERE e.slug = 'intuicell' AND v.len = 'short';
```

```cypher
// Neo4j
MATCH (e:Entity {slug:'intuicell'})<-[:BELONGS_TO]-(c:Claim)<-[:VARIANT_OF]-(v:Variant {len:'short'})
RETURN c.slug, v.body
```

Both are five lines. Both are instant. **But the Cypher version costs you a running server, a sync step, and a second copy of the data — for identical output.** When the shape is known and shallow, SQL wins on everything except aesthetics.

### Where Neo4j genuinely wins — and it's not hypothetical

The question SQL handles badly:

> *"This job ad wants 8 skills. Which of my claims across all entities touch those skills, which skills does it want that I've never claimed, and what's the shortest connection between my strongest claim and the gap?"*

That's variable-depth traversal across a network you didn't design in advance. In SQL it's a recursive CTE with exploding intermediate results; in Cypher it's natural.

**But it requires a skills graph that doesn't exist yet.** Today the brain has **zero items**. Neo4j on an empty graph tells you nothing.

### The concrete threshold

**Rebuild the graph when `item_links` has roughly 200+ edges** — i.e. when you've recorded enough `extends` / `contradicts` / `see_also` relationships that *"what connects to what"* is a question you actually have. Until then it's a server that answers questions you can already answer.

Practically: `brain relate` (§4a) has to exist and be used for months before Neo4j earns anything. **Same one-way rule as ChatVault — SQLite is the source, Neo4j is a disposable projection.**

---

## 6. Order

| # | What | Effort | Why |
|---|---|---|---|
| 1 | **§3 — add the three variants by hand today** | 5 min | Feel the gaps yourself before committing to a design |
| 2 | **§4a — tag/untag/tags, `--body`, `edit`, `set`, `relate`** | ~2h | No schema change. **Unblocks tagging, which is currently unreachable.** |
| 3 | §4b migration — `parent_id`, `slug`, `rank`, `attrs`, `len` | ~1h | Generic hierarchy, not CV-specific |
| 4 | §4b commands — `tree`, `pick`, `variants` | ~3h | The retrieval you described |
| 5 | Start entering vetted sentences for real | — | This is the actual goal |
| 6 | Datasette on `brain.sqlite` (port 8002) | ~15 min | Your preferred browsing surface, free |
| — | *Neo4j* | — | **Not until §5's threshold.** Months away. |

**Step 1 costs five minutes and will tell you more than this document does.** Do that before deciding anything else.

**Step 2 is the one I'd push hardest for** — the tag tables and the `--tag` filter already exist, and nothing can write to them. That's a half-built feature sitting in your way, and it blocks every use of the brain, not just the CV one.

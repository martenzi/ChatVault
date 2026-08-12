# ChatVault — Spec v1.5: Taxonomy, Tagging & Analysis

**The question this answers:** can ChatVault replace my nested folder organisation, and does it make my portfolio-analysis scripts obsolete?

**Short answers:** yes to the folders, and your scripts don't become obsolete — they get a much better place to put their results.

---

## 1. The one idea that changes everything

**A folder is a tree. One conversation, one location. You must choose.**

**A tag is a graph. One conversation, many locations. You never choose.**

Your npm conversation is a good example. It is genuinely about:

- macOS package managers
- Docker (that's what you were installing when it broke)
- Troubleshooting
- A system decision you made and want to remember
- Something still unresolved

In Finder you must pick one folder, or duplicate the file five times and then keep five copies in sync forever. That's why "good enough" is where you stopped — not laziness, a real structural limit you correctly sensed.

In the database it gets all five, costs nothing, and every one of them finds it later.

**This is why your Finder work stalled, and it's the single biggest thing you gain.**

---

## 2. Nested taxonomy — you keep it, you get several

Tags are hierarchical by naming them with slashes. This already works in the current schema; nothing new is needed:

```
tech/macos/package-managers/npm
tech/macos/package-managers/homebrew
tech/containers/docker
```

Ask for `tech/macos/*` and you get everything underneath, at any depth. So your nested instinct survives intact.

**But here's the upgrade.** You don't get *one* tree. You get as many parallel trees as you want, all applying to the same conversation at the same time:

| Tree | Example tag | Answers |
|---|---|---|
| **Topic** | `tech/macos/package-managers/npm` | What is this about? |
| **Project** | `project/claude-web/PSA/subtopic` | Where does it belong? |
| **Purpose** | `purpose/troubleshooting` | Why did I open this chat? |
| **Skill** | `skill/python/api-integration` | What does this prove I can do? |
| **Status** | `status/unresolved` | Is this finished? |
| **Value** | `value/reference` · `value/noise` | Keep or ignore? |

One conversation sits in one place in each tree. Six coordinates instead of one folder path. Your PSA subfolders become `project/claude-web/PSA/<subfolder>` — the structure you already invented, preserved exactly, but now able to coexist with five other structures.

**Convention, not new machinery:** the first segment of a tag name is its tree. `tech/`, `project/`, `skill/`, `status/`, `purpose/`, `value/`. That's the whole design.

---

## 3. Your folder work is not wasted — it's the starting taxonomy

You already did the hard part. Sorting conversations into project folders and inventing subfolders **is** taxonomy design, and it's the part no tool can do for you.

So don't retype it. Walk the folder tree, turn each path into a tag:

```
PSA/troubleshooting/network.md   →   project/claude-web/PSA/troubleshooting/network
```

```
chatvault tag import-from-folders <path> --prefix "project/claude-web" --dry-run
```

Matches files to conversations by title and date, reports anything it can't match so you can fix those by hand. **Your Finder organisation becomes the seed taxonomy in one command**, and from then on it lives somewhere it can be combined with everything else.

---

## 4. Folders come back — as output, not as commitment

The part you'll like most: you can still have beautiful nested folders. They're just generated now, not maintained.

```bash
chatvault export --tree "project/claude-web/**" --out ~/Chats/ByProject/
chatvault export --tree "tech/**"              --out ~/Chats/ByTopic/
chatvault export --tree "skill/**"             --out ~/Chats/BySkill/
```

Each command writes a full nested folder structure of markdown, mirroring that tag tree.

**And you can have all three at once**, from the same conversations, without duplicating anything meaningful. The npm chat appears in the topic tree *and* the troubleshooting tree *and* the project tree.

Don't like the structure? Delete the folder, change the tags, regenerate. **Folders stop being a commitment and become a view** — which is exactly what they should always have been. The database holds the truth; folders are a picture of it taken from one angle.

---

## 5. The npm problem — your real pain

You described it precisely: the conversations aren't *about* npm, they're about troubleshooting three other things while npm quietly caused the damage. Keyword search fails because you can't guess the phrasing you used eight months ago.

**Three layers, and you need all three:**

### Layer 1 — Better keyword search *(works today)*
FTS5 supports `NEAR`, `OR`, prefixes: `npm OR node_modules OR "package manager"`. Helps. Not enough.

### Layer 2 — Semantic search *(the actual answer — build this)*
Every conversation gets converted into a list of numbers representing its meaning. Then you search by *meaning*, not words:

> "package manager conflicts causing problems on macOS"

and it finds the conversation that never once used that phrasing. This is `sqlite-vec` plus a local embedding model — stays on your machine, no API cost, runs once over the corpus and then incrementally.

I originally filed this as optional Phase 7. **Your npm example moves it to essential.** It is the difference between a searchable archive and one that actually answers questions.

### Layer 3 — Tag what you find *(the durable part)*
Semantic search is how you *discover*. Tagging is how you *never search again*.

```bash
chatvault search --semantic "npm package manager conflicts macos"
chatvault tag add "tech/macos/package-managers/npm" --pick 1,3,4,7
chatvault note --pick 3 "root cause of the Docker breakage — start here"
```

Fifteen seconds, and that question is permanently answered. Next time it's `chatvault find --tag tech/macos/package-managers/npm` and you're done.

**The pattern to internalise: search is for discovery, tags are for memory.** Every time a search takes real effort, tag the result. Your retrieval system builds itself out of your actual frustrations, which is the only reliable way it ever gets built.

---

## 6. Your portfolio scripts — what changes

Reading your READMEs: the analysis logic is good and stays. What becomes obsolete is the plumbing around it.

| Your script does | In ChatVault |
|---|---|
| Parse `conversations.json` from scratch | `SELECT` — already parsed, indexed, deduplicated |
| Filter technical vs personal by keyword density | Same logic, but reads `v_blocks` |
| Handle multiple export formats | Gone. One shape for every platform. |
| Write results to `analysis_results.json` | **Write tags back into the database** |
| Write markdown reports | `chatvault export --tree "skill/**"` |
| Re-run everything when the export updates | Runs on new conversations only |

**The important shift is the output.** Right now your finding — *"148 conversations show technical work, 136 show LLM testing"* — lives in a JSON file. It's a snapshot. It can't be combined with anything, and it's already going stale.

As tags, the same finding becomes live and composable:

```bash
# things your JSON file cannot answer, that tags answer instantly
chatvault find --tag skill/llm-evaluation --tag status/unresolved
chatvault find --tag skill/docker --since 2026-01-01 --platform claude-code
chatvault stats --by tag --prefix skill/
```

And because `tag_links` records *how* a tag was applied (`manual`, `rule`, or `llm`), you can always separate what you decided from what a script guessed — and override any of it.

**Concretely: keep `technical_portfolio_extractor.py`'s scoring logic. Change its input to a SQL query and its output to `chatvault tag add`.** Probably an hour of work, and the tool stops being a one-shot report generator and becomes part of the system.

### Automatic rules

For patterns you can express, tag automatically on ingest:

```bash
chatvault tag rule add "tech/containers/docker" \
  --where "blocks.text LIKE '%docker%' OR blocks.text LIKE '%dockerfile%'" \
  --min-hits 3
```

Applied to every future conversation, marked `source='rule'`, always reversible. Your keyword-density logic fits here almost unchanged.

---

## 7. Realistic expectations — you asked, so plainly

**Easy, works well, do it now:**
- Hierarchical tags, unlimited depth, several parallel trees
- Filtering and combining any of them
- Exporting any tag tree as nested folders
- Importing your existing folder structure as tags
- Rule-based auto-tagging for anything keyword-shaped

**Real work, but worth it:**
- Semantic search — a day to build, then it just runs
- Bulk AI tagging of 5,000 conversations — an AI proposes, you review. Expect a few hours spread over weeks, not one sitting.

**Will not happen, and don't chase it:**
- A machine building a taxonomy that matches how *you* think. The categories are yours. AI can suggest and can apply them at scale; it cannot decide that `tech/macos/package-managers` matters to you and `tech/macos/fonts` doesn't.
- Perfect coverage. Some conversations stay untagged forever. That's fine — semantic search still finds them.

**The honest split:** the tool does the labour, you do the judgement. That's a good trade, and the judgement is the part worth your time anyway.

---

## 8. The trap — please read this one

You are exactly the person who will design a beautiful 200-node taxonomy before tagging a single conversation, and then never tag anything because the taxonomy isn't finished yet.

**Don't design it upfront. Grow it from real needs.**

The npm example is the correct pattern, and notice how it went:

1. You needed something
2. Searching for it was annoying
3. *Therefore* a tag deserves to exist

A tag that comes from a real retrieval failure is guaranteed useful. A tag invented in advance is a guess.

**Start with maybe ten tags.** Add one every time a search is harder than it should be. In three months you'll have a taxonomy that fits your actual mind, built entirely out of moments of friction — and it will be better than anything you could have designed in advance, because it was designed by evidence.

---

## 9. What to build

| # | Item | Size |
|---|---|---|
| 1 | `tag import-from-folders` — your existing structure becomes tags | small |
| 2 | Hierarchical tag queries (`--tag "tech/**"`), `find --tag`, `stats --by tag` | small |
| 3 | `export --tree` — any tag tree becomes nested folders | medium |
| 4 | **Semantic search** (`sqlite-vec` + local embeddings) | medium — highest value |
| 5 | `tag rule add` — automatic tagging on ingest | small |
| 6 | Port `technical_portfolio_extractor.py` to read SQL and write tags | small |

Build 1–3 first: they turn your existing Finder work into something live, which is immediate payoff for zero new thinking. Then 4, which is what makes the npm problem go away for good.

## 10. Acceptance criteria

| Test | Passes when |
|---|---|
| Folder import | A nested folder tree becomes matching hierarchical tags; unmatched files are reported, not silently dropped |
| Multi-tree | One conversation carries tags from `tech/`, `project/` and `skill/` simultaneously, and each tree finds it |
| Prefix query | `--tag "tech/macos/**"` returns everything at any depth below |
| Tree export | `export --tree "project/**"` reproduces a nested folder structure of markdown |
| Two views | The same conversation appears in both a topic-tree export and a project-tree export |
| Semantic search | A query using none of the conversation's actual words still finds it |
| Rule tagging | A new ingest auto-applies matching rules with `source='rule'` |
| Reversibility | Every tag applied in bulk can be removed in bulk |

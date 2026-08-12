# ChatVault — Tagging: What You'll Actually See and Do

**Revises:** archive/CHATVAULT_INTERFACE_STRATEGY.md §3, which was wrong about tagging.

I told you Datasette would cover browsing and filtering. That's true. I implied it would cover tagging too. **It won't** — Datasette is built for reading, not for putting data in. Clicking tags onto fifty conversations is not something it does well. Correcting that here.

---

## 1. How these compare to macOS Finder tags

You already have the right mental model. Same idea, bigger.

| | macOS tags | ChatVault tags |
|---|---|---|
| One item, many tags | ✅ | ✅ |
| Filter by tag | ✅ | ✅ |
| Nesting | ❌ flat list | ✅ unlimited depth |
| How many before it's unusable | ~20 | thousands |
| Applied by a rule automatically | ❌ | ✅ |
| Applied by an AI in bulk | ❌ | ✅ |
| Remembers *who* tagged it | ❌ | ✅ you / rule / AI |
| Combine several at once | clumsy | native |

**No connection to macOS tags whatsoever.** Different system, same instinct. Nothing you do in Finder affects ChatVault or vice versa.

The one that matters most is the last row: *"show me everything tagged `tech/macos/**` AND `status/unresolved` AND not tagged `value/noise`"* is a normal question here and impossible in Finder.

---

## 2. Yes — define your tag tree in advance

You can, and it actually helps. My earlier warning was aimed at something narrower than it sounded.

Write a plain text file:

```yaml
# taxonomy.yml
tech:
  macos:
    package-managers: [npm, homebrew, pip, uv]
    system-config:
    troubleshooting:
  containers: [docker, podman]
  databases: [sqlite, neo4j, postgres]
  ai: [llm-testing, prompting, agents, mcp]

purpose: [troubleshooting, learning, building, planning, reference]
status: [unresolved, solved, superseded]
value: [gold, reference, noise]
```

```bash
chatvault tag import-tree taxonomy.yml
```

Now they exist, they're in the palette, and you click them instead of typing them. **Pre-defining makes application faster, not slower.**

**The warning, restated precisely:** don't let *finishing* the taxonomy become a prerequisite for *starting* the tagging. Import twenty tags today, tag some conversations, and add branches as you discover you need them. Editing the tree later is free — `chatvault tag rename "tech/ai" "tech/llm"` updates every conversation carrying it.

The failure mode isn't planning. It's perfectionism.

---

## 3. What tagging actually looks like

This is the screen to build. It's one screen in the terminal app, **clickable with the mouse and drivable from the keyboard** — Textual supports both, so you don't have to choose.

```
┌─ FILTERS ──────────────────────────────────────────────────────────────┐
│ search: "npm package manager conflict"        ⚡ semantic     47 results │
│ ● platform: any   ● untagged only   ○ date: any                        │
└────────────────────────────────────────────────────────────────────────┘
┌─ RESULTS ─────────────────────────────┬─ TAGS ──────────────────────────┐
│ ☑ 1  2026-03-04  Homebrew doctor errs │ ▾ tech                          │
│ ☑ 2  2026-03-04  Docker install fails │   ▾ macos                       │
│ ☐ 3  2025-11-20  Node version mess    │     ▾ package-managers          │
│ ☑ 4  2025-11-19  Cleaning node_modul… │       [npm] ← click to apply    │
│ ☐ 5  2025-09-02  Python env confusion │       [homebrew]  [pip]  [uv]   │
│ ☑ 6  2025-08-30  PATH broken after up │     ▸ system-config             │
│ ☐ 7  2025-08-30  Jupyter kernel gone  │   ▸ containers                  │
│                                        │ ▾ status                        │
│ 4 selected                             │   [unresolved] [solved]         │
│                                        │ ▾ value                         │
│ [a] select all  [n] none  [space] togg │   [gold] [reference] [noise]    │
└───────────────────────────────────────┴─────────────────────────────────┘
 recently used:  [tech/macos/package-managers/npm]  [status/unresolved]
 [/] search   [t] tag selected   [u] untag   [enter] read   [s] save view
```

**The loop, in practice:**

1. Type a search — plain words or semantic, toggled with one key
2. Results appear as a checkbox list
3. Space-bar or click to select the ones that belong
4. Click a tag in the right-hand tree — **applies to everything selected at once**
5. Repeat

Selecting six conversations and clicking two tags is about **ten seconds**. That's the sorting experience you described, and it's the whole point of building this screen.

**Details that make it fast rather than tedious:**

- **Recently-used tags** sit along the bottom. In a sorting session you use the same three tags repeatedly; they're always one click away.
- **"Untagged only"** filter, so you can work through the backlog without re-seeing what you've done.
- **Read before deciding** — Enter opens the conversation in a pane, Escape returns with your selection intact.
- **Undo.** `Cmd-Z` reverses the last tagging action. Bulk mistakes are cheap.
- **Multi-select survives re-searching**, so you can gather from several searches and tag them together.

---

## 4. The four surfaces, corrected

| Surface | What it's for | Build cost |
|---|---|---|
| **Tagging screen** (Textual) | **Sorting, tagging, curating.** Where you'll spend real time. | *build this* |
| **Datasette** (web) | Browsing, filtering, stats, exporting a filtered set. **Reading, not writing.** | ~zero |
| **AI via MCP** | "Find that conversation about the protobuf thing." Fuzzy recall. | already done |
| **Markdown export** | Long-form reading in Obsidian; folder trees | already done |

**What changed from my earlier advice:** I said don't build a custom app because Datasette covers it. That holds for *browsing*. It doesn't hold for *tagging* — no off-the-shelf tool does bulk tagging against your own schema, because your schema is yours. This one screen is the exception, and it's worth building.

**Roughly a day of Sonnet's time.** It's one screen: a query, a list with checkboxes, a tree of buttons, and one write operation.

### The AI shortcut

For the times you don't want to click at all:

> "Tag every conversation about npm or node package conflicts as `tech/macos/package-managers/npm`"

The AI runs the search, applies the tags, marks them `source='llm'`, and reports what it did. You review with `find --tag ... --source llm` and correct anything wrong. Good for coarse first passes over thousands of conversations; the clicking is for the judgement calls afterwards.

---

## 5. How long this actually takes

Concrete numbers, since "it'll be fast" is not a plan.

**Bulk pass (AI-assisted), ~5,000 conversations:** you describe ten to fifteen broad categories, AI proposes, you spot-check. **A couple of hours**, mostly waiting. Gets you maybe 60% coverage at coarse resolution.

**Manual refinement:** a focused sorting session clears **100–200 conversations an hour** once the palette is set up and your hands know the keys. You will not do 5,000 this way, and you shouldn't.

**The realistic pattern — and the one I'd actually bet on:** you tag maybe 300 conversations total, ever, and they're the 300 you kept coming back to. Everything else stays untagged and gets found by search when needed. **Tagging is not filing; it's leaving a note for yourself at the places you know you'll return to.** Coverage is not the goal, and chasing it is how this becomes a chore.

---

## 6. What not to expect

Honest limits, given how you work:

**No polish.** This is a personal tool. Rough edges, occasional bugs, no support. When something breaks you fix it or ask an AI to. That's the price of it being exactly yours.

**Single machine.** One database file on one Mac. No sync to phone or iPad, no access from elsewhere, until you put it on the Linux server — which turns Datasette into a LAN-accessible web app but is a project of its own.

**No live "smart folders."** Finder's saved searches update themselves in the sidebar. Here, a saved view is a query you run — one keystroke, but you run it. There's no ambient always-updating list sitting in a sidebar.

**Nothing organises itself.** AI can apply categories at scale; it cannot decide which categories matter to you. That judgement is permanently yours, and it's the part that's actually worth your attention.

**Images and attachments aren't visually browsable.** No thumbnail grid. You get filenames and extracted text. Building a visual browser is possible but nobody's building it.

**It goes stale if you stop.** Live sources keep growing. Skip re-ingest for six months and the vault is six months behind. Recovery is one command, but the gap is real.

**Renaming across trees needs a moment's thought.** `rename` handles a whole branch cleanly, but reorganising a taxonomy you've deeply nested is still a decision, not a reflex.

**And the real one: every interface is something you build or configure.** There's no App Store version of this. Datasette is five minutes; the tagging screen is a day of Sonnet's time; the rest is glue. Cheap, but not free, and it never quite ends.

---

## 7. What you gain that you can't get anywhere else

Balanced against the above:

- **One conversation, many places at once** — the thing you said matches how you think, and the thing folders structurally cannot do
- **Search by meaning**, so the npm conversations surface without you guessing the words you used a year ago
- **Every angle at once** — topic, project, skill, status, all live simultaneously, none excluding another
- **Folders on demand**, in as many arrangements as you like, regenerated whenever
- **Nothing is ever lost** — excluding, redacting and untagging are all reversible
- **Your judgement compounds.** Every tag you apply makes the next search better, permanently. Finder work never accumulated like that.

---

## 8. Build order for this part

| # | Item | Cost |
|---|---|---|
| 1 | `tag import-tree` from YAML | 1 hour |
| 2 | `tag import-from-folders` — your existing Finder structure | 2 hours |
| 3 | **Tagging screen** (§3) | ~1 day |
| 4 | Semantic search, wired into the same screen | ~1 day |
| 5 | `tag rename`, `tag merge`, undo | half day |

Do 1 and 2 first — they cost almost nothing and mean the tagging screen opens with your taxonomy already populated and a few hundred conversations already sorted. Starting from an empty palette is demoralising; starting from your own existing structure is not.

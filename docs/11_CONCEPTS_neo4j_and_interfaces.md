# Neo4j & Your Tools — In Practice

**Two questions answered:** what Neo4j is actually for in *your* workflow, and which of your interfaces to reach for when.

---

# Part 1 — Your four interfaces

You have more surfaces than you realise, and they overlap confusingly. Here's the map.

| # | What | How you launch it | What you see |
|---|---|---|---|
| 1 | **Bash menus** | Double-click a `.command` file | Numbered text menu, you type `3`, it runs a command and prints |
| 2 | **CLI** | `chatvault find "..."` in a terminal | Prints, returns your prompt |
| 3 | **Textual UI** | `chatvault screen` | Full-screen panels, arrow keys, mouse, stays until `q` |
| 4 | **Datasette** | `datasette db/chatvault.sqlite` → browser | A real web page with clickable filters |

**Your three `.command` files are all category 1.** They're friendly wrappers around category 2 — no memorising commands, no flags to recall. Good design for what they do.

- `chatvault.command` — **the working menu.** Find, semantic search, filter, export, read. Where you'll spend time.
- `chatvault-dashboard.command` — **the status board.** DB size, counts, Neo4j state, ports, MCP health, disk usage. Answers "is everything fine?"
- `chatvault-neo4j.command` — **Neo4j on/off switch.** Nothing else.

**Resolved:** `chatvault.command` menu option 4 ("Browse") now launches this — it calls `chatvault screen` (the command shipped under that name, not `chatvault ui` as originally planned below).

**Also worth knowing:** semantic search requires **Ollama running**. Your menu already warns about it. If semantic results come back empty or error, check Ollama first — it's the usual cause.

## Which to use when

| You want to… | Use |
|---|---|
| See what's in the vault, click around, get a feel | **Datasette** |
| Find and read one conversation | **chatvault.command** menu |
| Select fifteen conversations and tag them all | **Textual UI** — the only one that does multi-select |
| Check everything's healthy | **dashboard.command** |
| Repeat something, or script it | **CLI directly** |
| Ask in plain English | **AI via MCP** |

**Pragmatic ranking for you, right now:**

1. **Datasette** — best value per minute. Zero learning, GUI comfort, fastest way to build a mental picture of your own data.
2. **chatvault.command** — daily driver for find/read/export.
3. **Textual UI** — only when doing a tagging session. Don't force it otherwise.
4. **CLI** — as you learn what you repeat.
5. **Neo4j** — later. See Part 2.

---

# Part 2 — Neo4j

## The concept, in one line

> **SQLite answers questions about things. Neo4j answers questions about paths between things.**

With your actual data:

| Question | Which DB | Why |
|---|---|---|
| "Which conversations are tagged `tech/macos/npm`?" | SQLite | Filtering a set |
| "How many tool calls did Claude Code make in June?" | SQLite | Counting rows |
| "Which conversations share tags with *this* one — and which share tags with *those*?" | **Neo4j** | Following links outward, depth unknown in advance |
| "Which two topics keep showing up together, that I never thought were related?" | **Neo4j** | Pattern across connections |

**The rule of thumb:** you know what you're looking for → SQLite. You want to find out what you didn't know to ask → Neo4j.

## How the two databases relate

```
chatvault.sqlite  ──►  chatvault graph rebuild  ──►  Neo4j
   (the truth)                                     (a rearranged copy)
```

**One direction only.** Neo4j never writes back. It's a copy of a slice of SQLite, restructured so traversal is fast.

**Which means Neo4j is completely disposable.** Delete the whole graph, wipe the container, break it experimenting — rebuild takes seconds and nothing is lost. **You cannot damage your data through Neo4j.** Experiment as recklessly as you like.

## What's in your graph right now

```
(:Conversation) ──[:USED_MODEL]──►  (:Model)
                ──[:USED_TOOL]───►  (:Tool)
                ──[:ON_PLATFORM]─►  (:Platform)
                ──[:IN_PROJECT]──►  (:Project)
                ──[:TAGGED]──────►  (:Tag)
```

## A 20-minute practice session

```bash
# 1. Start it
./chatvault-neo4j.command      # → start

# 2. Fill it from SQLite
chatvault graph rebuild --depth meta

# 3. Open http://localhost:7474 — log in with your .env password
```

Paste these into the Neo4j Browser query bar one at a time.

**See the shape of it** *(this is the "oh, I get it" moment — it draws an actual picture)*
```cypher
MATCH (c:Conversation)-[r]->(n)
RETURN c, r, n
LIMIT 75
```
Drag the nodes around. Click one to see its properties. This is what a graph *is*.

**Which models did you actually use, by volume**
```cypher
MATCH (c:Conversation)-[:USED_MODEL]->(m:Model)
RETURN m.name AS model, count(c) AS conversations
ORDER BY conversations DESC
```

**Conversations that used more than one model** — you switched mid-thread, usually because something was going wrong
```cypher
MATCH (c:Conversation)-[:USED_MODEL]->(m:Model)
WITH c, count(m) AS models
WHERE models > 1
RETURN c.title, models
ORDER BY models DESC LIMIT 20
```

**Tools that co-occur** — which tools you tend to use together
```cypher
MATCH (c:Conversation)-[:USED_TOOL]->(t1:Tool),
      (c)-[:USED_TOOL]->(t2:Tool)
WHERE t1.name < t2.name
RETURN t1.name, t2.name, count(c) AS together
ORDER BY together DESC LIMIT 25
```

**Hub conversations** — the most connected ones, which are usually the substantial ones
```cypher
MATCH (c:Conversation)-[r]->()
RETURN c.title, count(r) AS connections
ORDER BY connections DESC LIMIT 20
```

Then:
```bash
./chatvault-neo4j.command      # → stop
```

Nothing is lost. Rebuild whenever.

## When Neo4j becomes genuinely valuable to you

**Honestly: not quite yet.** With mostly models, tools and platforms in it, almost everything above is answerable in SQLite too. Today it's for intuition, not results.

**It becomes properly useful once you've tagged a few hundred conversations**, because then this works:

```cypher
// Which tags keep appearing together?
MATCH (c:Conversation)-[:TAGGED]->(t1:Tag),
      (c)-[:TAGGED]->(t2:Tag)
WHERE t1.name < t2.name
RETURN t1.name, t2.name, count(c) AS co_occurrences
ORDER BY co_occurrences DESC LIMIT 30
```

**This is the killer use case for you specifically.** You're about to design a taxonomy and you've said you don't know what shape it should take. This query lets *the data tell you*:

- If `tech/macos/npm` and `tech/containers/docker` co-occur 40 times, that's not two topics — that's one recurring problem you keep hitting, and it deserves its own tag
- If a tag co-occurs with nothing, it's probably too specific
- If two tags always appear together, one of them is redundant

**Taxonomy discovered from evidence rather than invented in advance** — exactly the approach that avoids the trap of building a beautiful structure you never use.

And the one after that, once concepts exist:

```cypher
// Bridge conversations — where two separate areas of my work actually met
MATCH (p1:Project)<-[:IN_PROJECT]-(c)-[:TAGGED]->(t:Tag)<-[:TAGGED]-(c2)-[:IN_PROJECT]->(p2:Project)
WHERE p1.name < p2.name
RETURN p1.name, p2.name, collect(DISTINCT t.name)[0..5] AS shared_topics, count(*) AS strength
ORDER BY strength DESC LIMIT 15
```

## Practical answer

**Run the 20-minute session once**, for intuition and because you'll enjoy seeing the picture. Then **turn it off and leave it off.**

Come back to it after your first serious tagging session — that's when the co-occurrence query starts telling you things you don't already know. Set yourself a marker: **200 tagged conversations, then rebuild the graph and run the tag co-occurrence query.**

Between now and then, Neo4j costs you nothing by being off, and gains you nothing by being on.

---

# Part 3 — Suggested additions to your launchers

Small, worth asking Claude Code for:

1. ~~`chatvault.command` → add "Open Textual UI"~~ — **done.** Menu option 4 calls `chatvault screen`.
2. **`chatvault.command` → add "Tag these results"** after a search, so find→tag is one flow rather than two tools.
3. **`dashboard.command` → show Ollama status** next to Neo4j. It's a live dependency of semantic search and currently invisible until something fails.
4. **`dashboard.command` → show tag count and untagged conversation count.** As tagging becomes your main activity, that's the number you'll want to watch.

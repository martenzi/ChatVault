# ChatVault — Architecture & Build Specification

**Version:** 1.0
**Date:** 2026-07-30
**Author role:** architecture spec. Written so a competent coding model (Sonnet-class or better) can implement it without further design decisions.
**Owner:** Martenzi (macOS Sequoia)

---

## 0. Read this first (implementer briefing)

You are building a local-first archive and analysis system for AI chat history exported from many platforms.

**Non-negotiable design rules:**

1. **RAW files are append-only.** Adapters read them. Nothing ever writes back to a source export.
2. **SQLite is the system of record.** Neo4j is a *derived projection*, rebuildable from SQLite with one command. Never write to Neo4j as primary.
3. **Nothing is ever DELETEd.** "Cleanup" = setting `excluded = 1`. All read paths go through views that filter excluded rows. Deletion is a separate, explicit, rarely-used command.
4. **Every row keeps its `raw_json`.** Normalized columns exist for filtering; the blob exists for fidelity. Losing source detail is a bug.
5. **Ingest is idempotent.** Running ingest twice over the same directory must produce zero new rows and zero duplicates. This is a hard acceptance test.
6. **Markdown export is pure SQL + template.** No LLM in the export path, ever. Zero tokens, deterministic output.
7. **Adapters are the only platform-specific code.** Everything downstream sees one canonical shape.

---

## 1. Concept model

```
  RAW exports (immutable, on disk, many formats)
        │
        ▼   adapters/*.py   ── platform-specific, ~100–250 LOC each
  Canonical Intermediate Representation (CIR)
        │   one normalised dict per conversation
        ▼   loader.py
  SQLite  ◄── SYSTEM OF RECORD ── tags, annotations, exclusions, FTS5
        │
        ├──► export.py + Jinja2 ──► Markdown / JSON / CSV  (no LLM, no tokens)
        ├──► MCP server ──────────► any AI assistant queries it directly
        └──► graph_push.py ───────► Neo4j (derived, disposable, re-seedable)
                                        │
                                        └─► + Obsidian vault ingest = Second Brain layer
```

**Why both databases, precisely (the fundamentals you asked for):**

| | SQLite | Neo4j |
|---|---|---|
| Core operation | Set operations over indexed rows | Index-free adjacency — each node holds physical pointers to its relationships |
| Cost model | Query cost ∝ rows scanned; B-tree index makes filters O(log n) | Traversal cost ∝ size of the *neighbourhood touched*, independent of total DB size |
| Wins at | Filter, aggregate, count, full-text, "all X where Y between dates" | Variable-depth traversal: "what connects A to D through unknown intermediaries, 2–5 hops" |
| Loses at | 5-hop joins (self-joins with exploding intermediate result sets) | Simple counting and text search — no advantage, more ceremony |

Your **chat archive** queries are 0–1 hops ("count tool calls by model in period"). That is textbook SQL; Neo4j offers zero speed benefit there.

Your **second brain** queries are 2–5 hops ("what other threads touch concepts that this thread touches, and which of my notes are about them"). That is textbook graph; SQL would need recursive CTEs that get ugly fast.

So they are **not competing options** — they are two layers of one system. SQLite stores *facts*. Neo4j stores *meaning*. Because the graph is a projection of SQLite, the graph loader is ~150 LOC and can be dropped and rebuilt at will. Building both in parallel costs almost nothing extra.

---

## 2. Environment & prerequisites

### 2.1 Software to install

| Tool | Purpose | Install |
|---|---|---|
| Python 3.12+ | Everything | `brew install python@3.12` |
| `uv` | venv + dependency manager (fast, reproducible) | `brew install uv` |
| SQLite 3.45+ CLI | Manual inspection | `brew install sqlite` (macOS system sqlite works too; brew's is newer) |
| Neo4j | Graph layer | Neo4j Desktop **or** Docker Compose — see §2.3 |
| DB Browser for SQLite | GUI browsing | `brew install --cask db-browser-for-sqlite` |
| *(optional)* TablePlus / DataGrip | Nicer multi-DB GUI, speaks both SQLite and Neo4j | cask / JetBrains |

### 2.2 Python virtual environment

Use `uv`. Project root gets a `pyproject.toml`; the venv lives at `.venv/`.

```toml
[project]
name = "chatvault"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "typer>=0.12",          # CLI
  "rich>=13",             # terminal output, progress bars
  "jinja2>=3.1",          # markdown templating
  "orjson>=3.10",         # fast JSON parsing (2000+ chats)
  "python-dateutil>=2.9", # timestamp normalisation across platforms
  "neo4j>=5.23",          # Bolt driver
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-cov"]
semantic = ["sqlite-vec>=0.1", "sentence-transformers>=3"]  # Phase 7 only

[project.scripts]
chatvault = "chatvault.cli:app"
```

Setup:
```bash
uv venv && uv pip install -e ".[dev]"
```

**Preflight check the implementer must add** (`chatvault doctor`):
- `sqlite3.sqlite_version >= 3.35`
- FTS5 available: `conn.execute("pragma compile_options")` contains `ENABLE_FTS5`, else fail loudly with instructions
- Neo4j reachable on Bolt (warn only, not fatal)

### 2.3 Neo4j hosting — both, and it doesn't matter

The loader talks **Bolt on `localhost:7687`** using credentials from environment variables. It has no idea what's hosting the server. So:

- **Neo4j Desktop** — recommended for daily use. No Docker daemon, no runaway containers, GUI for browsing. Create a local DBMS, set a password, start it.
- **Docker Compose** — a `docker-compose.yml` is checked into the repo for reproducibility and for wiping/re-seeding cleanly.

Both work with zero code change. Your runaway-process concern with Docker is real but bounded: always stop with `docker compose down` (not just closing Docker Desktop), and the compose file below sets memory caps.

```yaml
# docker-compose.yml
services:
  neo4j:
    image: neo4j:5.26-community
    container_name: chatvault-neo4j
    ports: ["7474:7474", "7687:7687"]
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD}
      NEO4J_server_memory_heap_max__size: 2G
      NEO4J_server_memory_pagecache_size: 1G
      NEO4J_PLUGINS: '["apoc"]'
    volumes:
      - ./neo4j/data:/data
      - ./neo4j/logs:/logs
    restart: "no"          # never auto-restart; you control lifecycle
```

`.env` (gitignored):
```
CHATVAULT_ROOT=/home/user/ChatVault
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=change-me
```

### 2.4 Disk layout & access required

The implementing assistant needs **read** access to wherever exports currently live, and **read/write** to the vault root.

```
~/ChatVault/                          # CHATVAULT_ROOT — the only writable location
├── raw/                              # immutable. write-once.
│   ├── claude-web/2026-07-30/conversations.json
│   ├── claude-code/2026-07-30/<project>/*.jsonl
│   ├── chatgpt/2026-07-30/conversations.json
│   ├── copilot/...
│   └── _manifest.jsonl               # append-only log: path, sha256, bytes, seen_at
├── db/
│   ├── chatvault.sqlite              # system of record
│   └── chatvault.sqlite.bak-<date>   # pre-migration snapshots
├── exports/                          # generated markdown — disposable, gitignored
├── neo4j/                            # docker volumes (if using Docker)
└── src/chatvault/                    # the code
```

**Source locations to harvest (inventory step will confirm):**
- Claude Code: `~/.claude/projects/**/*.jsonl`
- Claude.ai: manual data export → `conversations.json`
- ChatGPT: manual data export → `conversations.json` (+ `user.json`, `message_feedback.json`)
- VS Code / Copilot: `~/Library/Application Support/Code/User/workspaceStorage/*/state.vscdb` (SQLite key-value; chat blobs under `ItemTable` keys matching `chat%` / `interactive.sessions%`)
- Cursor: `~/Library/Application Support/Cursor/User/workspaceStorage/*/state.vscdb` (same shape)
- CLI agents (agy, copilot-cli, etc.): usually JSONL under `~/.<tool>/` — handled by the generic adapter

**RAW retention policy:** keep originals, but compress after ingest (`tar --zstd`). They're small relative to their value and you never have to look at them again. This is cheap insurance, not a folder you maintain.

---

## 3. Canonical Intermediate Representation (CIR)

Every adapter's `parse(path) -> Iterator[dict]` yields conversations in exactly this shape. This is the contract that makes new platforms cheap to add.

```python
{
  "platform": "chatgpt",              # required, lowercase slug
  "native_id": "6f2a...",             # platform's own conversation id, or None
  "title": "Neo4j schema design",
  "project": "second-brain",          # workspace/project/folder if the platform has one
  "started_at": "2026-03-01T10:04:00Z",   # ISO 8601 UTC
  "ended_at":   "2026-03-01T11:12:00Z",
  "source_path": "/abs/path/to/file",
  "raw": {...},                        # the untouched source object for this conversation
  "messages": [
    {
      "idx": 0,                        # 0-based, post-linearisation
      "role": "user",                  # user | assistant | system | tool
      "model": "gpt-4o",               # None for user messages
      "created_at": "2026-03-01T10:04:00Z",
      "raw": {...},
      "blocks": [
        {
          "idx": 0,
          "type": "text",              # see enum below
          "tool_name": None,
          "text": "How do I ...",      # ALWAYS a string; "" if non-textual
          "raw": {...}
        }
      ]
    }
  ]
}
```

**`block.type` enum (closed set — adapters must map into it):**
`text` · `thinking` · `tool_use` · `tool_result` · `code` · `image` · `attachment` · `system` · `error` · `unknown`

`unknown` is legal and expected — it keeps `raw` intact so nothing is lost when a platform ships a new content type. `chatvault stats --unknown` reports them so adapters can be improved incrementally.

### 3.1 Adapter difficulty — honest estimates

| Adapter | Source shape | Effort | Notes |
|---|---|---|---|
| `claude_code` | JSONL, one event per line | **Easy**, ~120 LOC | Already atomic. Group by `sessionId`; `type` field maps almost 1:1 to block types. Sidechain/subagent entries need a `parent_uuid` walk. |
| `claude_web` | `conversations.json` array | **Easy**, ~100 LOC | `chat_messages[].content[]` is already a block list. |
| `chatgpt` | `conversations.json`, `mapping` tree | **Medium**, ~250 LOC | **This is the hard one.** `mapping` is a dict of nodes with `parent`/`children`. You must walk from the root to `current_node` to linearise the *active* branch. Handle: `author.role` ∈ user/assistant/system/tool; `content.content_type` ∈ text/code/execution_output/multimodal_text/thoughts; `parts` may contain dicts not strings; null-content nodes must be skipped. Store abandoned branches as `blocks` on a synthetic message with `type='unknown'` and `excluded=1` — lossless but out of the way. |
| `vscode_state` | `state.vscdb` SQLite → JSON blobs | **Hard**, ~200 LOC | Schema is undocumented and version-drifts. Read `ItemTable`, filter keys, `json.loads` values, probe structure. Write it defensively and expect to revise. **Phase 3, not Phase 1.** |
| `generic_jsonl` | Any JSONL with role/content-ish keys | **Easy**, ~80 LOC | Heuristic fallback for CLI tools. Best-effort key detection (`role`/`author`, `content`/`text`/`message`), everything else into `raw`. Ensures a new CLI tool is never a blocker. |

**Total realistic effort: 2–3 focused days** for Phases 0–4 by a competent coding model, excluding `vscode_state`.

---

## 4. SQLite schema (authoritative DDL)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- ─────────────── provenance ───────────────
CREATE TABLE source_files (
  id           INTEGER PRIMARY KEY,
  path         TEXT    NOT NULL,
  sha256       TEXT    NOT NULL UNIQUE,   -- file-level dedupe: skip identical re-exports instantly
  platform     TEXT    NOT NULL,
  adapter      TEXT    NOT NULL,
  bytes        INTEGER,
  ingested_at  TEXT    NOT NULL,
  status       TEXT    NOT NULL DEFAULT 'ok',   -- ok | partial | failed
  error        TEXT
);

-- ─────────────── conversations ───────────────
CREATE TABLE conversations (
  id             INTEGER PRIMARY KEY,
  uid            TEXT    NOT NULL UNIQUE,  -- '<platform>:<native_id>' or '<platform>:h:<content_hash>'
  platform       TEXT    NOT NULL,
  native_id      TEXT,
  title          TEXT,
  project        TEXT,
  model_primary  TEXT,                     -- most frequent model across assistant messages
  started_at     TEXT,
  ended_at       TEXT,
  message_count  INTEGER NOT NULL DEFAULT 0,
  block_count    INTEGER NOT NULL DEFAULT 0,
  char_count     INTEGER NOT NULL DEFAULT 0,
  head_hash      TEXT,                     -- hash of first 3 message content_hashes → continuation detection
  content_hash   TEXT,                     -- hash of ALL ordered message content_hashes → exact-dupe detection
  source_file_id INTEGER REFERENCES source_files(id),
  superseded_by  INTEGER REFERENCES conversations(id),  -- set when a longer version of same thread exists
  excluded       INTEGER NOT NULL DEFAULT 0,
  starred        INTEGER NOT NULL DEFAULT 0,
  raw_json       TEXT,
  ingested_at    TEXT    NOT NULL
);
CREATE INDEX ix_conv_platform   ON conversations(platform);
CREATE INDEX ix_conv_started    ON conversations(started_at);
CREATE INDEX ix_conv_model      ON conversations(model_primary);
CREATE INDEX ix_conv_project    ON conversations(project);
CREATE INDEX ix_conv_head_hash  ON conversations(head_hash);
CREATE INDEX ix_conv_content_h  ON conversations(content_hash);

-- ─────────────── messages ───────────────
CREATE TABLE messages (
  id            INTEGER PRIMARY KEY,
  conv_id       INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  idx           INTEGER NOT NULL,
  role          TEXT    NOT NULL,          -- user | assistant | system | tool
  model         TEXT,
  created_at    TEXT,
  content_hash  TEXT    NOT NULL,          -- sha256(role + '\x1f' + concat of block hashes)
  char_count    INTEGER NOT NULL DEFAULT 0,
  excluded      INTEGER NOT NULL DEFAULT 0,
  raw_json      TEXT,
  UNIQUE(conv_id, idx)
);
CREATE INDEX ix_msg_conv  ON messages(conv_id);
CREATE INDEX ix_msg_model ON messages(model);
CREATE INDEX ix_msg_role  ON messages(role);
CREATE INDEX ix_msg_hash  ON messages(content_hash);

-- ─────────────── blocks — the atomic unit ───────────────
CREATE TABLE blocks (
  id            INTEGER PRIMARY KEY,
  msg_id        INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  conv_id       INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, -- denormalised for fast filtering
  idx           INTEGER NOT NULL,
  type          TEXT    NOT NULL,
  tool_name     TEXT,
  text          TEXT    NOT NULL DEFAULT '',
  char_count    INTEGER NOT NULL DEFAULT 0,
  content_hash  TEXT    NOT NULL,
  excluded      INTEGER NOT NULL DEFAULT 0,
  raw_json      TEXT,
  UNIQUE(msg_id, idx)
);
CREATE INDEX ix_blk_msg   ON blocks(msg_id);
CREATE INDEX ix_blk_conv  ON blocks(conv_id);
CREATE INDEX ix_blk_type  ON blocks(type);
CREATE INDEX ix_blk_tool  ON blocks(tool_name);
CREATE INDEX ix_blk_hash  ON blocks(content_hash);

-- ─────────────── your layer: tags, notes, exclusion reasons ───────────────
CREATE TABLE tags (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,   -- hierarchical by convention: 'project/second-brain', 'noise/boilerplate'
  colour     TEXT,
  created_at TEXT NOT NULL
);

-- polymorphic: tag a whole conversation, one message, or a single tool call
CREATE TABLE tag_links (
  tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  target_type TEXT    NOT NULL,      -- 'conversation' | 'message' | 'block'
  target_id   INTEGER NOT NULL,
  source      TEXT    NOT NULL DEFAULT 'manual',  -- manual | rule | llm
  created_at  TEXT    NOT NULL,
  PRIMARY KEY (tag_id, target_type, target_id)
);
CREATE INDEX ix_taglink_target ON tag_links(target_type, target_id);

CREATE TABLE annotations (
  id          INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id   INTEGER NOT NULL,
  note        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT
);
CREATE INDEX ix_anno_target ON annotations(target_type, target_id);

-- every exclusion is logged and reversible
CREATE TABLE exclusion_log (
  id          INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id   INTEGER NOT NULL,
  action      TEXT NOT NULL,          -- exclude | restore
  reason      TEXT,
  rule        TEXT,                   -- the SQL predicate used, if bulk
  created_at  TEXT NOT NULL
);

-- named, re-runnable queries — your "global filter system"
CREATE TABLE saved_views (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT,
  sql         TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

-- ─────────────── full-text search ───────────────
CREATE VIRTUAL TABLE blocks_fts USING fts5(
  text,
  content='blocks',
  content_rowid='id',
  tokenize='porter unicode61'
);

CREATE TRIGGER blocks_ai AFTER INSERT ON blocks BEGIN
  INSERT INTO blocks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER blocks_ad AFTER DELETE ON blocks BEGIN
  INSERT INTO blocks_fts(blocks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER blocks_au AFTER UPDATE OF text ON blocks BEGIN
  INSERT INTO blocks_fts(blocks_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO blocks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ─────────────── read views (ALWAYS query these, not base tables) ───────────────
CREATE VIEW v_conversations AS
  SELECT * FROM conversations WHERE excluded = 0 AND superseded_by IS NULL;

CREATE VIEW v_messages AS
  SELECT m.* FROM messages m
  JOIN v_conversations c ON c.id = m.conv_id
  WHERE m.excluded = 0;

CREATE VIEW v_blocks AS
  SELECT b.* FROM blocks b
  JOIN v_messages m ON m.id = b.msg_id
  WHERE b.excluded = 0;

CREATE VIEW v_schema_version AS SELECT 1 AS version;
```

### 4.1 Migrations

Use a plain numbered-file migrator (`migrations/001_init.sql`, `002_....sql`) plus a `schema_migrations(version, applied_at)` table. No Alembic — overkill here. `chatvault migrate` backs up the DB file first.

---

## 5. Deduplication (your explicit fear — solved at three levels)

You want to dump collections in without checking what's already there. This is designed for exactly that.

**Level 1 — file identity.** `source_files.sha256` UNIQUE. Re-ingesting a byte-identical export is a no-op that costs one hash. Fast path.

**Level 2 — conversation identity.** `conversations.uid` UNIQUE, built as `platform:native_id` when the platform gives an ID, else `platform:h:<content_hash>`. Re-importing the same conversation from a different file → UPSERT, not INSERT.

**Level 3 — continuation / superset detection.** *This is the one that actually bites you.* You export ChatGPT in March (a thread has 10 messages), export again in July (same thread now has 25). Same `native_id`, different content. Rule:

```
on conflict(uid):
    if new.message_count > existing.message_count and new.head_hash == existing.head_hash:
        insert new version; set existing.superseded_by = new.id
    elif new.content_hash == existing.content_hash:
        no-op (touch last_seen only)
    else:
        insert as new conversation, flag for review in `chatvault dedupe --report`
```

`head_hash` (hash of the first 3 message hashes) also catches the case where a platform reissues IDs — two conversations with the same opening are the same thread.

**Level 4 — near-duplicate blocks.** `blocks.content_hash` is indexed, so `SELECT content_hash, COUNT(*) FROM blocks GROUP BY 1 HAVING COUNT(*) > 20` instantly surfaces boilerplate — repeated system prompts, identical tool preambles, obsolete ID tags. One `UPDATE ... SET excluded=1 WHERE content_hash = ?` removes it from every conversation at once. **This is your "if the JSON contains recurring obsolete info, remove it everywhere" requirement**, and it's a two-line operation.

`chatvault dedupe --report` prints all four levels; `--apply` acts on levels 1–3 only (4 is always a deliberate choice).

---

## 6. Markdown export — no LLM, no tokens

Confirmed: **yes, you get markdown straight out of the DB, free and instant.** A `SELECT` feeds a Jinja2 template. Nothing else.

```bash
chatvault export --uid 'chatgpt:6f2a...' --template default
chatvault export --where "platform='claude-code' AND started_at > '2026-06-01'" --split-by conversation --out ~/Obsidian/Vault/Chats/
chatvault export --view 'my-neo4j-threads' --template obsidian --front-matter
```

Templates live in `templates/` — plain `.md.j2` files you can edit yourself without touching Python:

| Template | Output |
|---|---|
| `default` | Clean prose: user/assistant only, tool calls collapsed to one line |
| `full` | Everything including thinking blocks and full tool payloads |
| `obsidian` | YAML front-matter (`platform`, `model`, `date`, `tags`, `conv_uid`) + wikilinks — drops straight into your vault |
| `digest` | One line per conversation — an index file |

Filenames: `{started_at:%Y-%m-%d}--{platform}--{slug(title)}--{uid_short}.md`. Deterministic, sortable, no collisions, no scattered folder.

Exports are written to `exports/` and are **disposable by definition** — regenerate any time, never manage them as assets.

---

## 7. Neo4j projection layer

**Direction of truth: SQLite → Neo4j, one way, always.** `chatvault graph rebuild` drops and re-seeds. If the graph gets messy from experimentation, you lose nothing.

### 7.1 Graph model

```
Nodes
  (:Conversation {uid, title, platform, project, started_at, message_count})
  (:Message      {uid, idx, role, created_at})          -- only at --depth full
  (:Block        {uid, type, tool_name})                 -- only at --depth full
  (:Model        {name})
  (:Platform     {name})
  (:Project      {name})
  (:Tool         {name})
  (:Tag          {name})
  (:Concept      {name})        -- Phase 7: extracted topics/entities
  (:Note         {path, title}) -- Phase 7: Obsidian vault

Relationships
  (:Conversation)-[:ON_PLATFORM]->(:Platform)
  (:Conversation)-[:IN_PROJECT]->(:Project)
  (:Conversation)-[:USED_MODEL {message_count}]->(:Model)
  (:Conversation)-[:USED_TOOL  {call_count}]->(:Tool)
  (:Conversation)-[:HAS_MESSAGE]->(:Message)-[:NEXT]->(:Message)
  (:Message)-[:HAS_BLOCK]->(:Block)
  (:Conversation|:Message|:Block)-[:TAGGED]->(:Tag)
  (:Conversation)-[:MENTIONS {score}]->(:Concept)
  (:Note)-[:LINKS_TO]->(:Note)
  (:Note)-[:ABOUT]->(:Concept)
```

Constraints (create before loading):
```cypher
CREATE CONSTRAINT conv_uid IF NOT EXISTS FOR (c:Conversation) REQUIRE c.uid IS UNIQUE;
CREATE CONSTRAINT model_name IF NOT EXISTS FOR (m:Model) REQUIRE m.name IS UNIQUE;
CREATE CONSTRAINT tool_name IF NOT EXISTS FOR (t:Tool) REQUIRE t.name IS UNIQUE;
CREATE CONSTRAINT tag_name IF NOT EXISTS FOR (t:Tag) REQUIRE t.name IS UNIQUE;
CREATE CONSTRAINT concept_name IF NOT EXISTS FOR (c:Concept) REQUIRE c.name IS UNIQUE;
CREATE CONSTRAINT note_path IF NOT EXISTS FOR (n:Note) REQUIRE n.path IS UNIQUE;
```

### 7.2 Load depths

- `--depth meta` **(default)** — conversations + models + tools + tags + projects + concepts. ~5k conversations → ~8k nodes. Loads in seconds. This is where every interesting graph question lives.
- `--depth full` — adds messages and blocks. 5k × 40 × 2 ≈ 400k nodes. Neo4j handles it fine; use when you want message-level traversal.

Load via batched `UNWIND $rows AS row MERGE ...` in transactions of 5,000. `MERGE` on the unique key makes graph loading idempotent too.

### 7.3 Why this is worth doing (concrete queries SQL can't do gracefully)

```cypher
// Threads that share concepts with a given thread, ranked — "what else have I thought about this"
MATCH (c:Conversation {uid:$uid})-[:MENTIONS]->(k:Concept)<-[:MENTIONS]-(other:Conversation)
RETURN other.title, collect(k.name) AS shared, count(*) AS strength
ORDER BY strength DESC LIMIT 20;

// Bridge concepts: ideas linking two projects I thought were unrelated
MATCH (a:Project {name:'chat-archive'})<-[:IN_PROJECT]-()-[:MENTIONS]->(k:Concept)
      <-[:MENTIONS]-()-[:IN_PROJECT]->(b:Project {name:'second-brain'})
RETURN k.name, count(*) AS bridges ORDER BY bridges DESC;

// Second brain: my notes about concepts I've discussed with AI but never written up
MATCH (c:Conversation)-[:MENTIONS]->(k:Concept)
WHERE NOT (k)<-[:ABOUT]-(:Note)
RETURN k.name, count(c) AS discussed ORDER BY discussed DESC LIMIT 50;
```

The last one is the second brain's actual payload — surfacing the gap between what you've *thought* and what you've *recorded*.

---

## 8. CLI surface

```
chatvault doctor                       # env preflight: sqlite version, FTS5, neo4j reachability
chatvault init                         # create vault dirs + run migrations

chatvault inventory <dir> [--deep]     # scan, detect platform, count, size, sample — NO writes
chatvault ingest <path> [--adapter auto] [--dry-run]
chatvault verify                       # row counts, orphans, FTS integrity, hash spot-checks

chatvault stats [--by model|platform|project|month|tool] [--json]
chatvault search "<fts query>" [--platform X] [--type tool_use] [--limit N]
chatvault show <uid> [--template full]

chatvault tag add "<name>" --where "<sql predicate>" --target conversation|message|block
chatvault tag list
chatvault note <target_type> <id> "<text>"
chatvault exclude --where "<predicate>" --target block --reason "gpt-5-mini noise"
chatvault restore --where "<predicate>" --target block
chatvault view save "<name>" --sql "<sql>" ; chatvault view run "<name>"

chatvault dedupe --report | --apply

chatvault export [--uid|--where|--view] [--template T] [--out DIR] [--split-by conversation]

chatvault graph rebuild [--depth meta|full]
chatvault graph push    [--since <date>]
chatvault graph query "<cypher>"
```

Your `gpt-5-mini` cleanup, end to end:

```bash
chatvault stats --by model | grep mini              # see what's there
chatvault exclude --target message \
  --where "model LIKE 'gpt-5-mini%' AND created_at < '2026-01-01'" \
  --reason "obsolete model, low-prio work"          # reversible, logged
chatvault search "important" --platform copilot     # content still searchable if you want it back
chatvault restore --where "..."                     # undo any time
```

Nothing was deleted. Nothing was sent to an LLM. Nothing broke.

---

## 9. Build phases & acceptance criteria

| Phase | Deliverable | Done when |
|---|---|---|
| **0** | Repo, `uv` env, `doctor`, `init`, migration runner, `inventory` | `chatvault inventory ~/exports` prints an accurate table of files, detected platforms, and conversation counts. Zero writes. |
| **1** | CIR + `claude_code` + `claude_web` adapters + loader + hashing + dedupe L1–L3 | Ingest twice → `SELECT COUNT(*)` identical both times. `verify` clean. |
| **2** | Export engine + 4 templates | `chatvault export --where "1=1" --split-by conversation` produces well-formed markdown for every conversation, no LLM calls, deterministic filenames. |
| **3** | `chatgpt` adapter (tree linearisation) + `generic_jsonl` fallback | 2000+ ChatGPT conversations ingest with <1% `unknown` blocks; branch handling documented and tested. |
| **4** | Tags, annotations, exclusion + log, saved views, FTS search CLI | Bulk exclude by predicate, then restore, leaves DB byte-equivalent in the affected columns. |
| **5** | Neo4j: constraints, `graph rebuild --depth meta`, 5 canned Cypher queries | Drop DB → rebuild → identical node/rel counts. Works against both Desktop and Docker with only env var changes. |
| **6** | MCP wiring: SQLite MCP + Neo4j Cypher MCP registered so any AI can query directly | You can ask an assistant "how many tool calls did I make in June by model" and it answers from the DB. |
| **7** *(optional)* | `vscode_state` adapter; concept extraction; Obsidian vault ingest; `sqlite-vec` semantic search | Second brain graph queries in §7.3 return meaningful results. |

**Stop line:** Phases 0–4 deliver ~90% of the daily value. Do not start Phase 7 until you've lived with 0–6 for a few weeks — let real friction pick what comes next rather than speculation.

---

## 10. Testing requirements

- `tests/fixtures/` holds one small anonymised sample per platform (3–5 conversations each).
- Golden-file tests for each adapter: fixture → CIR JSON snapshot.
- **Idempotency test (mandatory):** ingest fixture dir twice, assert identical row counts across all tables.
- **Continuation test:** ingest a 5-message conversation, then a 12-message superset with the same `native_id`; assert the short one gets `superseded_by` set and `v_conversations` returns exactly one row.
- Export golden-file test per template.
- Graph test: rebuild twice, assert identical counts.

---

## 11. Open decisions for the owner (not blockers)

1. **Vault root** — spec assumes `~/ChatVault`. Change `CHATVAULT_ROOT` if you'd rather it live in iCloud/Documents. Caution: SQLite over cloud sync is safe single-machine-at-a-time, corrupting under concurrent writes. Recommended: DB local, `raw/` synced or in Git LFS.
2. **Git** — version the code and `templates/`; gitignore `db/`, `exports/`, `neo4j/`, `.env`. Optionally track `raw/_manifest.jsonl` so you have a history of what was ever seen.
3. **Concept extraction method (Phase 7)** — LLM-based (better quality, token cost, one-time per conversation) vs. KeyBERT/YAKE local (free, noisier). Recommend: local first, LLM pass only on starred conversations.
4. **Timezone** — spec stores UTC ISO-8601 everywhere; display converts to local. Don't deviate.

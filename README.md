# ChatVault

[![Tests](https://github.com/martenzi/ChatVault/actions/workflows/tests.yml/badge.svg)](https://github.com/martenzi/ChatVault/actions/workflows/tests.yml)

A unified, vendor-independent vault for AI conversation history.

ChatVault ingests chat transcripts from every major AI coding assistant and
chat product into one canonical SQLite store, then layers search, curation,
graph, and a personal "second brain" on top. The motivating problem: editor
upgrades silently destroy chat history, and conversations end up scattered
across a dozen platforms with no shared schema, no search, and no provenance.
ChatVault treats the raw exports as an immutable source of truth and makes
everything else — the database, the markdown renders, the Neo4j graph, the
embeddings — a disposable, rebuildable projection.

```
RAW exports (write-once)  →  SQLite (system of record)  →  derived renders
   immutable source              conversations·messages·blocks      markdown · Neo4j · embeddings
```

---

## Why it's interesting

- **18 ingestion adapters, one canonical shape.** ChatGPT (web + legacy
  recovered), Claude Code, Claude Desktop, Cursor (current + legacy + Copilot),
  VS Code, Windsurf/Cascade (encrypted `.pb` protobuf, with its own AES-256-GCM
  decryptor + protobuf-wire parser), Antigravity, DeepSeek, Grok, Gemini Takeout,
  Devin CLI, Devin Desktop ACP, Hermes, OpenClaw, and a generic JSONL fallback.
  Each adapter normalizes to the same `conversations → messages → blocks` schema.
- **Blocks are the atomic unit.** A block is a single text / tool_use /
  tool_result / thinking element. Making individual tool calls addressable is
  what unlocks block-level dedupe, tool statistics, and the `lean` export profile
  that strips tool noise for reading.
- **Four-level dedupe** — file hash → conversation uid → continuation chain →
  block boilerplate — so re-ingesting the same export is idempotent and safe.
- **Never delete; filter at the view layer.** `excluded`, `superseded_by`, and
  `orphaned` flags hide rows through `v_*` views. Every table gets a view; a
  convention without a mechanism decays.
- **Two search modes that answer different questions.** FTS5 for exact strings
  (error messages, filenames); `sqlite-vec` + local Ollama embeddings
  (`nomic-embed-text`) for meaning — the conversation you can't name.
- **Tags, not folders.** Hierarchical tags with unlimited axes, because a folder
  forces one item into one location and a tag lets it live in many. Folders come
  back as disposable *output* (`export --tree`).
- **A second-brain layer.** `chatvault stash` / `brief` capture context for a
  fresh AI thread; the Brain DB (`brain <verb> <noun>` grammar) is a curated,
  embeddable personal knowledge base with its own schema and migrations.
- **Specs carry acceptance criteria.** Every design spec ends with testable
  conditions — three genuine design flaws surfaced only because someone tried
  to write the test.

The design rationale for all of the above lives in [`docs/`](docs/) — start with
[`docs/02_DECISIONS.md`](docs/02_DECISIONS.md) (14 decisions, each with the
rejected alternative and the reason).

---

## Architecture

```
src/chatvault/
├── adapters/          18 source adapters → one canonical shape (CIR)
├── brain/             Second Brain: entries, collections, embeddings, graph
├── ingest.py          idempotent load + four-level dedupe
├── db.py              SQLite connection, migration runner, sqlite-vec load
├── search.py          FTS5 keyword search
├── embeddings.py      sqlite-vec semantic search (Ollama over HTTP)
├── tags.py            hierarchical tag tree + rules
├── projects.py        project identity (label ≠ tag) + backfill
├── curation.py        tags, annotations, exclusion tiers, export/import
├── export.py          markdown render profiles (full / digest / lean / obsidian)
├── graph.py           one-way Neo4j projection (disposable)
├── reconcile.py       presence tracking — detects vanished sources
├── ledger.py          ingestion ledger / manifest
├── audit.py           audit-shapes: enumerate a source before writing an adapter
├── screen.py          Textual TUI for tagging
├── brain_cli.py       brain <verb> <noun> command grammar
├── cli.py             Typer CLI entry point
└── ...
```

**The rule the adapters follow:** *enumerate the source's real shape before
writing any adapter code.* `chatvault audit-shapes` produces a field-path
investigation of a platform's storage (verified by direct byte inspection where
there is no schema) so the adapter is built against measured evidence, not
guesses. See `docs/50_SPEC_ingestion_fidelity_v1.md`.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| System of record | SQLite | 0–1 hop queries; graph DBs earn their cost at 2+ hops |
| Semantic search | `sqlite-vec` + Ollama `nomic-embed-text` | local, no torch/onnxruntime wheel needed on this platform |
| Graph projection | Neo4j 5 (disposable) | tag co-occurrence / taxonomy discovery; one-way from SQLite |
| Browse surface | Datasette | faceting, URL-as-state, export — better than worth rebuilding |
| Tagging surface | Textual TUI | Datasette is read-only by design; no off-the-shelf bulk tagger |
| CLI | Typer + Rich | |
| Templates | Jinja2 | markdown export profiles |
| Packaging | `uv` + hatchling | Python ≥ 3.12 |

---

## Getting started

```bash
# install
uv sync

# configure (copy and edit)
cp .env.example .env      # set CHATVAULT_ROOT and Neo4j creds

# optional: Neo4j graph projection
docker compose up -d neo4j

# initialize the SQLite schema
chatvault init

# ingest a source directory (idempotent — safe to re-run)
chatvault ingest /path/to/exports

# browse / search
chatvault browse
chatvault search "docker AND npm"
chatvault search --semantic "the npm problem that never says npm"

# tag, then export
chatvault tag add <conv-id> troubleshooting/docker
chatvault export --profile lean --out ./out
```

Semantic search needs a local Ollama server running
`nomic-embed-text` on port 11434 — see the Semantic Search section of the
project docs for setup. Neo4j is optional and off by default.

---

## Testing

```bash
uv run pytest
```

The suite covers every adapter (shape enumeration, ingestion, dedupe), the
Brain layer, search, tags, projects, curation, export profiles, and the
Textual screens — including regression tests for bugs found against real
production data rather than fixtures.

---

## Project layout

```
.
├── src/chatvault/        the package
├── tests/                pytest suite + per-adapter fixtures
├── migrations/           numbered SQL migrations (+ brain/ sub-schema)
├── scripts/              standalone extraction helpers for locked sources
├── docs/                  design specs, concepts, and decisions
├── templates/            markdown export profiles (Jinja2)
├── static/                Datasette dark theme
├── docker-compose.yml    Neo4j
└── pyproject.toml
```

---

## Design principles (the short version)

1. **RAW is immutable; everything else is derived.** You can be reckless
   downstream because upstream is safe.
2. **Never delete; filter instead.** Cleanup is an opinion, and opinions should
   be reversible.
3. **Flag, don't guess.** Where identity or quality is ambiguous, mark it
   `review/*` and let the human decide.
4. **Measure, don't derive.** Query the database before making a claim about the
   database.
5. **Search is for discovery, tags are for memory.** When a search takes real
   effort, tag the result so it never takes effort again.

See [`docs/02_DECISIONS.md`](docs/02_DECISIONS.md) for the full set.

# ChatVault

[![Tests](https://github.com/martenzi/ChatVault/actions/workflows/tests.yml/badge.svg)](https://github.com/martenzi/ChatVault/actions/workflows/tests.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)

One searchable archive for AI chat history that is otherwise scattered across a
dozen tools, formats, and storage mechanisms.

```
RAW exports (write-once)  →  SQLite (system of record)  →  derived renders
   immutable source            conversations·messages·blocks     markdown · Neo4j · embeddings
```

---

## Why this exists

Every AI product stores conversations its own way — a `mapping` tree in one
export, a flat `messages` array in another, Protocol Buffers inside a SQLite
file in a third, encrypted `.pb` blobs in a fourth. Editor upgrades have
silently dropped old chat history more than once, and nothing reads across
these formats.

I wanted the *complete* record — not just the messages a UI chooses to show,
but tool calls, injected system context, editor-internal events, server-side
messages to the model, and failure responses. Getting that right means reading
the raw export in full rather than assuming a schema: when a conversation
looked "strange" — tool calls with no visible prompt, reasoning filed under
the wrong role — I couldn't tell whether the source was odd or my parser was
wrong. So the method became: enumerate the entire raw file first, classify
every element it actually contains, and only then write the adapter.

The first pass rendered everything to Markdown. Markdown is great to read and
a poor thing to query — so the archive became a database, and the database
became this project.

## Supported sources

18 adapters normalise everything below into one schema — each one built and
regression-tested against real exports, not documentation.

| Source | What ChatVault reads | Adapter |
|---|---|---|
| ChatGPT | Web export JSON (`mapping` tree, branching history) | `chatgpt.py` |
| ChatGPT (legacy) | Recovered legacy export JSON | `chatgpt_legacy_recovered.py` |
| Claude (web/desktop) | Account export JSON, incl. Projects | `claude_desktop.py` |
| Claude Code | `~/.claude/projects` JSONL session logs, incl. subagent transcripts | `claude_code.py` |
| Cursor | Composer storage (SQLite), current format | `cursor.py` |
| Cursor (legacy) | Legacy composer JSONL | `cursor_legacy.py` |
| Cursor Copilot (legacy) | Copilot-era session JSONL | `cursor_legacy_copilot.py` |
| VS Code Copilot Chat | Session JSONL | `vscode_legacy.py` |
| Windsurf / Cascade | Encrypted `.pb` protobuf — own AES-256-GCM decryptor + wire-format parser | `windsurf_legacy.py` |
| Antigravity | Protocol Buffers inside SQLite | `antigravity.py` |
| Gemini | Google Takeout activity records | `gemini_takeout.py` |
| Grok | xAI privacy-request export JSON | `grok.py` |
| DeepSeek | Official export JSON | `deepseek.py` |
| Devin CLI | Session JSONL | `devin_cli.py` |
| Devin Desktop | ACP protocol stream | `devin_desktop_acp.py` |
| Hermes | Session JSONL (two on-disk shapes) | `hermes.py` |
| OpenClaw | Session JSONL | `openclaw.py` |
| Anything else | Generic JSONL fallback | `generic_jsonl.py` |

Don't see yours? `chatvault audit-shapes <dir>` enumerates every key path in
an unknown export — that's the starting point for a new adapter, and a useful
inspection tool on its own.

## What it does

- **Block-level storage** — the atomic unit is the *block* (a single text /
  thinking / tool-call / tool-result element), not the message. Roles are
  unreliable across providers — reasoning shows up as `assistant` in one
  export and `system` in another — so nothing is keyed on them.
- **Four-level deduplication** — file hash → conversation uid → continuation
  chain → block boilerplate. Re-ingesting the same export is idempotent.
- **Two searches, honestly separated** — `find` is keyword (FTS5), `search` is
  semantic (`sqlite-vec` + local embeddings). No flags, no silent fallback.
- **Hierarchical tagging** — rules auto-apply on ingest; any bulk tagging run
  is reversible with one command.
- **Export profiles** — `read`, `lean`, `full`, plus Obsidian — pick the level
  of detail you need. Results from `find`/`search` feed straight into
  `export --pick`.
- **A Second Brain layer** — promote conversations into a curated, embeddable
  personal knowledge store (`chatvault brain ...`).
- **Soft deletion only** — `excluded` / `superseded_by` / `orphaned` flags and
  `v_*` views; nothing is ever destructively removed.

## Requirements

- Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/) for dependencies
- Everything else is SQLite — no services needed for the core tool
- *Optional:* [Ollama](https://ollama.com) with `nomic-embed-text` for
  semantic search; Neo4j (`docker compose up -d neo4j`) and Datasette as
  disposable browse/graph projections

## Quick start

```bash
uv sync                              # install
cp .env.example .env                 # set CHATVAULT_ROOT
chatvault init                       # create the schema
chatvault ingest /path/to/exports    # point at a folder of exports — safe to re-run

chatvault find "docker"                        # keyword search (FTS5)
chatvault search "why did the build break"     # semantic search (needs Ollama)
chatvault export --pick 1-5 --profile lean --out ./out
```

Point [Datasette](https://datasette.io) at `db/chatvault.sqlite` for a faceted
browse UI, or `chatvault graph *` for the Neo4j projection — both optional.

The repo is data-empty by design: you ingest *your own* exports. No real
conversation data is ever committed — the vault directories are gitignored.

## Design decisions worth reading

The calls that shaped the codebase, kept short — full versions with rejected
alternatives in [`docs/02_DECISIONS.md`](docs/02_DECISIONS.md):

- **RAW exports are immutable; everything else is a projection.** The
  database, renders, and graph can be rebuilt from source at any time. Source
  files are never edited in place.
- **Adapters walk the entire file before anyone writes a parser.**
  `audit-shapes` enumerates every key path and every element shape in a real
  export first; the adapter then classifies what was measured, not what the
  documentation claims exists. Formats are almost always stranger than their
  docs — this is how "weird" conversations turned out to be complete after all.
- **Tiers are decided by block type, never by role.** An earlier draft
  filtered on `messages.role` — which would have made reasoning filed under
  `system` by one provider invisible, while looking like missing data rather
  than a wrong filter.
- **Semantic search runs on local Ollama embeddings, not
  sentence-transformers.** You can point `embeddings.py::embed_text` at any
  backend — it's one function — but be warned: on Intel macOS with a current
  Python there are no installable torch/onnxruntime wheels at all. Ollama over
  HTTP sidesteps the Python ML-wheel problem entirely while staying fully
  offline. `nomic-embed-text` is purpose-built for retrieval.
- **Cosine distance is set explicitly on the vector table.** Verified
  empirically that `nomic-embed-text` output isn't unit-length — the `vec0`
  default of L2 wouldn't track meaning-similarity reliably.
- **Never delete; filter at the view layer.** Cleanup is an opinion, and
  opinions should be reversible.

## Architecture

```
src/chatvault/
├── adapters/          18 source adapters → one canonical shape
├── brain/             Second Brain: entries, collections, embeddings, graph
├── ingest.py          idempotent load + four-level dedupe
├── search.py          FTS5 keyword search
├── embeddings.py      sqlite-vec semantic search (Ollama over HTTP)
├── tags.py            hierarchical tag tree + auto-apply rules
├── curation.py        tags, annotations, exclusion tiers, export/import
├── export.py          markdown render profiles (read / lean / full / obsidian)
├── audit.py           audit-shapes: enumerate a source before writing an adapter
├── graph.py           one-way Neo4j projection (disposable)
├── reconcile.py       presence tracking — detects vanished sources
├── screen.py          Textual TUI for tagging
└── cli.py             Typer CLI entry point
```

The tool is built to be customised — commands are plain Typer functions,
adapters subclass a common base, and export profiles are Jinja2 templates.

## Testing

```bash
uv run pytest
```

500+ tests covering every adapter (shape enumeration, ingestion, dedupe), the
Brain layer, search, tags, curation, and export profiles — including
regression tests for bugs found against real production data rather than
fixtures. CI runs the suite on every push.

## Status and scope

A working personal tool, shared in the open. It runs daily against a corpus of
~3,500 conversations (~210k messages) across the sources in the table above.
Not on PyPI, no multi-user story, no hosted component — issues and ideas are
welcome regardless.

The standalone precursor — a smaller tool that converts AI chat JSON exports
to Markdown — lives separately as
[chat2md](https://github.com/martenzi/chat2md) (AI Chat-to-Markdown converter).
ChatVault grew out of it when "render everything faithfully" turned out to
want a database underneath.

## Documentation

- [`docs/02_DECISIONS.md`](docs/02_DECISIONS.md) — the 14 decisions, each with
  the rejected alternative
- [`docs/00_README.md`](docs/00_README.md) — index of specs, concepts, and
  historical architecture docs

## License

MIT.

# ChatVault — Spec v1.3: Human Browsing & Selective Export

**Problem being fixed:** the original export command required a UID. No human recalls a UID. Humans recall a title, a rough date, and a vague memory of what it was about — the same way you'd find something in the ChatGPT or Claude web UI. This spec makes the CLI match that.

---

## 1. `chatvault find` — the human-facing lister

```
chatvault find ["<query>"] [--platform X] [--since DATE] [--before DATE]
               [--project X] [--tag X] [--has-attachments]
               [--limit 20] [--sort recent|oldest|longest]
```

No query = most recent conversations, newest first. With a query, it's FTS-backed but matched against **titles first, body text second** — you're usually recalling a title, not a phrase from turn 14.

Output is a **numbered table**, and the numbering is the whole point:

```
 #   DATE        PLATFORM      PROJECT          TITLE                                MSGS  📎
 1   2026-07-29  claude-code   chatvault        Windsurf history database extract…    34
 2   2026-06-14  chatgpt       —                Windsurf .pb protobuf extraction …    58   3
 3   2026-05-02  claude-web    Second Brain     Protobuf schema for chat export       12   1

showing 3 of 47 matches · saved as last query
```

Required columns: `PROJECT` (em-dash when none) and an attachment count (blank when zero).

**Always print the total match count**, even when the result is limited — `showing 20 of 143 matches`. Without it you can't tell whether you're seeing everything, which is the same "guess what's active" problem the filter bar exists to prevent.

The last query result set is cached to `~/.chatvault/last_query.json` (row → `conv_id` mapping, query params, DB modification counter). This is what makes row-number selection possible — `find` and `export --pick` are a two-step conversation, not two independent commands you have to keep in sync yourself.

## 2. `chatvault export --pick`

```
chatvault export --pick 2                      # row 2 from the last `find`
chatvault export --pick 1,3                     # multiple rows
chatvault export --pick 2 --profile lean        # see §4
```

**Cache invalidation is content-based, never time-based.** Store SQLite's `PRAGMA data_version` (and the DB file mtime) alongside the cached rows; invalidate only when those change. A result list from three days ago is still perfectly valid if nothing was ingested since — expiring it on a clock would just make the tool annoying for no safety gain. When it *is* stale, refuse with a one-line prompt to re-run `find`; silent staleness would be worse than the UID problem, not better.

`--where "<sql>"` remains, unchanged, as the power-user / AI-facing path. Humans use `find` + `--pick`; an AI you've asked to "export everything tagged X" uses `--where` directly. Same command, two entry points, no redundant code paths.

## 2b. `chatvault show` — read without exporting

```
chatvault show --pick 2 [--profile read|lean|full]
chatvault show --pick 2 --raw          # no pager, for piping
```

Renders the conversation to the terminal through a pager (the system pager — `$PAGER`/`less` — via the stdlib `pydoc.pager`; no `click` dependency since typer 0.27 vendors it internally). **Curiosity should not force you to create a file.** This is the command you'll actually use most — `find` then `show`, with `export` reserved for when you genuinely want a file on disk.

## 3. Interactive picker (optional, for the terminal-and-menu instinct)

```
chatvault export                # no args → interactive
```

Uses `questionary` (arrow-key select, type-to-filter — the same interaction model as `fzf`, no separate binary dependency). Prompts: recent conversations first, type to narrow, arrow to select, confirm profile with a second small menu. This is the "light UI that rests the eyes" version of `find` + `--pick` collapsed into one flow — same underlying query, different front door.

---

## 4. Export profiles — the atomic-selection answer

Two genuinely different intents were being served by one export path, and that's why it felt wrong. Split them explicitly:

| You say | You mean | Profile |
|---|---|---|
| "Show me what this conversation looked like" | Curiosity, memory-jogging, human reading | **`read`** |
| "Give me the substance, I'm about to think with an AI about this" | Synthesis input, noise stripped | **`lean`** |
| "Give me literally everything, I'm debugging the adapter" | Forensics | **`full`** |

```
chatvault export --pick 2 --profile read      # DEFAULT if --profile omitted
chatvault export --pick 2 --profile lean
chatvault export --pick 2 --profile full
```

**`read`** *(= today's "default" template, renamed for clarity)* — user/assistant text in full, tool calls collapsed to one line each (`→ ran search_docs(...)`), no thinking blocks. This is "what did this conversation look like."

**`lean`** — built for feeding back into an AI for synthesis, per your stated workflow:
- drop `thinking` entirely
- drop `tool_use`/`tool_result` bodies over 500 chars → replace with `[tool: web_search, 1,204 chars omitted]`
- drop anything already `excluded`
- drop blocks matching a `content_hash` you've flagged as boilerplate (level-4 dedupe, if you've applied any)
- typical result: 5–10× smaller, much higher signal-to-noise

**`full`** — everything, raw, nothing collapsed. Rare, deliberate, mostly for you debugging *ChatVault itself*, not for daily use.

### Note: these are not a size ladder

`read` and `lean` serve **different consumers**, not different compression levels:

- `read` is optimised for **human eyes** — hide the machinery, keep the conversation
- `lean` is optimised for an **AI's context window** — keep whatever carries reasoning, cut bulk
- `full` is for **forensics**

Because `read` collapses *every* tool call to a stub while `lean` preserves short tool results in full, **`lean` can legitimately be larger than `read`** on a conversation with many small tool calls. That is correct behaviour, not a defect. Only `full ≥ both` is guaranteed.

Do not "fix" this by making `lean` drop all tool calls — short tool *results* frequently contain the exact output that mattered (the error message, the command that revealed the bug), and stripping them defeats the profile's purpose.

### Manual override, since you specifically asked for atomic control

`--profile` is a named preset. `--blocks` overrides it directly when you want to hand-pick:

```
chatvault export --pick 2 --blocks text,code                    # prose + code only
chatvault export --pick 2 --blocks text --max-tool-chars 200    # tighter than lean
```

`--profile lean --blocks text,code` = apply lean's trimming rules, then further restrict to those block types. Profiles set defaults; `--blocks`/`--max-*` narrow them. You're not choosing between presets and atomic control — presets *are* saved atomic-control settings, and you can always override further.

---

## 5. What this deliberately does not yet do

**Multi-conversation project synthesis** — "reduce this whole project's conversations to effective content" — is a different, larger operation: select N conversations (probably via `--where` or a saved view), export each as `lean`, concatenate, hand to an AI, and **store the result back in the DB** rather than as a loose file (per the `digests` table proposed earlier — that's the next spec, once this browsing layer is built and lived with). This spec is the prerequisite: you can't build "synthesize a project" cleanly until "find and export one conversation" feels natural.

---

## 6. Acceptance criteria

| Test | Passes when |
|---|---|
| `find` → `export --pick` | Row numbers in `find` output map to the exact conversations exported |
| Cache survives time | `find`, wait, `export --pick 2` with no DB changes → still works. **Must not expire on a clock.** |
| Cache invalidates on change | `find`, ingest something, `export --pick 2` → refuses with a clear message |
| `show` | `find` then `show --pick 2` renders in the terminal and creates no files |
| Total count | `find --limit 5` on a 40-match query prints `showing 5 of 40` |
| Profile sizes | `full` ≥ `read` and `full` ≥ `lean`, always. On a tool-heavy conversation, `lean` is ≥50% smaller than `full`. **`read` vs `lean` has no guaranteed ordering** — see the note in §4 |
| Dialogue integrity | `lean` reproduces every user and assistant prose block **verbatim and complete** — it only ever trims machinery, never the conversation |
| `--blocks` override | `--blocks text` on a conversation with code blocks omits the code |
| Interactive picker | Launches with no args, exports the selected conversation with the confirmed profile |

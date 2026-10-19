# Spec — Markdown Reconciliation (`chatvault md-scan`)

**For:** Claude Code · **Sonnet, mid effort** · ~4–5 hours
**Written:** 2026-08-14
**Problem:** thousands of `.md` files scattered across dev folders. Some are chat exports, some are AI summaries of chats, some are original documentation. Which of them are already in the vault?

---

## 0. What this is not

**Not** about markdown *produced by* ChatVault (`export --tree`, `brief`). Those are disposable renders with known provenance.

**This is about markdown that predates the vault or came from elsewhere:** browser-plugin exports, AI-written summaries of a session, hand-pasted transcripts — mixed in with genuine documentation and notes across every dev folder.

---

## 1. Why exact hashing fails

The obvious approach — hash the file, look it up — cannot work. A markdown file is a **rendering** of a conversation, not its source. Different producers add different headers, whitespace, and structure; a summary shares no bytes at all with the original.

**So this is a record-linkage problem, not a lookup.** The answer is a tiered matcher that reports confidence rather than a boolean.

---

## 2. The four verdicts

Every scanned file gets exactly one:

| Verdict | Meaning | What you'd do |
|---|---|---|
| `in_vault` | The conversation is ingested. Verbatim match. | Safe to archive the file |
| `summary_of` | Paraphrases a vault conversation, isn't a copy of it | **Keep** — it may hold distillation the transcript doesn't |
| `chat_not_in_vault` | Looks like a chat transcript, no match found | **Actionable** — a source you never ingested |
| `not_a_chat` | Documentation, notes, original writing | Leave alone |

**`not_a_chat` is as valuable as the rest.** Your dev folders mix docs and exported chats; separating them is useful on its own, regardless of the dedup question.

---

## 3. The matcher — four tiers, cheapest first

### Tier 0 — Metadata (instant)

Check YAML frontmatter and the first 30 lines for a direct identifier:

```yaml
session_id: ec731b80-a872-416e-842b-3a5447069e34
conversation_id: … | uuid: … | url: https://gemini.google.com/app/<id>
```

Match against `conversations.native_id` / `uid`. **Exact hit → `in_vault`, confidence 1.0, done.**

Real examples exist in `history-r/Archive/corpus_markdown_parked_20260731/` — many carry `session_id`, `workspace_uri`, `created`, `messages`. Plugin exports often carry a title + date + URL.

### Tier 1 — Verbatim phrase match (fast, the workhorse)

Extract **5 distinctive spans** from the file:

- prose sentences of **40–200 chars**
- skip code fences, tables, headers, list markers, boilerplate
- **prefer text under a `user`/`prompt`/`Human` heading** — user prompts survive verbatim through plugin exports, copy-pastes, and are frequently quoted in summaries
- spread across the file (start / 25% / 50% / 75% / end), so a partial copy still hits

FTS5 phrase-search each span, group hits by `conv_id`:

| Spans hitting one conversation | Verdict | Confidence |
|---|---|---|
| ≥ 4 of 5 | `in_vault` | 0.95 |
| 3 of 5 | `in_vault` | 0.80 |
| 2 of 5 | ambiguous → Tier 2 | — |
| ≤ 1 | → Tier 2 | — |

**Your prompts are unusually long and idiosyncratic**, which makes this far more reliable than it would be for most people. Look at the samples — 100+ word prompts with distinctive phrasing. Two of those matching the same conversation is already near-certain.

### Tier 2 — Semantic (slower, only for Tier-1 misses)

Embed the file (title + first ~2,000 chars), compare against `conv_embeddings`:

| Cosine similarity | Verdict |
|---|---|
| ≥ 0.85 | `summary_of` — same content, different words |
| 0.70–0.85 | `summary_of`, low confidence — flag for review |
| < 0.70 | → Tier 3 |

**This is the tier that catches AI-written summaries**, which is exactly the case you described noticing.

### Tier 3 — Is it even a chat?

Heuristics on structure:

- alternating role headers (`## user` / `## assistant`, `**You:**`, `### Human`)
- turn-like alternation of long prose blocks
- known plugin fingerprints in header or footer

Chat-shaped → `chat_not_in_vault`. Otherwise → `not_a_chat`.

---

## 4. Command

```bash
chatvault md-scan <dir> [--recursive] [--since DATE] [--min-size 500]
chatvault md-scan <dir> --report ~/Desktop/md-report.md
chatvault md-scan <dir> --verdict chat_not_in_vault    # filter
chatvault md-scan <dir> --json
```

Output:

```
 VERDICT             CONF   FILE                                      MATCHES
 in_vault            0.95   dev/ai-detective/notes/2025-04-16-gen…    vscode:ec731b80  "I let genspark ai create…"
 summary_of          0.88   dev/chatvault/DESIGN_NOTES.md             claude_desktop:a3f1…
 chat_not_in_vault   —      Downloads/gemini-export-2025-11.md        —
 not_a_chat          —      dev/chatvault/README.md                   —

 412 files · 218 in_vault · 47 summary_of · 12 chat_not_in_vault · 135 not_a_chat
```

### Actions — never destructive

```bash
chatvault md-scan <dir> --link           # record the match in the DB (see §5)
chatvault md-scan <dir> --stage-matched ~/ChatVault/md_matched/   # MOVE, not delete
```

**Never delete a markdown file. Never modify one.** Report, optionally move confirmed duplicates to a staging folder you review yourself. Per `02_DECISIONS.md` D7 — flag, don't guess.

`--stage-matched` only moves files with confidence ≥ 0.90 and requires `--confirm`.

---

## 5. Recording matches in the vault

`--link` writes to a new table so a file is never re-scanned and the vault knows a markdown twin exists:

```sql
CREATE TABLE markdown_links (
  id          INTEGER PRIMARY KEY,
  path        TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  conv_id     INTEGER REFERENCES conversations(id),
  verdict     TEXT NOT NULL,
  confidence  REAL,
  method      TEXT NOT NULL,        -- metadata | phrase | semantic | structural
  scanned_at  TEXT NOT NULL,
  present     INTEGER NOT NULL DEFAULT 1,
  UNIQUE(sha256, conv_id)
);
CREATE INDEX ix_mdlinks_conv ON markdown_links(conv_id);
CREATE INDEX ix_mdlinks_verdict ON markdown_links(verdict);
```

Then `chatvault find --has-markdown` works, and re-scanning skips unchanged files by `sha256`.

**`summary_of` links are the interesting ones long-term** — they point at a human- or AI-written distillation of a conversation, which is exactly the raw material for the Second Brain.

---

## 6. Performance

- Tier 0 + 1 handle the large majority and are FTS-fast
- Only Tier-1 misses reach embedding — expect a small fraction
- Skip by `sha256` on re-scan
- Expect **~1,000 files in a few minutes**; embedding-heavy runs slower

Add `--limit N` for a trial run before committing to a whole tree.

---

## 7. Acceptance criteria

| Test | Passes when |
|---|---|
| Frontmatter match | A file with a known `session_id` → `in_vault`, confidence 1.0, method `metadata` |
| Phrase match, no metadata | Strip the frontmatter from that same file → still `in_vault` via `phrase` |
| Summary detection | An AI summary of an ingested conversation → `summary_of`, not `in_vault` |
| Negative control | A README with no chat content → `not_a_chat` |
| Uningested chat | A chat export from a platform not in the vault → `chat_not_in_vault` |
| Non-destructive | No source `.md` is modified or deleted under any flag combination |
| Idempotent | Second scan of an unchanged tree does no work and yields identical verdicts |

---

## 8. Suggested first run

```bash
chatvault md-scan ~/Developer --recursive --limit 200 --report ~/Desktop/md-trial.md
```

Read the report before scanning everything. The verdict distribution tells you whether the thresholds suit your material — and if `in_vault` is under-firing, the phrase thresholds are the knob, not the tiers.

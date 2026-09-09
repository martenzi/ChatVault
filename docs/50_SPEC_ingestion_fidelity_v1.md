# SPEC — Ingestion Fidelity v1: port the vetted JSON→Markdown identifier map into ChatVault

**Written:** 2026-09-07 · Claude (Opus 5) · **Status: not started**
**Audience:** a coding model executing this end to end.
**Companion audit (read first, it holds the measured evidence):** `49_AUDIT_ingestion_fidelity_vs_json_tool_2026-09-07.md` (internal, not in the public release)
**Source of the identifier map:** `~/Desktop/history-r/tools/markdown_v4/` — `_session_extractor_core_v4.0.py` (626 lines) plus the three mode wrappers and `EXTRACTION_v4.0_CHANGELOG.txt`. **Read all of them in full before writing any code.**

---

## 0. Why this spec exists, and the two independent problems it covers

The user built a standalone 3-tier JSON→Markdown converter by reverse-engineering real export files
across every provider they use, with their own manual investigation on top ("human hunter feedback").
It is vetted against real data. ChatVault's adapters were built separately, per-platform, by
pattern-guessing each wire format. Neither effort used the other's groundwork.

**These are two separate defects, not one.** The audit found them in the same pass, which caused
confusion — stated plainly:

| # | Defect | Layer | Independent? |
|---|---|---|---|
| **A** | `templates/profile.md.j2` renders only `msg.role in ("user","assistant")`, making 40.6M of 169.2M block chars (24%) unreachable through *any* profile including `full` | **export / render** | Yes — would exist even if every adapter were perfect |
| **B** | 9,175 blocks classified `unknown`, 9,097 of them with empty extracted text — adapters stored `raw_json` but recognised no shape | **ingestion / adapter** | Yes — would exist even if the template were perfect |

**A is not caused by B and B is not caused by A.** A is cheap and unlocks content that is already
correctly ingested; B is the substantial work and is where the standalone tool's value belongs.
Both are in scope here. Do A first.

### 0.1 The governing instruction from the user, verbatim in intent

> "Do not make assumptions about the json, read it entirely. That is how we pushed the JSON tool."

This is a hard constraint on Phase 2, not a style note. Every claim this spec's implementer makes
about what a provider's JSON contains must come from having enumerated **every key path present in
every record of every affected file** — not from sampling, not from reading an adapter, not from
this document. This spec deliberately does not tell you what you will find; it tells you how to
find it and what to do with it.

### 0.2 Working presumption

Both efforts did a decent job. Neither is presumed wrong wholesale. What is presumed is that
**caveats and edge cases were discovered on both sides that the other never saw** — the tool
found provider quirks ChatVault's adapters miss; ChatVault's adapters handle sources (Windsurf
protobuf, Cursor `.vscdb`, Gemini Takeout, Grok, DeepSeek) the tool was never pointed at. The
job is a merge, not a replacement.

---

## 1. The one architectural translation that governs everything

**The tool DROPS. ChatVault must LABEL.**

The converter's three tiers are implemented by *discarding* records:
`Config.skip_roles` removes whole roles, `is_boilerplate()` removes whole messages,
`_SKIP_ITEM_TYPES` removes content items. That is correct for a converter whose output is
a disposable markdown file — you can always re-run it against the JSON.

ChatVault cannot do this. Decision **D3 — never delete, filter at the view layer** — means the
database must retain everything and let profiles decide what to show. So every place the tool
says *skip*, ChatVault says *classify, then let a profile hide it*.

| Tool mechanism | ChatVault equivalent |
|---|---|
| `skip_roles` (CHAT drops `system`/`tool`/`developer`/…) | `messages.role` set correctly + profile-level role filtering (Phase 1) |
| `_SKIP_ITEM_TYPES` (drop `tool_use`, `image`, `refusal`…) | `blocks.type` set correctly — these become **first-class block types**, not omissions |
| `is_boilerplate()` (drop memory/instruction injections) | new `blocks.type = 'injected_context'` (§3.3) — retained, hidden by default |
| `clean()` drops base64/hex/`thinkingsignature` lines | retained as-is in `blocks.raw_json`; stripped from `blocks.text` (the rendered surface) |

**The three tiers map onto the three profiles.** This mapping is the deliverable's north star.
The tier definitions below are the **user's own**, given directly and quoted in substance — they
are not inferred from the code, and they correct an earlier draft of this spec that had the middle
tier wrong:

| Tool mode | ChatVault profile | What belongs in it |
|---|---|---|
| CHAT | `read` | **Dialogue only.** What was actually said. |
| TOOLS | `lean` | **The conversational process** — "conversational value and process of what the model was doing in the actual conversation for the flow. Tool calls, thinking, explicit failures." |
| RAW | `full` | **Everything, for debugging and evaluating model performance** — system/start prompts fed from external rules files before the user's prompt, heavy metadata, internal system messages (the model messaging itself or system instances, compaction events), server/EDITOR warnings. |

**The middle tier does NOT include system prompts, injected rules files, or internal system
chatter.** Those are "a really good debugging feature but very distracting and repetitive."
They belong to `full`. An earlier draft of §2.2 put `system`/`developer` roles into `lean`;
that was wrong and is corrected below.

After this spec is built, `chatvault export --profile full` must be capable of reproducing
substantively what `markdown_extraction_raw_v4.0.py` produces from the same source file. That
equivalence is the acceptance test (§7.4).

### 1.1 THE CENTRAL DESIGN RULE — tiers are driven by block type, not by role

This is the most important paragraph in this spec. Read it twice.

**Providers do not agree on where reasoning lives.** The user's direct account, from having pushed
the investigation repeatedly during the converter's development:

> "AI models' 'assistant' tag, which is the general identifier for the AI model within most JSON
> conversations, varies between where they put the thinking/reasoning process — such as 'thinking',
> or simply 'assistant' and you get the thinking content, and I think at some point they put it
> under system/tool related identifiers. It was not straightforward and consistent how AI providers
> used identifiers within JSON to signal the model's thinking process."

Treat this as accurate in shape even where memory is imprecise on specifics — it is exactly what
`_session_extractor_core_v4.0.py::extract_thinking()` encodes, which searches **four** different
places (`thoughts`/`thinking`/`reasoning` top-level keys; nested content items typed
`thinking`/`reasoning`; `redacted_thinking`; and a list-of-`{subject, description}` dicts) precisely
because no single one of them was sufficient.

**Therefore:**

1. **`blocks.type` must be derived from the observed JSON, never from a role name.** A record's role
   being `assistant`, `system`, `tool` or absent tells you nothing about whether its payload is
   reasoning. Only reading the content structure does.
2. **Profile membership is decided by `blocks.type`, not by `messages.role`.** A `thinking` block
   lands in `lean` and `full` no matter which role its provider filed it under. This makes the whole
   class of "provider put reasoning somewhere unexpected" bug *unable to affect what the user sees*,
   as long as the adapter classified the block correctly.
3. **A message renders if any of its blocks survive the profile filter.** `messages.role` becomes
   presentational — it labels the heading — and is no longer a filter at all. This eliminates the
   Defect A class of bug structurally, rather than by patching one template.

This is a deliberate change from the earlier draft, which proposed a role-allow-list per profile.
A role allow-list would have re-created the exact failure the user is warning about: reasoning filed
under `system` by some provider would have been invisible in `lean` despite being correctly
classified as `thinking`. **Do not implement a role allow-list.**

### 1.2 Standing instruction to the implementing model

> "It is important that any AI model that is instructed to read a JSON file does not proceed under
> assumptions if they seek to investigate/find content in the JSON files. They should work from
> first principles and read the actual content. This was a repeated problem where I pushed further
> as a human to simply ask to read everything instead of searching JSON via assumed
> tags/identifiers or inferred guesswork."

Concretely, in this codebase, the following are **prohibited** while executing this spec:

- `grep`-ing a JSON file for a key you expect to exist and concluding from a hit or a miss.
- Reading an existing adapter and treating its assumptions as a description of the data.
- Sampling N records and generalising.
- Reading this spec and treating its examples as an inventory of what exists.

The only admissible evidence is a full enumeration produced by `chatvault audit-shapes` (§4.2) over
**every record of every file** in scope. If a claim in this spec conflicts with that enumeration,
the enumeration wins and the spec is wrong — say so in the report.

---

## 2. Phase 1 — Fix the export role gate (do this first)

**Effort:** small. **Files:** `templates/profile.md.j2`, `src/chatvault/export.py`, `tests/test_export_profiles.py`.
**No schema change. No re-ingest. No adapter change.**

### 2.1 The defect

`templates/profile.md.j2` line 16 wraps the whole message body in:

```jinja
{% if msg.role in ("user", "assistant") %}
```

Live message-role counts in production: `assistant` 60,297 · `tool` 28,191 · `user` 21,771 ·
`system` 9,196 · `unknown` 8. So every block hanging off a `tool`, `system` or `unknown` message
is silently unreachable — 36,673 blocks / 40,601,007 chars, including 13.9M chars of `tool_use`,
12.8M of `attachment`, 9.4M of `tool_result`.

This contradicts `export.py`'s own documented contract, which states that `full` "never collapses"
and that only `thinking` and `tool_use`/`tool_result` are ever profile-affected. It is a third,
undocumented filter that no profile and no `--blocks` flag can turn off. It also makes export
fidelity vary by platform for reasons unrelated to the source data: adapters that put tool traffic
on its own `role='tool'` message (claude_code, hermes, openclaw, grok, claude_desktop) lose all of
it, while adapters that nest tool blocks inside an `assistant` message keep it.

### 2.2 Required change — block-type policy replaces the role gate entirely

Per §1.1, **do not add a role allow-list.** Replace the role gate with an explicit block-type
visibility matrix, and let roles be presentational only.

```python
# What each profile SHOWS, keyed on blocks.type — the only tier axis.
# Mirrors the standalone converter's three modes, inverted per D3: nothing is
# dropped from the DB, only from this render.
#
# Crucially, membership is NOT keyed on messages.role. Providers disagree about
# which role they file reasoning under (see spec §1.1), so a role-keyed policy
# would hide correctly-classified `thinking` blocks for some providers and not
# others. Block type is the provider-independent fact.
_PROFILE_BLOCK_TYPES: dict[str, set[str] | None] = {
    # CHAT tier — dialogue only.
    "read": {"text", "code", "attachment", "image"},
    # TOOLS tier — the conversational process: dialogue + what the model was
    # doing + how it failed. NOT system prompts, NOT injected rules files,
    # NOT internal system chatter (those are noisy and repetitive; they're RAW).
    "lean": {"text", "code", "attachment", "image",
             "thinking", "tool_use", "tool_result", "error", "refusal", "audio"},
    # RAW tier — everything, for debugging and model-performance evaluation.
    "full": None,   # None = no filtering whatsoever
}
```

Required behaviour:

- `render_profile()` filters **blocks** by this matrix. It does not filter messages by role.
- **A message renders if at least one of its blocks survives.** A message whose blocks are all
  filtered out is skipped silently (it would render as an empty heading otherwise).
- The template's `{% if msg.role in ("user","assistant") %}` gate is **deleted**. The heading
  prints the actual role, whatever it is — `## Tool`, `## System`, `## Unknown` are all valid.
- The existing `--blocks` option keeps working and keeps overriding the profile's default set —
  it is the same axis, now made primary rather than secondary.
- `--max-tool-chars` and the `read`/`lean`/`full` tool-collapse budgets are unchanged; collapsing
  is a separate concern from visibility.
- Do **not** add a `--roles` option. There is no role axis anymore. If a user genuinely wants
  "only what the user said," that is `--blocks text` plus a role they can grep for, or a future
  `--only-role` filter added on evidence of real need — not speculative surface area now.
- `profile.md.j2` must dispatch on every block type in §3.3's extended vocabulary. One template,
  not two.

### 2.3 Why this also fixes the thing the user is worried about

Under this design, if provider X files its reasoning under `role="system"` and provider Y files it
under `role="assistant"`, and both adapters correctly set `blocks.type='thinking'`, then **both
appear in `lean` and `full`, identically.** The role variance the user described becomes irrelevant
to output. All the risk concentrates in one place — adapter classification — which is exactly where
§4's evidence requirement is aimed.

### 2.4 Do not silently change `read`

`read`'s current output is user+assistant only, which is what `read` is *for*. Phase 1 must leave
`read` byte-identical for every conversation in production. Prove it (§7.1) — this is the guard
that the change is additive, not a behavioural rewrite.

---

## 3. Phase 2 — Port the identifier map into a shared ingestion module

**Effort:** medium. **New file:** `src/chatvault/adapters/identifiers.py`. **Tests:** new `tests/test_identifiers.py`.

Phase 2 creates the shared vocabulary and helpers. Phase 3 wires adapters to it. Splitting these
is deliberate: the module must be independently testable before any adapter's behaviour changes.

### 3.1 Port these from `_session_extractor_core_v4.0.py` — verbatim in logic

Do not paraphrase, re-derive, or "improve" these while porting. They encode findings from real
files. Port them as data/functions, then extend only where §3.3 says to.

| From the core | Port as | Notes |
|---|---|---|
| `norm_role()` incl. the synonym map `{human→user, assistant_response→assistant, gemini→assistant, bot→assistant, ai→assistant}` and the dict-valued-role case (`role`/`name`/`type` keys) | `normalise_role(value) -> str` | Every adapter must route its role through this. Currently each adapter has its own ad-hoc mapping. |
| `_SKIP_ITEM_TYPES` (`tool_call`, `tool_result`, `tool_use`, `function_call`, `function_call_output`, `function_result`, `custom_tool_call`, `custom_tool_call_output`, `image`, `image_url`, `input_image`, `output_image`, `audio`, `refusal`, `error`, `toolcall`) | `CONTENT_ITEM_TYPE_MAP: dict[str, str]` mapping each to a ChatVault `blocks.type` | **Inverted per §1** — the tool skips these, ChatVault classifies them. `refusal` and `audio` have no current ChatVault block type; add them (§3.3). |
| `_BASE_SKIP_TYPES` (`session`, `session_meta`, `event_msg`, `turn_context`, `thinking_level_change`, `model_change`, `custom`, `info`) | `NON_MESSAGE_RECORD_TYPES: set[str]` | Records that are not conversational turns at all. ChatVault should classify these as `blocks.type='meta'` (§3.3) rather than emit a message, **or** skip emitting a message while still recording the record in the conversation's `raw_json`. Choose one and document which; do not let them land as `unknown`. |
| `extract_content()` — the str / list-of-dicts / dict tri-branch, **including the `itype == ""` case** (Gemini-CLI `{text: "..."}` items with no `type` field) and the nested `content.content` recursion | `extract_content(content) -> tuple[str, str \| None]` | The empty-type branch is exactly the kind of edge case adapters guessing at a schema will miss. |
| `extract_thinking()` — `thoughts`/`thinking`/`reasoning` top-level keys, the nested-content-item fallback, `redacted_thinking`, and the **list-of-dicts `{subject, description}`** shape | `extract_thinking(rec) -> str \| None` | Reasoning is exposed differently by every provider; this function is the accumulated answer. |
| `COLLECTION_KEYS` (`messages`, `items`, `turns`, `conversation`, `conversations`, `entries`, `mapping`) | `COLLECTION_KEYS: tuple[str, ...]` | Where to descend when a record is a container. |
| `clean()` — CRLF normalisation, NUL strip, `<final>` tag strip, blank-line collapse, and the drops for `thinkingsignature` lines and `LONG_B64` / `HEX_LONG` (≥60-char base64/hex) lines | `clean_text(text) -> str` | The base64 rule is the fix for the Windsurf protobuf-leak class of bug. **Applies to `blocks.text` only — `blocks.raw_json` keeps the original bytes.** |
| `_METADATA_RE` (`(Conversation info\|Sender) (untrusted metadata): ```json…```` `), `_TS_RE` (leading `[timestamp]` line prefixes), `_GEMINI_REF_RE` (`--- Content from referenced files --- … --- End of content ---`) | `strip_metadata_envelope()`, `strip_referenced_file_dump()` | Same rule: strip from `text`, never from `raw_json`. |
| `coerce_dt()` — the **epoch-seconds vs milliseconds disambiguation at the `1_000_000_000_000` boundary**, digit-string handling, ISO-8601 with `Z` | `coerce_timestamp(value) -> datetime \| None` | Every adapter currently does its own timestamp parsing. Unify. |
| The `boilerplate_chat()` marker list — `# agents.md instructions`, `<instructions>`, `<environment_context>`, `<permissions_instructions>`, `<system-retrieved-memory[`, `# user.md`, `# soul.md`, `# identity.md`, `# memory.md`, and the `---\n*scout ·` / `*orchestrator ·` / `*archivist ·` separators | `INJECTED_CONTEXT_MARKERS: tuple[str, ...]` + `is_injected_context(role, text) -> bool` | **Inverted per §1** — these become `blocks.type='injected_context'`, retained and hidden by default, never dropped. Also port `_is_tool_json()` (detects a `{results\|tool_use_id}` JSON body pasted into a text field) and `_is_ts_listing()`. |
| `read_records()`'s `errors="replace"` + U+FFFD counting | `decode_utf8_counting_replacements(raw: bytes) -> tuple[str, int]` | **Mandatory.** Never `errors="ignore"` anywhere in the ingest path — that silently deletes bytes. The v4.0 changelog documents this as a latent-correctness landmine found by audit, not by failure. Surface the count (§3.4). |

### 3.2 Also port the changelog's two documented fixes as regression tests

`EXTRACTION_v4.0_CHANGELOG.txt` records two real defects found against real files. Both must
exist as tests in `tests/test_identifiers.py`, so ChatVault cannot regress into them:

1. **Windsurf `<SYSTEM-RETRIEVED-MEMORY[uuid]>…</…>` memory injection.** Detected by the marker
   substring; the changelog verified these are dedicated memory-dump turns (>99.9% of the message
   covered by the tag span), not messages mixing memory with real dialogue. In ChatVault this must
   classify as `injected_context`, not as `text`.
2. **Invalid UTF-8 must not silently vanish.** A fixture with a deliberately invalid byte must
   produce a non-zero replacement count that reaches the ingest report.

### 3.3 New `blocks.type` values this spec introduces

Current live vocabulary: `text · thinking · tool_use · tool_result · attachment · image · system · error · unknown`.
Add:

| New type | Meaning | Rendered in |
|---|---|---|
| `injected_context` | System-injected memory/instruction/environment content that arrived inside an ordinary message body — what the tool's `boilerplate_*` drops | `full` only |
| `meta` | Non-conversational session records (`session`, `turn_context`, `model_change`, `thinking_level_change`, …) that carry information but are not a turn | `full` only |
| `refusal` | A model refusal item | `lean`, `full` |
| `audio` | Audio content item | `lean`, `full` |

`unknown` **remains in the vocabulary and remains legitimate** — it is the honest label for a shape
no rule matched, per Decision D7 ("flag, don't guess"). The goal of this spec is not to eliminate
`unknown` by relabelling; it is to eliminate it only where a *verified* rule exists. **An `unknown`
block with populated `text` is strictly better than a wrongly-confident classification.** See §4.4.

Update `profile.md.j2`'s block dispatch and `_PROFILE_ROLES`/block-visibility policy for the four
new types. No migration is required — `blocks.type` is an unconstrained `TEXT` column (verified
against the live schema).

### 3.4 Ingest reporting

`decode_utf8_counting_replacements`'s count, plus a per-file count of blocks that landed as
`unknown`, must be surfaced at the end of `chatvault ingest` and recorded on the `source_files`
row. Mirror `conversion_log.md`'s structure from the tool: per source file, what was produced,
and an explicit `⚠ Warning` line when something was lossy. Silent lossiness is the single failure
mode this whole spec exists to remove.

---

## 4. Phase 3 — Per-platform `unknown`-block audit and adapter fixes

**Effort:** the bulk of the work. **This is where "read the JSON entirely" is binding.**

### 4.1 Scope, by measured size

| Platform | `unknown` blocks | Adapter |
|---|---|---|
| vscode | 6,288 | `adapters/vscode_legacy.py` |
| cursor | 1,512 | `adapters/cursor_legacy.py` |
| windsurf | 727 | `adapters/windsurf_legacy.py` |
| cursor_copilot | 620 | `adapters/cursor_legacy_copilot.py` |
| grok | 19 | `adapters/grok.py` |
| chatgpt | 6 | `adapters/chatgpt.py` |
| claude_code | 3 | `adapters/claude_code.py` |

Do them in this order. The first four are 99.7% of the problem.

### 4.2 Build the audit harness first — `chatvault audit-shapes`

A new command (`src/chatvault/shapes.py` + a `cli.py` entry). It is a permanent diagnostic, not a
throwaway script, because this problem will recur every time a provider changes its format.

```
chatvault audit-shapes --platform vscode [--type unknown] [--out report.md]
```

It must:

1. Select the target blocks from `v_blocks` joined to `v_conversations`.
2. For each, parse `blocks.raw_json` and **walk every key path to full depth** — including inside
   nested dicts and lists. No `head`, no `LIMIT 5`, no sampling. Every record.
3. Aggregate: every distinct key path, its occurrence count, its observed value types, and for
   string-valued leaves the min/median/max length and a short example.
4. Rank leaf paths by **total character volume** — that is the direct measure of "how much real
   content is currently being thrown away under this shape."
5. Cross-tabulate distinct shape signatures (the sorted set of top-level keys, plus discriminator
   fields like `kind` / `type` / `role`) against counts, so the implementer sees *how many distinct
   shapes* hide behind `unknown` rather than one.

The output of this command is the evidence base. **Do not write a single adapter change before
running it and reading its full output for that platform.**

### 4.2b The reasoning-locator sweep — mandatory, and NOT limited to `unknown` blocks

**This sub-phase exists because of a specific gap the `unknown`-block audit cannot see.**

The §4.1 table counts blocks the adapter *admitted* it couldn't classify. It does **not** count
blocks the adapter classified **confidently and wrongly** — most importantly, reasoning content
that landed as `type='text'` (so it shows up in `read` as if it were dialogue) or as `type='system'`
(so it is invisible below `full`). Neither shows up as `unknown`. Both are worse than `unknown`,
because they are silent.

Given §1.1 — providers file reasoning under inconsistent identifiers — this is a live risk, not a
theoretical one.

Run a second `audit-shapes` pass, `--mode reasoning`, over **every block of every platform
regardless of current type**, which must:

1. Enumerate, across all `blocks.raw_json`, every key path whose final segment matches any of:
   `thinking`, `thought`, `thoughts`, `reasoning`, `reasoning_content`, `reasoningText`,
   `redacted_thinking`, `chain_of_thought`, `cot`, `analysis`, `deliberation`, `scratchpad`,
   `internal`, `reflection`.
   **This list is a starting point, not a definition.** It is derived from
   `_session_extractor_core_v4.0.py` plus common provider vocabulary. Step 2 is what actually finds
   the answer.
2. **Independently of that list**, enumerate every *string-valued* key path in the corpus that does
   not currently contribute to any block's `text`, ranked by total character volume. Any high-volume
   path not accounted for is a candidate for misclassified or dropped content of *any* kind —
   reasoning, tool output, or otherwise. This step is what satisfies "read it entirely" rather than
   "search for the names we already know."
3. Cross-tabulate each discovered reasoning-bearing path against: the platform, the record's role
   value, and the `blocks.type` currently assigned. **Report every case where reasoning content is
   currently typed as something other than `thinking`.**
4. Report the inverse too: blocks currently typed `thinking` whose content does not come from a
   reasoning-bearing path — i.e. possible over-classification.

The output of step 3 is a defect list. Each entry becomes an adapter fix under §4.4's evidence rule.

**Expected finding, stated so it is not mistaken for success if it appears:** the corpus currently
has zero `thinking` blocks for `gemini`, `cursor`, `cursor_copilot`, `hermes` and `generic`. For
some of those that is genuinely correct (the source has no reasoning to expose). For others it may
be reasoning sitting under a shape nobody looked for. **Do not assume which is which — determine
it from the enumeration, per platform, and state the evidence either way.**

### 4.3 What the current code does wrong — the pattern to expect

`adapters/vscode_legacy.py::_classify()` is representative and worth quoting as the archetype:

```python
def _classify(rec: dict) -> tuple[str, str]:
    role = rec.get("role")
    if role == "user":      return "user", "text"
    if role == "thinking":  return "assistant", "thinking"
    if role == "tool":      return "tool", "tool_use"
    if role == "result":    return "system", "unknown"      # <-- content discarded
    return "assistant", "text" if rec.get("text") else "unknown"   # <-- content discarded
```

Two guesses, both lossy. `role=="result"` records are labelled `unknown` and the parse then reads
`rec.get("text") or rec.get("content")` — neither of which exists on those records, so `text` lands
empty while the real payload sits in the raw JSON (audit §4a sampled `result.metadata.renderedUserMessage`,
`result.timings`, `metadata.codeBlocks`). The final line is a catch-all: any assistant record without
a top-level `text` key — `kind:"inlineReference"` and every other part kind — becomes an empty
`unknown`. This is exactly "acts on standard practices and makes educated guesses to identifiers."

**Expect this pattern in all four major adapters.** Do not assume the fix for one transfers to
another without running §4.2 against each.

### 4.4 Rules for changing an adapter

1. **Evidence before code.** Every new branch must cite a key path and an occurrence count from
   the `audit-shapes` report for that platform. Put the citation in a code comment.
2. **Never invent a mapping to make a number go down.** If a shape's meaning is genuinely unclear
   after reading the JSON, leave the block `type='unknown'` but **populate its `text`** from the
   most content-bearing leaf path, and tag the conversation `review/shape-unclear`. Per D7, a
   flagged unknown is a correct outcome; a confident wrong label is not.
3. **Route everything through `identifiers.py`.** No adapter keeps a private role map, timestamp
   parser, or thinking-extraction path after this phase.
4. **`raw_json` stays byte-identical.** Nothing in this phase changes what is stored raw; it only
   changes what is *extracted into* `type`, `text`, `tool_name` and `role`.
5. **One adapter per commit**, with its `audit-shapes` before/after numbers in the message.

### 4.5 Cross-check against the tool's four extractors

The core's `extract_messages()` dispatches through `_from_openclaw` → `_from_rollout` →
`_from_history` → `_from_generic`, then falls back to descending `COLLECTION_KEYS`. Compare each
ChatVault adapter's record-walking against that chain and note in the audit report any container
shape the tool descends into that the adapter does not. `_from_generic`'s field-preference order
(`role` → `type` → `author.role`/`author.name`; content from `content`/`text`/`parts`/`body`/`value`/`message`;
timestamps from `timestamp`/`create_time`/`ts`/`time`/`created_at`) is the accumulated fallback
knowledge and should become `identifiers.py`'s generic path.

---

## 5. Phase 4 — Reprocess and verify

`chatvault ingest --reprocess` already exists (built 2026-08-04, spec v1.4 REVISED) and exists
precisely for this: re-running an improved adapter over already-ingested sources via loader-level
positional backfill.

Mandatory sequence, per this repo's established practice (STATUS.md shows it on every prior
data-touching pass):

1. `chatvault backup` production before anything.
2. Dry-run `--reprocess` against a **full scratch copy** of the real DB, per platform.
3. Compare: conversation and message identities must be preserved; count changes are permitted only
   when the improved adapter exposes additional real conversations or splits an opaque block into
   multiple accurately typed blocks. Report and explain every count delta. The `unknown`-block count
   must drop; **no existing block's `raw_json` may change**; `chatvault verify` clean;
   `PRAGMA integrity_check` ok.
4. Only then run against production, and confirm it matches the scratch run.
5. Re-embed anything whose `text` materially changed (`chatvault embed --force` scoped to the
   affected platform) — embeddings are derived from `text`, so improving extraction invalidates them.
6. Regenerate `docs/05_INGESTION_LEDGER.md` §1 via `chatvault ledger --markdown`.

---

## 6. Explicit non-goals

- **Do not port the tool's file discovery, output naming, `derive_title()`, `dedupe()` or markdown
  writer.** ChatVault has its own, better-integrated equivalents (four-level dedupe, uid-based
  naming, Jinja templates). Only the *identifier map* is being ported.
- **Do not make ChatVault shell out to the standalone tool.** The tool reads files; ChatVault reads
  a database. Porting the knowledge is correct; invoking the tool is not.
- **Do not modify anything in `~/Desktop/history-r/`.** It is the source of truth for the extraction
  side and is read-only for the purposes of this spec.
- **Do not change `raw/`, or re-download/re-export any source.** Everything needed is already ingested.
- **Do not delete or exclude any row** to make a metric look better (D3).

---

## 7. Acceptance criteria

Each must be verified against **real production data**, not fixtures, and reported with the actual
numbers — per Decision D14, verified against data, never against a summary.

| # | Criterion | How to prove it |
|---|---|---|
| 7.1 | **`read` is unchanged by Phase 1** | Render `read` for a large random sample of real conversations before and after; assert byte-identical output. |
| 7.2 | **`full` reaches every role** | For a real `claude_code` or `hermes` conversation with `role='tool'` messages, `--profile full` output contains the tool traffic. Assert the total rendered char count for that conversation rises by the amount §2.1 predicts for it. |
| 7.3 | **Tier membership is block-type-driven, not role-driven** | Construct a test conversation with a `thinking` block on a `role='system'` message and another on a `role='assistant'` message. Assert both are absent from `read`, both present in `lean`, both present in `full` — **identically**. This is the regression guard for §1.1. |
| 7.3b | **`lean` excludes system/injected context** | A real conversation carrying a start-prompt / rules-file injection shows that content in `full` and **not** in `lean`. |
| 7.4 | **Tier equivalence with the vetted tool** | Take one real source JSON file that exists both on disk and in the vault. Run `markdown_extraction_raw_v4.0.py` on it, and `chatvault export --profile full` on the same conversation. Diff. Every substantive difference must be explained in writing — either as a ChatVault improvement or as a remaining gap logged in STATUS.md. **This is the single most important criterion in this spec.** Repeat for CHAT↔`read`. |
| 7.5 | **`unknown` blocks with empty text drop materially** | Report the before/after count per platform. Baseline is 9,175 total / 9,097 empty-text. State the number, do not characterise it. |
| 7.6 | **No confident guessing** | Every remaining `unknown` block after Phase 3 either has populated `text`, or its conversation carries `review/shape-unclear`. Assert with a query returning zero violations. |
| 7.7 | **Raw fidelity preserved** | `SELECT COUNT(*) FROM blocks WHERE raw_json IS NULL OR raw_json = ''` is 0 before and after. Hash the full `raw_json` column before and after reprocess; assert identical. |
| 7.8 | **Invalid UTF-8 is reported, never dropped** | A fixture with an invalid byte produces a non-zero replacement count in the ingest report. `grep -rn 'errors="ignore"' src/` returns nothing in the ingest path. |
| 7.9 | **Injected context is retained, not dropped** | A real Windsurf conversation containing `<SYSTEM-RETRIEVED-MEMORY[` classifies as `injected_context`, is absent from `read`, and is present in `full`. |
| 7.10 | **Vault integrity** | `chatvault verify` clean, `PRAGMA integrity_check` = `ok`, conversation/message identities preserved, every legitimate count delta explained, full test suite green before and after each phase. |

---

## 8. Build order

| # | Phase | Gate before moving on |
|---|---|---|
| 1 | §2 — export role gate + `--roles` | 7.1, 7.2, 7.3 pass |
| 2 | §3 — `identifiers.py` + tests, no adapter wired yet | 7.8 passes; `tests/test_identifiers.py` green; **zero change** to any production data |
| 3 | §4.2 — `chatvault audit-shapes`, run against all 7 platforms, full reports committed | Reports exist and have been read in full |
| 3b | §4.2b — the reasoning-locator sweep, all 13 platforms (not just the 7 with `unknown` blocks) | Misclassified-reasoning defect list produced; per-platform evidence stated for every platform currently showing zero `thinking` blocks |
| 4 | §4 — adapters, one commit each, vscode → cursor → windsurf → cursor_copilot → grok → chatgpt → claude_code | 7.5, 7.6 measured per platform |
| 5 | §5 — reprocess to production, re-embed, regenerate ledger | 7.7, 7.10 pass |
| 6 | §7.4 — the tier-equivalence diff against the standalone tool, written up | Differences explained in writing |

**Stop and report to the user after Phase 3 (the audit reports).** That is the point where the
evidence is in but nothing has been changed, and it is the natural place for the user — who is
the one who did the original reverse-engineering — to look at what the JSON actually contains and
correct any reading of it before adapters are touched.

---

## 9. Update these docs when done

- `STATUS.md` — a build-log row per phase, and close the two Open items added 2026-09-07.
- `docs/49_AUDIT_ingestion_fidelity_vs_json_tool_2026-09-07.md` — dated resolution banner with the real after-numbers.
- `docs/02_DECISIONS.md` — a new decision recording that the standalone converter's identifier map
  is the canonical source for provider-shape knowledge, with rejected alternatives (shell out to the
  tool; keep per-adapter private guessing).
- `docs/05_INGESTION_LEDGER.md` — regenerate §1.
- `docs/specs/CHATVAULT_SPEC_v1.3_BROWSE_AND_EXPORT.md` — correct §4's profile contract to document
  role filtering as a first-class axis alongside block types and tool budgets.

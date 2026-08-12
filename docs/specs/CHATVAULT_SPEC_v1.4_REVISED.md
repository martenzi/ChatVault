# ChatVault — Spec v1.4 REVISED: Attachments & Projects

**Supersedes CHATVAULT_SPEC_v1.4_ATTACHMENTS_AND_PROJECTS.md entirely.** Delete or ignore that file.

**Why revised:** the Haiku probe of the Claude export came back and invalidated two of my core assumptions. The design is now roughly a third of the work.

---

## 0. What the probe found

| Finding | Consequence |
|---|---|
| 254 conversations, 2,240 messages, 27 projects | Small. This is an afternoon, not a week. |
| **88 attachments totalling 0.41 MB — no binary files at all** | The whole content-addressed blob store I specced is **unnecessary for this source**. |
| Attachment text is **already inline** in the JSON, in `extracted_content` | No file fetching, no text extraction pipeline. It's just a string sitting there. |
| **Conversations carry no project reference** | The real problem. See §3. |
| 100+ MB of extracted artifacts in the folder | Derivatives of your earlier markdown-conversion effort. **Ignore — do not ingest.** |

**Verify before building:** 254 conversations may be lower than you expect. Open claude.ai and compare against what you actually see. If it's short, this export is old or partial and a fresh one is worth requesting first — including because a newer export may solve §3 for free.

---

## Part A — Attachments (much simpler than planned)

### A.1 What changed

I specced a content-addressed file store because I assumed binary files. There aren't any. The text is inline, and the total payload is under half a megabyte.

**So: build the table, skip the file store.** The schema keeps room for real files so that a future source with binaries needs no migration — but nothing gets built for that until a source actually produces one.

This removes an entire subsystem: no hashing-and-sharding, no size policy, no PDF/docx extraction, no `rebuild`, no separate backup concern. **Your weekly backup stays complete.** Everything stays inside the database.

### A.2 Schema — migration 005

```sql
CREATE TABLE attachments (
  id             INTEGER PRIMARY KEY,
  platform       TEXT NOT NULL,
  filename       TEXT,
  mime           TEXT,
  ext            TEXT,
  bytes          INTEGER,               -- reported size, may be NULL
  content        TEXT,                  -- the inline extracted text
  content_hash   TEXT NOT NULL,         -- sha256 of content (or of file bytes, later)
  availability   TEXT NOT NULL DEFAULT 'text',
      -- text    : we have the text content
      -- file    : we have actual bytes on disk (future)
      -- missing : recorded as having existed, content unavailable
  store_path     TEXT,                  -- NULL unless availability='file'
  sensitivity    TEXT NOT NULL DEFAULT 'normal',
  first_seen_at  TEXT NOT NULL,
  excluded       INTEGER NOT NULL DEFAULT 0,
  raw_json       TEXT,
  UNIQUE(content_hash, filename)
);

CREATE TABLE attachment_links (
  attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
  target_type   TEXT NOT NULL,   -- 'message' | 'conversation' | 'project'
  target_id     INTEGER NOT NULL,
  role          TEXT,            -- 'upload' | 'artifact' | 'project_knowledge'
  created_at    TEXT NOT NULL,
  PRIMARY KEY (attachment_id, target_type, target_id)
);
CREATE INDEX ix_attlink_target ON attachment_links(target_type, target_id);

ALTER TABLE blocks ADD COLUMN attachment_id INTEGER REFERENCES attachments(id);

CREATE VIRTUAL TABLE attachments_fts USING fts5(
  content, filename,
  content='attachments', content_rowid='id',
  tokenize='porter unicode61'
);
-- triggers mirroring blocks_fts

CREATE VIEW v_attachments AS SELECT * FROM attachments WHERE excluded = 0;
```

`availability='missing'` is the honest-record case: an attachment referenced by a message whose content we never got. Recording it is strictly better than silence — you at least learn the filename and date of the thing you're missing.

### A.3 Surgical removal — you asked for no limitations

Three levels, escalating, all available:

```bash
# 1. Hide it. Reversible. The normal move.
chatvault attachments exclude <id> --reason "not needed"
chatvault attachments restore <id>

# 2. Wipe the content, keep the record. Middle ground:
#    "I don't want this text in my database, but I want to remember it existed."
chatvault attachments redact <id> --confirm
#    → content = NULL, availability = 'missing', logged

# 3. Delete the row entirely. Takes a backup first, logs it, requires --confirm.
chatvault attachments purge <id> --confirm
```

Every one accepts `--where "<sql predicate>"` for bulk operations, so "remove every attachment over 100 KB from platform X before 2025" is one command. **Level 2 is the interesting one** and it doesn't exist in most systems — it lets you strip content you don't want stored while keeping the historical fact that it was there.

### A.4 Commands

```
chatvault attachments list [--conv <n>] [--project X] [--platform X] [--type pdf] [--missing]
chatvault attachments show <id>          # print the text
chatvault attachments stats
chatvault attachments search "<query>"   # FTS over content + filename
```

`chatvault find --has-attachments` and `chatvault search --include attachments` both work, per v1.3.

Export: `--profile read` renders each attachment as a header line plus its text, indented. Since it's all text, nothing needs copying next to the markdown — an export is self-contained by default.

---

## Part B — Projects

### B.1 Schema — migration 006

```sql
CREATE TABLE projects (
  id            INTEGER PRIMARY KEY,
  uid           TEXT NOT NULL UNIQUE,      -- '<platform>:<native_id|slug>'
  platform      TEXT NOT NULL,             -- ALWAYS set, ALWAYS displayed
  native_id     TEXT,
  name          TEXT NOT NULL,
  description   TEXT,
  instructions  TEXT,
  created_at    TEXT,
  raw_json      TEXT,
  excluded      INTEGER NOT NULL DEFAULT 0,
  ingested_at   TEXT NOT NULL
);

ALTER TABLE conversations ADD COLUMN project_id INTEGER REFERENCES projects(id);
CREATE INDEX ix_conv_project_id ON conversations(project_id);

CREATE VIEW v_projects AS SELECT * FROM projects WHERE excluded = 0;
```

**`platform` is NOT NULL and is displayed in every project view, without exception.** A Claude project named "Second Brain" and a ChatGPT project named "Second Brain" are two unrelated records and must never be merged, summed, or shown ambiguously.

```
chatvault projects list

 PLATFORM     PROJECT                CONVS   DOCS
 claude-web   Second Brain              ?       8
 claude-web   Mentoring Me              ?       3
 chatgpt      Second Brain              ?       0    ← separate record, never merged
```

Project knowledge docs are `attachments` linked with `target_type='project'`, `role='project_knowledge'`. No new mechanism.

### B.2 The real problem: no conversation→project mapping

The export gives you 254 conversations and 27 projects with **no link between them**. Options, in order of preference:

**Option 1 — Get a fresh export first. Do this before anything else.**
Costs you one request and a wait. If Anthropic's current export format includes project references, the problem evaporates and you skip everything below. Check before doing manual work.

**Option 2 — AI-assisted tagging, then you review.** *(the realistic fallback)*

254 conversations is small enough to be tractable:

1. `chatvault export --where "platform='claude-web'" --profile lean --first-message-only` → a compact list of title + opening message per conversation
2. Hand that plus the 27 project names, descriptions and instructions to Sonnet
3. Sonnet proposes a mapping with a confidence score per conversation
4. Import as tags — **`tag_links.source='llm'`**, which the schema already supports
5. You review only the low-confidence ones — probably 30–50 of them, maybe 30 minutes
6. Correct individual assignments by hand whenever you notice one is wrong

Store the result as **tags first** (`project/claude-web/<name>`), and only promote to `project_id` once you're satisfied. Tags are reversible and already covered by `curation export`, so a bad guess costs nothing. **Do not let an AI write `project_id` directly.**

**Option 3 — accept no mapping.** Projects exist as standalone records with their docs, searchable and browsable; conversations just aren't grouped. Perfectly usable, and honestly fine if Option 1 fails and you don't want to spend the 30 minutes.

Recommendation: **try Option 1, fall back to Option 2, and treat Option 3 as an acceptable landing spot.** Don't turn project mapping into a project.

### B.3 Adapter work

`claude_web` needs:
1. Parse the projects file → `projects` rows (platform `claude-web`)
2. Parse project docs → `attachments` with `role='project_knowledge'`
3. Parse `extracted_content` on message attachments → `attachments` + `attachment_links` + `blocks.attachment_id`
4. **Ignore the 100+ MB artifacts folder entirely** — add it to `raw_exclusions/_why.md` as "derivatives of prior markdown extraction, not source material"

Then re-ingest. Existing conversations update in place; nothing duplicates.

---

## Acceptance criteria

| Test | Passes when |
|---|---|
| Attachment count | After re-ingest, attachment count matches the probe's 88 |
| Attachment search | A distinctive phrase inside an attachment is found by `attachments search` |
| Redact | `redact` empties content, sets `availability='missing'`, keeps the row and the link |
| Purge | Takes a backup first, logs, requires `--confirm` |
| Platform separation | Two same-named projects on different platforms remain two rows in every view |
| Project docs | 27 projects appear with their docs attached and searchable |
| No regression | Conversation and message counts unchanged after migrations 005/006 |
| Artifacts ignored | The 100+ MB derivative folder produces zero rows |

## Build order

1. Migration 005 + attachments table + `claude_web` attachment parsing
2. FTS + search + list/show
3. Removal commands (exclude / redact / purge)
4. Migration 006 + projects + project docs
5. Re-ingest, verify against probe numbers
6. **Then** decide on project mapping (§B.2) — separately, unhurried

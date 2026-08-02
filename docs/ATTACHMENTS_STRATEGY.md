# ChatVault — Attachment Handling Strategy

**Current state:** Attachments are recognized and tagged, but not stored. This document explains the options and recommends an approach.

---

## What you have now

Your adapters create blocks with `type='image'` and `type='attachment'`, which means:

- ✅ **Searchable:** `chatvault search --type image` finds conversations with images
- ✅ **Preserved in raw_json:** the original attachment metadata lives in `raw_json` blob
- ✅ **Counted:** audit reports how many images/attachments exist
- ❌ **Not stored:** the actual file content (image bytes, audio, etc.) is not in the DB

The adapters preserve **references** to attachments — URLs, asset pointers, hashes — but not the files themselves.

---

## The three options

### Option A — Status quo (current)

**Keep attachments out of the DB. Store raw files separately.**

```
~/ChatVault/
├── raw/              (source exports, no attachment files)
├── attachments/      (side folder, organized by conversation uid + block id)
│   ├── chatgpt_6f2a_b4c3.png
│   ├── claude-code_abc_msg2_blk3.wav
│   └── _manifest.jsonl  (path, content_hash, conversation uid, block id, captured_at)
└── db/
    └── chatvault.sqlite  (metadata only)
```

**Pros:**
- Keeps DB lean; images and audio don't bloat SQLite
- Easy to back up DB and attachments separately (DB for metadata/decisions, attachments for archive)
- Can manage attachment lifecycle independently (delete old ones, keep recent ones)

**Cons:**
- Two systems to manage; `attachments/_manifest.jsonl` is similar to `raw/_manifest.jsonl`
- You manually ensure consistency: if you delete an attachment, you have to update the manifest
- Harder to export a conversation with all its attachments together

---

### Option B — Store in DB as BLOB

Add an `attachments` table:

```sql
CREATE TABLE attachments (
  id                 INTEGER PRIMARY KEY,
  block_id           INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
  content_type       TEXT NOT NULL,     -- image/png, audio/wav, etc.
  filename           TEXT,
  data               BLOB NOT NULL,     -- the actual file bytes
  size_bytes         INTEGER,
  content_hash       TEXT NOT NULL UNIQUE,
  captured_at        TEXT NOT NULL,
  metadata_json      TEXT               -- exif, duration, etc. from raw_json
);
```

**Pros:**
- **Single source of truth:** the DB holds everything, including the files
- Export a conversation → get all attachments with it
- Consistency guaranteed by FK (delete block → delete attachment)
- Dedupe at the file level (same image in two conversations = one BLOB row)

**Cons:**
- **Database bloat:** images and audio multiply the DB size. A 4MB image in 10 conversations = 40MB of storage
- Backup/restore slower (DB backups now include file data)
- SQLite isn't optimized for blobs this large; reads become slower
- Harder to selectively archive (can't just delete old attachments without repacking the DB)

**When it makes sense:** small attachments (< 100KB each, < 500 total), long-term archival priority over performance

---

### Option C — Hybrid (recommended)

**Store attachment *metadata* in the DB, files on disk, path/hash in a sidecar manifest.**

```sql
CREATE TABLE attachments (
  id                 INTEGER PRIMARY KEY,
  block_id           INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
  conv_id            INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  content_type       TEXT NOT NULL,
  filename           TEXT,
  size_bytes         INTEGER,
  content_hash       TEXT NOT NULL UNIQUE,
  disk_path          TEXT,              -- relative to $CHATVAULT_ROOT/attachments/
  captured_at        TEXT NOT NULL,
  status             TEXT DEFAULT 'present',   -- present | missing | archived
  metadata_json      TEXT
);
```

Files live in:

```
~/ChatVault/attachments/
├── <YYYY-MM>/ (by capture date)
│   ├── <content_hash_short>_<filename>
│   └── …
└── _manifest.jsonl (auto-generated from DB query)
```

**Pros:**
- ✅ DB holds searchable metadata; `chatvault search --has-attachments` works
- ✅ Conversational export includes file paths; you can fetch them on demand
- ✅ Files on disk don't bloat the DB
- ✅ Can selectively delete old attachments and just update `status='archived'`
- ✅ Reconcile can detect missing attachments and mark `status='missing'` (like it does for source files)
- ✅ Easy to move attachments to external drive; just update `disk_path`
- ✅ Dedupe works: same image in two conversations references one file

**Cons:**
- Slightly more complex: two systems, but the DB is authoritative
- Need to keep files and DB in sync (reconcile handles this)

---

## Recommendation: Option C

This aligns with ChatVault's overall philosophy: **immutable raw files, metadata in DB, derived filesystem follows the DB.**

It solves your actual use case: you want to be able to search "conversations with images" and extract them, but you don't need images bloating your database backups. You want to keep files long-term but be able to archive them and know they're archived.

---

## Implementation (for Sonnet)

### Migration 005

```sql
CREATE TABLE attachments (
  id             INTEGER PRIMARY KEY,
  block_id       INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
  conv_id        INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  content_type   TEXT NOT NULL,
  filename       TEXT,
  size_bytes     INTEGER,
  content_hash   TEXT NOT NULL UNIQUE,
  disk_path      TEXT,                 -- relative: YYYY-MM/<hash_short>_<name>
  captured_at    TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'present',
  metadata_json  TEXT,
  UNIQUE(block_id, content_hash)
);

CREATE INDEX ix_att_conv    ON attachments(conv_id);
CREATE INDEX ix_att_block   ON attachments(block_id);
CREATE INDEX ix_att_status  ON attachments(status);
CREATE INDEX ix_att_hash    ON attachments(content_hash);

CREATE VIEW v_attachments AS
  SELECT * FROM attachments WHERE status IN ('present', 'archived');
```

### Commands

```bash
chatvault attachments list [--conv-id X] [--type image|audio] [--missing]
chatvault attachments export <conv-uid> [--out DIR]  # downloads files to folder
chatvault attachments archive --before YYYY-MM-DD    # mark old ones as archived
chatvault attachments dedupe --report [--apply]      # find same-file duplicates
chatvault reconcile [--include-attachments]          # detects missing files
```

### Adapter changes

When an adapter encounters an attachment:

1. Extract the bytes (or reference URL) from the source
2. Hash them
3. Create a block with `type='image'` or `type='attachment'`, `raw_json` preserving the original reference
4. If there are bytes and `--fetch-attachments` flag: create an `attachments` row with `status='pending'`
5. A separate `chatvault attachments fetch` command (or a bulk job) downloads pending attachments from URLs, verifies hashes, moves to disk, updates `status='present'`

---

## Decision points for you

1. **Do you want attachments at all, or just metadata about them?** If you just want to know "this conversation has 3 images" but don't need the images, Option C still works — just don't run `fetch`, leave `disk_path=NULL`
2. **For your current archive, are attachment files sitting alongside the JSONs?** (OpenClaw, ChatGPT exports sometimes include them.) If yes, they need to be moved to `attachments/` during ingest.
3. **How large is your attachment corpus?** If it's <100MB total, Option B becomes reasonable. If it's >1GB, Option C is necessary.

Query the current archive:

```bash
find ~/ChatVault/raw -type f \( -name "*.png" -o -name "*.jpg" -o -name "*.wav" -o -name "*.mp3" \) | wc -l
du -sh ~/ChatVault/raw/**/* | sort -h | tail -20
```

That tells you whether attachments are even present in your sources.

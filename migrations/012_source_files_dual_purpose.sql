-- ─────────────── source_files: one physical file, two independent purposes ───────────────
-- Fixes a structural gap surfaced by the ingest.py dispatch fix for Grok's
-- real export, which carries BOTH a `conversations` array and a `projects`
-- array in the same file. ingest_path() now checks project data and
-- conversation data independently instead of treating a file as one or the
-- other — but source_files.sha256 UNIQUE meant only one row could ever exist
-- per physical file, so the second half of a dual-purpose file's ingest
-- (whichever ran second) saw the sha256 already marked 'ok' from the first
-- half and was silently treated as an already-ingested duplicate, dropping
-- it just as completely as the bug this was meant to fix.
--
-- `adapter` already distinguishes the two purposes for a shared file
-- (e.g. 'grok' vs 'grok_projects', set in ingest.py/_ingest_projects_file) —
-- the fix is to key uniqueness on (sha256, adapter) instead of sha256 alone,
-- so a projects-half row and a conversations-half row of the same file can
-- coexist. SQLite has no ALTER TABLE ... DROP CONSTRAINT, so the table is
-- rebuilt with the same columns and data, id-for-id (conversations.
-- source_file_id references source_files(id) and must keep resolving).
--
-- Built under the NEW name first, then the OLD table is dropped and the new
-- one renamed into place — not the other way around. Renaming the original
-- table away first (source_files -> source_files_old) makes SQLite rewrite
-- conversations' `REFERENCES source_files(id)` clause to point at
-- source_files_old, which then dangles once that table is dropped. Building
-- under a new name and renaming *it* into "source_files" at the end means
-- conversations' FK text (unmodified the whole time) is already correct the
-- moment the name exists again.
PRAGMA foreign_keys=OFF;

CREATE TABLE source_files_new (
  id             INTEGER PRIMARY KEY,
  path           TEXT    NOT NULL,
  sha256         TEXT    NOT NULL,
  platform       TEXT    NOT NULL,
  adapter        TEXT    NOT NULL,
  bytes          INTEGER,
  ingested_at    TEXT    NOT NULL,
  status         TEXT    NOT NULL DEFAULT 'ok',
  error          TEXT,
  kind           TEXT    NOT NULL DEFAULT 'frozen',
  last_seen_at   TEXT,
  root_dir       TEXT,
  first_seen_at  TEXT,
  presence       TEXT    NOT NULL DEFAULT 'present',
  missing_since  TEXT,
  presence_note  TEXT,
  UNIQUE(sha256, adapter)
);

INSERT INTO source_files_new (
  id, path, sha256, platform, adapter, bytes, ingested_at, status, error,
  kind, last_seen_at, root_dir, first_seen_at, presence, missing_since, presence_note
)
SELECT
  id, path, sha256, platform, adapter, bytes, ingested_at, status, error,
  kind, last_seen_at, root_dir, first_seen_at, presence, missing_since, presence_note
FROM source_files;

DROP TABLE source_files;
ALTER TABLE source_files_new RENAME TO source_files;

CREATE INDEX ix_srcfiles_presence ON source_files(presence);
CREATE INDEX ix_sf_kind     ON source_files(kind);
CREATE INDEX ix_sf_root_dir ON source_files(root_dir);
CREATE INDEX ix_sf_sha256   ON source_files(sha256);

PRAGMA foreign_keys=ON;

-- ─────────────── live_roots: an explicit registry of what `ingest --live` refreshes ───────────────
-- Until now the set of live roots was DERIVED: SELECT DISTINCT root_dir FROM
-- source_files WHERE kind = 'live'. root_dir records where a file was found,
-- so every quirk of how a file was once ingested (a nested project folder, a
-- wrong-case path, a staged snapshot marked live) became a phantom live root.
-- This table DECLARES the intention instead. source_files.root_dir stays as
-- provenance only.
--
--   source      platform label shown in output (matches source_files.platform)
--   path        directory `ingest --live` scans directly, `~` allowed. NULL for
--               a store that must be extracted first (SQLite databases).
--   live_store  JSON array of the real live store(s): files, directories or
--               globs, `~` allowed. Staleness is measured against these.
--   extract_cmd script (relative to the project) that snapshots live_store into
--               stage_dir as JSONL. NULL = nothing to extract, scan `path`.
--   stage_dir   where the snapshot lands, relative to the vault root
--               (raw/00_inbox/<source>), so `intake` files it onward.
--   last_extracted_at  when `ingest --live` last snapshotted live_store; staleness
--               is "store changed since then", not "snapshot file is old".
--   active      0 = deliberately retired; skipped by --live, never deleted (D3).
CREATE TABLE live_roots (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,
  path        TEXT,
  live_store  TEXT,
  extract_cmd TEXT,
  stage_dir   TEXT,
  last_extracted_at TEXT,
  active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  note        TEXT,
  created_at  TEXT NOT NULL,
  CHECK (path IS NOT NULL OR (extract_cmd IS NOT NULL AND stage_dir IS NOT NULL AND live_store IS NOT NULL))
);
CREATE UNIQUE INDEX ux_live_roots_path   ON live_roots(path) WHERE path IS NOT NULL;
CREATE UNIQUE INDEX ux_live_roots_source ON live_roots(source) WHERE extract_cmd IS NOT NULL;

-- ─────────────── one spelling per directory ───────────────
-- APFS is case-insensitive: /x/Chatvault and /x/ChatVault are one folder but
-- two strings. Python canonicalises every path before it is stored
-- (chatvault.paths.canonical); this is the database-level backstop so a
-- future typo fails loudly instead of silently registering a second root.
CREATE INDEX ix_sf_root_dir_lower ON source_files(lower(root_dir));

CREATE TRIGGER trg_source_files_root_case_insert
BEFORE INSERT ON source_files
WHEN NEW.root_dir IS NOT NULL AND EXISTS (
  SELECT 1 FROM source_files WHERE lower(root_dir) = lower(NEW.root_dir) AND root_dir <> NEW.root_dir
)
BEGIN
  SELECT RAISE(ABORT, 'source_files.root_dir differs only by letter case from an existing root — use the canonical path (chatvault.paths.canonical)');
END;

CREATE TRIGGER trg_source_files_root_case_update
BEFORE UPDATE OF root_dir ON source_files
WHEN NEW.root_dir IS NOT NULL AND EXISTS (
  SELECT 1 FROM source_files WHERE lower(root_dir) = lower(NEW.root_dir) AND root_dir <> NEW.root_dir
)
BEGIN
  SELECT RAISE(ABORT, 'source_files.root_dir differs only by letter case from an existing root — use the canonical path (chatvault.paths.canonical)');
END;

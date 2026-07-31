-- ─────────────── provenance (Patch 2 / sources) ───────────────
ALTER TABLE source_files ADD COLUMN kind         TEXT NOT NULL DEFAULT 'frozen';  -- frozen | live
ALTER TABLE source_files ADD COLUMN last_seen_at TEXT;
ALTER TABLE source_files ADD COLUMN root_dir     TEXT;
CREATE INDEX ix_sf_kind     ON source_files(kind);
CREATE INDEX ix_sf_root_dir ON source_files(root_dir);

-- ─────────────── audit ───────────────
CREATE TABLE audit_runs (
  id          INTEGER PRIMARY KEY,
  run_at      TEXT NOT NULL,
  verdict     TEXT NOT NULL,        -- pass | warn | fail
  report_json TEXT NOT NULL
);

-- ─────────────── dedupe (Patch 1) ───────────────
CREATE TABLE dedupe_candidates (
  id          INTEGER PRIMARY KEY,
  conv_id_a   INTEGER NOT NULL REFERENCES conversations(id),
  conv_id_b   INTEGER NOT NULL REFERENCES conversations(id),
  reason      TEXT    NOT NULL,     -- head_match_unverified | cross_source | near_identical (reserved, unimplemented)
  confidence  REAL    NOT NULL,     -- 0.0-1.0
  detail_json TEXT,
  status      TEXT    NOT NULL DEFAULT 'open',  -- open | applied | dismissed
  resolved_at TEXT,
  created_at  TEXT    NOT NULL,
  UNIQUE(conv_id_a, conv_id_b)
);
CREATE INDEX ix_dedupe_status ON dedupe_candidates(status);

-- ─────────────── annotations natural-key dedupe (Patch 3) ───────────────
-- Computed in Python (hashing.note_hash) — SQLite has no built-in SHA-256 for
-- a generated column. db.migrate() backfills existing rows after applying
-- this migration; new rows are populated at insert/import time.
ALTER TABLE annotations ADD COLUMN note_hash TEXT;
CREATE INDEX ix_anno_dedupe ON annotations(target_type, target_id, note_hash);

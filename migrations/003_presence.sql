-- ─────────────── presence tracking (Spec v1.2 Part A) ───────────────
ALTER TABLE source_files ADD COLUMN first_seen_at  TEXT;
ALTER TABLE source_files ADD COLUMN presence       TEXT NOT NULL DEFAULT 'present';
       -- present | missing | retired
ALTER TABLE source_files ADD COLUMN missing_since  TEXT;
ALTER TABLE source_files ADD COLUMN presence_note  TEXT;

CREATE INDEX ix_srcfiles_presence ON source_files(presence);

CREATE TABLE reconcile_runs (
  id            INTEGER PRIMARY KEY,
  run_at        TEXT NOT NULL,
  roots_json    TEXT NOT NULL,
  checked       INTEGER NOT NULL,
  still_present INTEGER NOT NULL,
  newly_missing INTEGER NOT NULL,
  reappeared    INTEGER NOT NULL,
  report_json   TEXT NOT NULL
);

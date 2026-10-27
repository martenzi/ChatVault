-- ─────────────── remote live sources: provenance of a machine is a column ───────────────
-- docs/specs/77_SPEC_remote_live_sources.md. The Mac is the only writer; other
-- machines are sources it pulls from over ssh.
--
--   live_roots.host       NULL = this machine; otherwise an ssh alias ('fedora').
--                         Remote paths keep `~` and expand on the remote side.
--   live_roots.last_pulled_at  when `ingest --live --remote` last copied this
--                         row's data from the remote host (shown when UNREACHABLE).
--   source_files.host     the machine the file was read on. Every row that exists
--                         today was read on the Mac: backfilled 'mac' (rewritten,
--                         no row added or removed).
ALTER TABLE live_roots ADD COLUMN host TEXT;
ALTER TABLE live_roots ADD COLUMN last_pulled_at TEXT;
ALTER TABLE source_files ADD COLUMN host TEXT;
UPDATE source_files SET host = 'mac' WHERE host IS NULL;
CREATE INDEX ix_sf_host ON source_files(host);

-- One registration per machine: ~/.claude/projects exists on both.
DROP INDEX ux_live_roots_path;
DROP INDEX ux_live_roots_source;
CREATE UNIQUE INDEX ux_live_roots_path   ON live_roots(COALESCE(host, ''), path) WHERE path IS NOT NULL;
CREATE UNIQUE INDEX ux_live_roots_source ON live_roots(COALESCE(host, ''), source) WHERE extract_cmd IS NOT NULL;

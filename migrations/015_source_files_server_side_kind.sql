-- ─────────────── source_files.kind: add 'server_side' (docs/05_INGESTION_LEDGER.md §5) ───────────────
-- 'live'/'frozen' don't capture a real third category: sources that live on
-- a vendor's server and go stale the moment they're exported — claude_desktop,
-- chatgpt, grok today, gemini/deepseek once ingested. 'frozen' reads as "will
-- never change," which is wrong for these (a fresh export always has more);
-- 'live' reads as "re-ingest picks up new local files automatically," which
-- is also wrong (there's no local directory that grows — a human has to
-- request a new export). server_side names the actual constraint: refresh by
-- requesting a new export, not by re-running ingest against the same files.
--
-- Backfill: every existing claude_desktop/chatgpt/grok source_files row was
-- inserted as 'frozen' (ingest.py's hardcoded default at the time) but is
-- actually server_side by this definition. Scoped to platform, not adapter,
-- so it also catches each platform's *_projects adapter rows (e.g.
-- grok_projects, claude_desktop_projects).
UPDATE source_files
SET kind = 'server_side'
WHERE platform IN ('claude_desktop', 'chatgpt', 'grok');

-- Guard against an unrecognized kind creeping back in, same pattern as
-- migration 013's projects.kind triggers.
CREATE TRIGGER trg_source_files_kind_valid_insert
BEFORE INSERT ON source_files
WHEN NEW.kind NOT IN ('live', 'frozen', 'server_side')
BEGIN
  SELECT RAISE(ABORT, 'invalid source_files.kind — see docs/05_INGESTION_LEDGER.md §5');
END;

CREATE TRIGGER trg_source_files_kind_valid_update
BEFORE UPDATE OF kind ON source_files
WHEN NEW.kind NOT IN ('live', 'frozen', 'server_side')
BEGIN
  SELECT RAISE(ABORT, 'invalid source_files.kind — see docs/05_INGESTION_LEDGER.md §5');
END;

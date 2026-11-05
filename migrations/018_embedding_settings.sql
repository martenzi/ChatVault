-- ─────────────── which settings produced each conversation vector ───────────────
-- representative_text() is tunable (CHATVAULT_EMBED_MAX_CHARS / HEAD_BLOCKS /
-- TAIL_BLOCKS / PER_BLOCK_CHARS). Without a record per row, a half-reindexed
-- vault can't be explained. NULL = embedded before this migration, i.e. with
-- the original first-6-blocks / 3000-char representation.
ALTER TABLE embedding_meta ADD COLUMN settings TEXT;

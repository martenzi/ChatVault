-- ─────────────── follow-up to 013: uid + projects.platform missed the first pass ───────────────
-- Migration 013 renamed conversations.platform and source_files.platform/
-- adapter from claude_web to claude_desktop, but missed two things,
-- discovered only after the fact by comparing real CLI output before/after:
--
-- 1. conversations.uid (built at ingest time as f"{platform}:{native_id}",
--    see hashing.make_uid) still carried the literal "claude_web:" prefix
--    on all 255 rows, unchanged by 013. Meanwhile the renamed adapter
--    (adapters/claude_desktop.py) now computes uid with "claude_desktop:"
--    for anything ingested from here on. Left alone, the next real ingest
--    of a Claude account export would fail to match these rows by uid
--    (the idempotency key) and insert 255 duplicate conversation rows
--    instead of no-opping/updating in place.
--
-- 2. projects.platform still said 'claude_web' on all 27 Claude Desktop
--    project rows (kind='claude_project') — 013 only updated
--    conversations.platform and source_files, not projects.platform.
--    Same uid mismatch risk applies there (projects.uid is also
--    f"{platform}:{native_id}").
--
-- uid has no FK dependents (messages/blocks reference the integer
-- conversations.id, not uid) and this vault has 0 annotations/saved_views
-- referencing a literal uid string, so this rewrite is additive and safe.
UPDATE conversations
SET uid = 'claude_desktop:' || substr(uid, length('claude_web:') + 1)
WHERE platform = 'claude_desktop' AND uid LIKE 'claude_web:%';

UPDATE projects SET platform = 'claude_desktop' WHERE platform = 'claude_web';

UPDATE projects
SET uid = 'claude_desktop:' || substr(uid, length('claude_web:') + 1)
WHERE platform = 'claude_desktop' AND uid LIKE 'claude_web:%';

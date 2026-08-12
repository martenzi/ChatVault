-- ─────────────── rename projects.kind values (docs/20_FIX_naming_project_kinds.md) ───────────────
-- Migration 010 introduced kind as workspace | desktop | folder | scratch.
-- 'folder' was never used (0 rows, ever). The other three names collided
-- with meanings that only became obvious once Grok's own project concept
-- ('grok_project', already correctly named) needed a peer: 'workspace' reads
-- as any project, not specifically a disk directory; 'desktop' is ambiguous
-- with the OS desktop; 'scratch' doesn't say "no project" clearly. Renamed:
--   workspace -> code_workspace  (a project directory a .code-workspace file points at)
--   desktop   -> claude_project  (a project created inside Claude, not a folder)
--   scratch   -> no_project      (an ad-hoc session with no project context)
-- grok_project is unchanged. Additive, reversible (values only, no rows
-- created or destroyed), no dependency on migration 012's source_files
-- rebuild. This is migration 013, not 012 — 012_source_files_dual_purpose.sql
-- already claimed that number (written earlier, not yet applied at the time
-- this one was authored).
UPDATE projects SET kind = 'code_workspace' WHERE kind = 'workspace';
UPDATE projects SET kind = 'claude_project' WHERE kind = 'desktop';
UPDATE projects SET kind = 'no_project'     WHERE kind = 'scratch';

-- Guard against the old (or any unrecognized) vocabulary creeping back in.
CREATE TRIGGER trg_projects_kind_valid_insert
BEFORE INSERT ON projects
WHEN NEW.kind NOT IN ('code_workspace','claude_project','grok_project','no_project')
BEGIN
  SELECT RAISE(ABORT, 'invalid projects.kind — see docs/20_FIX_naming_project_kinds.md');
END;

CREATE TRIGGER trg_projects_kind_valid_update
BEFORE UPDATE OF kind ON projects
WHEN NEW.kind NOT IN ('code_workspace','claude_project','grok_project','no_project')
BEGIN
  SELECT RAISE(ABORT, 'invalid projects.kind — see docs/20_FIX_naming_project_kinds.md');
END;

-- ─────────────── platform rename: claude_web -> claude_desktop ───────────────
-- claude_web's export (Claude account data export) cannot distinguish web UI
-- from Desktop app — same account, same backend, identical records. Most of
-- these 255 conversations are in fact Desktop conversations, and every
-- project the user has created was made inside the Desktop app, so
-- claude_desktop reads consistently alongside claude_project. claude_code is
-- unchanged — it comes from a genuinely different artifact (~/.claude/projects
-- session JSONL), not a labeling choice.
UPDATE conversations SET platform = 'claude_desktop' WHERE platform = 'claude_web';
UPDATE source_files   SET platform = 'claude_desktop', adapter = 'claude_desktop' WHERE platform = 'claude_web';

-- ─────────────── v_model_coverage: per-platform null-model rate ───────────────
-- Replaces the deleted not_found tag (verified: 9,565 links, 100% on
-- model IS NULL messages, all windsurf/manual — duplicated messages.model,
-- covered only 13% of the real null-model population, 82% of links on
-- non-assistant messages where "has no model" was never a meaningful
-- question). model IS NULL is already a fact in messages.model; this view
-- answers it per platform, always current, no tagging needed.
CREATE VIEW v_model_coverage AS
SELECT c.platform,
       COUNT(*) AS assistant_msgs,
       SUM(m.model IS NULL) AS model_missing,
       ROUND(100.0 * SUM(m.model IS NULL) / COUNT(*), 1) AS pct_missing
FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id
WHERE m.role = 'assistant'
GROUP BY c.platform ORDER BY pct_missing DESC;

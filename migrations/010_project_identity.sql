-- ─────────────── project identity (docs/FIX_project_identity.md) ───────────────
-- Promotes the existing `conversations.project` text label into real
-- `projects` rows, keyed on a normalised absolute path rather than a display
-- name — the fix for `latex` and `Latex_VS_Code_Resume_Project` being the
-- same project split into two labels by the exporter. See CONCEPTS_label_
-- tag_project.md for why project (fact) and tag (judgement) are kept apart.
ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'workspace';
      -- desktop | workspace | folder | scratch
ALTER TABLE projects ADD COLUMN path TEXT;          -- normalised absolute dir
ALTER TABLE projects ADD COLUMN path_raw TEXT;      -- original URI, verbatim
ALTER TABLE projects ADD COLUMN ambiguous INTEGER NOT NULL DEFAULT 0;
CREATE INDEX ix_projects_path ON projects(path);

-- Existing rows predate this column and got the 'workspace' default
-- regardless of origin. chatgpt/claude_web have no workspace concept at all
-- (CONCEPTS_label_tag_project.md) — every project row on those platforms
-- came from loader.load_project (a Claude Desktop / ChatGPT project), never
-- from a workspace backfill, so this reclassification is unambiguous.
UPDATE projects SET kind = 'desktop' WHERE platform IN ('claude_web', 'chatgpt');

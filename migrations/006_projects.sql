-- ─────────────── projects (Spec v1.4 REVISED Part B) ───────────────
-- platform is NOT NULL and always displayed: a same-named project on two
-- platforms is two unrelated records, never merged/summed/shown ambiguously.
CREATE TABLE projects (
  id            INTEGER PRIMARY KEY,
  uid           TEXT NOT NULL UNIQUE,      -- '<platform>:<native_id|slug>'
  platform      TEXT NOT NULL,
  native_id     TEXT,
  name          TEXT NOT NULL,
  description   TEXT,
  instructions  TEXT,
  created_at    TEXT,
  raw_json      TEXT,
  excluded      INTEGER NOT NULL DEFAULT 0,
  ingested_at   TEXT NOT NULL
);
CREATE INDEX ix_proj_platform ON projects(platform);

-- Deliberately not populated automatically (spec §B.2): no adapter writes
-- conversations.project_id in this build. Real exports carry no conv->project
-- reference, so this column stays NULL until a future, unhurried decision
-- (AI-assisted tagging reviewed by hand, or a fresh export that includes it).
ALTER TABLE conversations ADD COLUMN project_id INTEGER REFERENCES projects(id);
CREATE INDEX ix_conv_project_id ON conversations(project_id);

CREATE VIEW v_projects AS SELECT * FROM projects WHERE excluded = 0;

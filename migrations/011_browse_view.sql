-- ─────────────── v_browse (docs/DATASETTE_AND_VIEWS_GUIDE.md §2) ───────────────
-- A single SQL view carrying the column order you actually want to look at,
-- with readable dates and the resolved project name. Because it's a real
-- view (not a Datasette canned query), every surface gets it for free —
-- Datasette facets/filters/sort, the CLI, Textual, Neo4j, AI via MCP — and
-- it costs nothing to keep in sync: it's just v_conversations, reshaped.
CREATE VIEW v_browse AS
SELECT
    c.id,
    substr(replace(c.started_at, 'T', ' '), 1, 19)        AS date,
    COALESCE(p.name, c.project, '(no project)')            AS project,
    c.title,
    c.platform,
    c.model_primary                                        AS model,
    (SELECT group_concat(t.name, ', ') FROM tag_links tl
       JOIN tags t ON t.id = tl.tag_id
      WHERE tl.target_type = 'conversation' AND tl.target_id = c.id) AS tags,
    c.message_count                                        AS msgs,
    c.project_id,
    c.uid
FROM v_conversations c
LEFT JOIN projects p ON p.id = c.project_id
ORDER BY c.started_at DESC;

from __future__ import annotations

import sqlite3


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """platform is always shown, never merged across platforms — a
    same-named project on two platforms is two unrelated rows here."""
    return conn.execute(
        """
        SELECT p.id, p.platform, p.name,
               (SELECT COUNT(*) FROM conversations c WHERE c.project_id = p.id) AS convs,
               (SELECT COUNT(*) FROM attachment_links al
                  JOIN attachments a ON a.id = al.attachment_id
                  WHERE al.target_type = 'project' AND al.target_id = p.id AND a.excluded = 0) AS docs
        FROM v_projects p
        ORDER BY p.platform, p.name
        """
    ).fetchall()

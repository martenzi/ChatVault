from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from chatvault import tags as tags_mod

# ─────────────── project identity (docs/FIX_project_identity.md) ───────────────
# Identity is keyed on a normalised absolute path, never on the display name —
# that's what makes 'latex' and 'Latex_VS_Code_Resume_Project' (two exporter
# naming rules for one real project) resolve to a single row, and what makes
# two different folders that happen to share a name stay two separate rows
# (flagged ambiguous, never silently merged). See CONCEPTS_label_tag_project.md.

# Which raw_json field holds the workspace path, per platform. Anything not
# listed here (chatgpt, claude_desktop, hermes) has no path-like project field —
# hermes's `project` is a platform label ('telegram', 'cli'), not a folder —
# so those conversations are left alone: flag, don't guess.
_PATH_FIELD = {
    "cursor": "workspace_uri",
    "vscode": "workspace_uri",
    "cursor_copilot": "workspace_uri",
    "windsurf": "project_path_guess",
    "claude_code": "cwd",
    "openclaw": "cwd",
}

_USERNAME_RE = re.compile(r"^/Users/[^/]+/")
_SCRATCH_RE = re.compile(r"/Workspaces/\d+/workspace\.json$")
_EXTENSION_RE = re.compile(r"\.([A-Za-z0-9]{1,12})$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _raw_path(platform: str, raw_json: str | None) -> str | None:
    field_name = _PATH_FIELD.get(platform)
    if not field_name or not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    return data.get(field_name) or None


@dataclass
class ResolvedIdentity:
    kind: str          # code_workspace | no_project | unresolved
    path: str | None   # normalised absolute dir (None for no_project/unresolved)
    path_raw: str       # original URI, verbatim


def _looks_like_file(path: str) -> bool:
    """True for a trailing segment with a file-extension shape ('main.py',
    '.gitignore', 'notes.tex') — including dotfiles, since the leading dot
    doesn't change that it's a single file, not a directory."""
    segment = PurePosixPath(path).name
    return bool(segment) and bool(_EXTENSION_RE.search(segment))


def _walk_up_for_project_root(normalised_path: str) -> str | None:
    """docs/20_FIX_project_identity.md §3b: Windsurf's `project_path_guess`
    is often the first @-mentioned *file*, not the workspace. Best-effort
    recovery — walk the file's real ancestors (only works when the path
    still exists on this machine, true for anything already migrated to the
    current account) looking for the nearest `.code-workspace` file or
    `.git` directory. Returns None (never an arbitrary ancestor) if nothing
    is found within a bounded number of levels or the path doesn't exist —
    flag, don't guess."""
    fs_path = Path(os.path.expanduser(normalised_path))
    home = Path.home()
    current = fs_path.parent

    for _ in range(10):
        if not current.exists():
            return None
        try:
            entries = list(current.iterdir())
        except OSError:
            return None
        if (current / ".git").is_dir() or any(e.suffix == ".code-workspace" for e in entries):
            try:
                rel = current.relative_to(home)
            except ValueError:
                return str(current)
            return f"~/{rel}" if str(rel) != "." else "~"
        if current == home or current.parent == current:
            return None
        current = current.parent
    return None


def resolve_identity(raw_uri: str) -> ResolvedIdentity:
    """Normalise a raw workspace URI into (kind, path). Never guesses: a
    `.code-workspace` file's project is its containing directory; a Cursor/
    VS Code app-support scratch workspace has no real project at all; and a
    guess that turns out to be a plain file (§3b) either walks up to a real
    project root on disk or is left unresolved rather than keying a project
    on a file path."""
    decoded = unquote(raw_uri)
    normalised = _USERNAME_RE.sub("~/", decoded)

    if _SCRATCH_RE.search(normalised):
        return ResolvedIdentity(kind="no_project", path=None, path_raw=raw_uri)

    if normalised.endswith(".code-workspace"):
        project_dir = str(PurePosixPath(normalised).parent)
        return ResolvedIdentity(kind="code_workspace", path=project_dir, path_raw=raw_uri)

    if _looks_like_file(normalised):
        root = _walk_up_for_project_root(normalised)
        if root:
            return ResolvedIdentity(kind="code_workspace", path=root, path_raw=raw_uri)
        return ResolvedIdentity(kind="unresolved", path=None, path_raw=raw_uri)

    return ResolvedIdentity(kind="code_workspace", path=normalised.rstrip("/"), path_raw=raw_uri)


def _project_name(path: str) -> str:
    return PurePosixPath(path).name or path


@dataclass
class BackfillReport:
    resolved: int = 0
    scratch: int = 0
    unresolved: int = 0
    guessed_unresolved: int = 0  # §3b: file-like guess, no project root found on disk
    ambiguous_projects: int = 0
    projects_created: int = 0


def _get_or_create_project(
    conn: sqlite3.Connection, *, uid: str, platform: str, name: str, kind: str, path: str | None, path_raw: str | None
) -> tuple[int, bool]:
    now = _now()
    existing = conn.execute("SELECT id FROM projects WHERE uid = ?", (uid,)).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE projects SET path = ?, path_raw = ? WHERE id = ?",
            (path, path_raw, existing["id"]),
        )
        return existing["id"], False
    cur = conn.execute(
        """
        INSERT INTO projects (uid, platform, name, kind, path, path_raw, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uid, platform, name, kind, path, path_raw, now),
    )
    return cur.lastrowid, True


def _tag_conv(conn: sqlite3.Connection, conv_id: int, tag_name: str) -> None:
    tag_id = tags_mod.get_or_create_tag(conn, tag_name)
    conn.execute(
        """
        INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
        VALUES (?, 'conversation', ?, 'system', ?)
        ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
        """,
        (tag_id, conv_id, tags_mod.now()),
    )


def backfill(conn: sqlite3.Connection) -> BackfillReport:
    """Promote `conversations.project` labels into real `projects` rows,
    keyed on normalised path. Idempotent — safe to re-run after new ingests;
    re-running never duplicates a project row or a review/* tag."""
    report = BackfillReport()
    # v_conversations, never the base table (docs/21_FIX_scope_consistency.md):
    # a superseded duplicate would otherwise get its own project_id and its
    # own review/* tag_link alongside the live row that replaced it.
    rows = conn.execute(
        """
        SELECT id, platform, project, raw_json FROM v_conversations
        WHERE project IS NOT NULL AND platform IN ({})
        """.format(",".join("?" * len(_PATH_FIELD)))  # noqa: S608
        , list(_PATH_FIELD),
    ).fetchall()

    for row in rows:
        raw_uri = _raw_path(row["platform"], row["raw_json"])
        if not raw_uri:
            _tag_conv(conn, row["id"], "review/project-unresolved")
            report.unresolved += 1
            continue

        identity = resolve_identity(raw_uri)

        if identity.kind == "unresolved":
            conn.execute("UPDATE conversations SET project_id = NULL WHERE id = ?", (row["id"],))
            _tag_conv(conn, row["id"], "review/project-guessed")
            report.guessed_unresolved += 1
            continue

        if identity.kind == "no_project":
            uid = f"{row['platform']}:scratch"
            project_id, created = _get_or_create_project(
                conn, uid=uid, platform=row["platform"], name="(no workspace)",
                kind="no_project", path=None, path_raw=identity.path_raw,
            )
            _tag_conv(conn, row["id"], "review/project-scratch")
            report.scratch += 1
        else:
            uid = f"{row['platform']}:ws:{identity.path}"
            project_id, created = _get_or_create_project(
                conn, uid=uid, platform=row["platform"], name=_project_name(identity.path),
                kind="code_workspace", path=identity.path, path_raw=identity.path_raw,
            )
            report.resolved += 1

        if created:
            report.projects_created += 1
        conn.execute("UPDATE conversations SET project_id = ? WHERE id = ?", (project_id, row["id"]))

    conn.commit()
    report.ambiguous_projects = _flag_ambiguous(conn)
    conn.commit()
    return report


def _flag_ambiguous(conn: sqlite3.Connection) -> int:
    """Same display name, different real path — flagged, never silently
    merged (§7). Scoped to kind='code_workspace' rows: a scratch bucket
    sharing the literal name '(no workspace)' across platforms isn't a
    collision."""
    conn.execute("UPDATE projects SET ambiguous = 0 WHERE kind = 'code_workspace'")
    conn.execute(
        """
        UPDATE projects SET ambiguous = 1
        WHERE kind = 'code_workspace' AND name IN (
            SELECT name FROM projects
            WHERE kind = 'code_workspace' AND path IS NOT NULL
            GROUP BY name HAVING COUNT(DISTINCT path) > 1
        )
        """
    )
    ambiguous_projects = [
        row[0] for row in conn.execute("SELECT id FROM projects WHERE ambiguous = 1").fetchall()
    ]
    for project_id in ambiguous_projects:
        conv_ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM conversations WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]
        for conv_id in conv_ids:
            _tag_conv(conn, conv_id, "review/project-ambiguous")
    return len(ambiguous_projects)


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """platform is always shown, never merged across platforms — a
    same-named project on two platforms is two unrelated rows here."""
    return conn.execute(
        """
        SELECT p.id, p.platform, p.name, p.kind, p.path, p.ambiguous,
               (SELECT COUNT(*) FROM v_conversations c WHERE c.project_id = p.id) AS convs,
               (SELECT COUNT(*) FROM attachment_links al
                  JOIN attachments a ON a.id = al.attachment_id
                  WHERE al.target_type = 'project' AND al.target_id = p.id AND a.excluded = 0) AS docs
        FROM v_projects p
        ORDER BY p.platform, p.name
        """
    ).fetchall()


def review(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every project flagged ambiguous, plus scratch buckets — the set
    `chatvault projects review` shows so nothing odd disappears quietly."""
    return conn.execute(
        """
        SELECT p.id, p.platform, p.name, p.kind, p.path, p.ambiguous,
               (SELECT COUNT(*) FROM v_conversations c WHERE c.project_id = p.id) AS convs
        FROM v_projects p
        WHERE p.ambiguous = 1 OR p.kind = 'no_project'
        ORDER BY p.ambiguous DESC, p.platform, p.name
        """
    ).fetchall()


def merge(conn: sqlite3.Connection, source_id: int, target_id: int) -> int:
    """Moves every conversation from `source_id` onto `target_id`, then
    deletes the now-empty source project row. Your decision, reversible only
    by re-running `backfill` (which re-derives identity from raw_json) —
    never automatic."""
    source = conn.execute("SELECT id FROM projects WHERE id = ?", (source_id,)).fetchone()
    target = conn.execute("SELECT id FROM projects WHERE id = ?", (target_id,)).fetchone()
    if source is None:
        raise ValueError(f"no project with id {source_id}")
    if target is None:
        raise ValueError(f"no project with id {target_id}")
    if source_id == target_id:
        return 0

    cur = conn.execute(
        "UPDATE conversations SET project_id = ? WHERE project_id = ?", (target_id, source_id)
    )
    moved = cur.rowcount
    conn.execute("DELETE FROM projects WHERE id = ?", (source_id,))
    conn.commit()
    return moved


def rename(conn: sqlite3.Connection, project_id: int, new_name: str) -> None:
    row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"no project with id {project_id}")
    conn.execute("UPDATE projects SET name = ? WHERE id = ?", (new_name, project_id))
    conn.commit()

from __future__ import annotations

from chatvault import adapters, attachments, ingest, loader, projects
from tests.conftest import FIXTURES


def _ingest_projects_fixture(conn):
    return ingest.ingest_path(conn, FIXTURES / "claude_web" / "projects.json")


def test_projects_file_is_detected_and_routed_separately_from_conversations():
    mod = adapters.detect_projects(FIXTURES / "claude_web" / "projects.json")
    assert mod is not None
    assert mod.platform == "claude_web"
    # and it must NOT be picked up by the plain conversation sniff
    assert adapters.detect(FIXTURES / "claude_web" / "projects.json") is None


def test_conversations_file_is_not_mistaken_for_a_projects_file():
    assert adapters.detect_projects(FIXTURES / "claude_web" / "conversations.json") is None


def test_ingest_creates_project_rows(conn):
    summary = _ingest_projects_fixture(conn)
    assert summary.totals["inserted"] == 2
    rows = projects.list_projects(conn)
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"Second Brain", "Empty Project"}


def test_project_docs_attached_and_searchable(conn):
    _ingest_projects_fixture(conn)
    row = conn.execute("SELECT id FROM projects WHERE name = 'Second Brain'").fetchone()
    docs = attachments.list_attachments(conn, project=row["id"])
    assert len(docs) == 1
    assert docs[0]["filename"] == "notes.md"

    results = attachments.search(conn, "quokkawombat42")
    assert len(results) == 1
    assert results[0]["filename"] == "notes.md"


def test_project_with_no_docs_still_appears(conn):
    _ingest_projects_fixture(conn)
    rows = projects.list_projects(conn)
    empty = [r for r in rows if r["name"] == "Empty Project"][0]
    assert empty["docs"] == 0


def test_reingest_updates_in_place_not_duplicates(conn):
    _ingest_projects_fixture(conn)
    first_count = len(projects.list_projects(conn))

    # force a second parse+load (bypassing the source_files sha dedup) to
    # exercise load_project's update path directly
    mod = adapters.detect_projects(FIXTURES / "claude_web" / "projects.json")
    for project in mod.parse_projects(FIXTURES / "claude_web" / "projects.json"):
        result = loader.load_project(conn, project, source_file_id=None)
        assert result["action"] == "updated"
    conn.commit()

    assert len(projects.list_projects(conn)) == first_count


def test_same_named_project_on_different_platforms_stays_separate_rows(conn):
    """Spec v1.4 §B.1: platform is NOT NULL and never merged/summed/shown
    ambiguously — two same-named projects on different platforms are two
    unrelated records."""
    for platform in ("claude_web", "chatgpt"):
        loader.load_project(
            conn,
            {
                "platform": platform,
                "native_id": f"{platform}-proj-1",
                "name": "Second Brain",
                "description": None,
                "instructions": None,
                "created_at": "2026-01-01T00:00:00",
                "raw": {},
                "docs": [],
            },
            source_file_id=None,
        )
    conn.commit()

    rows = [r for r in projects.list_projects(conn) if r["name"] == "Second Brain"]
    assert len(rows) == 2
    assert {r["platform"] for r in rows} == {"claude_web", "chatgpt"}
    # every row from v_projects carries a non-null platform, always
    assert all(r["platform"] for r in rows)

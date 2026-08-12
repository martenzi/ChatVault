from __future__ import annotations

from chatvault import adapters, attachments, ingest, loader, projects
from tests.conftest import FIXTURES


def _ingest_projects_fixture(conn):
    return ingest.ingest_path(conn, FIXTURES / "claude_desktop" / "projects.json")


def test_projects_file_is_detected_and_routed_separately_from_conversations():
    mod = adapters.detect_projects(FIXTURES / "claude_desktop" / "projects.json")
    assert mod is not None
    assert mod.platform == "claude_desktop"
    # and it must NOT be picked up by the plain conversation sniff
    assert adapters.detect(FIXTURES / "claude_desktop" / "projects.json") is None


def test_conversations_file_is_not_mistaken_for_a_projects_file():
    assert adapters.detect_projects(FIXTURES / "claude_desktop" / "conversations.json") is None


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
    mod = adapters.detect_projects(FIXTURES / "claude_desktop" / "projects.json")
    for project in mod.parse_projects(FIXTURES / "claude_desktop" / "projects.json"):
        result = loader.load_project(conn, project, source_file_id=None)
        assert result["action"] == "updated"
    conn.commit()

    assert len(projects.list_projects(conn)) == first_count


def test_same_named_project_on_different_platforms_stays_separate_rows(conn):
    """Spec v1.4 §B.1: platform is NOT NULL and never merged/summed/shown
    ambiguously — two same-named projects on different platforms are two
    unrelated records."""
    for platform in ("claude_desktop", "chatgpt"):
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
    assert {r["platform"] for r in rows} == {"claude_desktop", "chatgpt"}
    # every row from v_projects carries a non-null platform, always
    assert all(r["platform"] for r in rows)


def test_convs_count_excludes_superseded_and_excluded_conversations(conn):
    """The convs column must scope through v_conversations, same as docs
    already scopes through excluded attachments — a superseded or excluded
    conversation shouldn't inflate the count a user sees."""
    from chatvault import curation, dedupe
    from chatvault.adapters.base import make_block, make_conversation, make_message

    result = loader.load_project(
        conn,
        {
            "platform": "synthetic", "native_id": "proj-1", "name": "Test Project",
            "description": None, "instructions": None, "created_at": "2026-01-01T00:00:00",
            "raw": {}, "docs": [],
        },
        source_file_id=None,
    )
    project_id = result["project_id"]

    def _load(native_id, started_at):
        cir = make_conversation(
            platform="synthetic", native_id=native_id, title=native_id, project=None,
            started_at=started_at, ended_at=started_at, source_path="synthetic", raw={},
            messages=[make_message(0, "user", None, started_at, [make_block(0, "text", "hi", raw={})], raw={})],
        )
        return loader.load_conversation(conn, cir, source_file_id=None)

    r1 = _load("q1", "2026-01-01T00:00:00")
    r2 = _load("q2", "2026-01-02T00:00:00")
    r3 = _load("q3", "2026-01-03T00:00:00")
    conn.execute(
        "UPDATE conversations SET project_id = ? WHERE id IN (?, ?, ?)",
        (project_id, r1.conv_id, r2.conv_id, r3.conv_id),
    )
    conn.commit()

    dedupe.supersede(conn, r1.uid, r2.uid)
    curation.exclude(conn, "conversation", f"id = {r3.conv_id}", "test exclusion")

    row = [r for r in projects.list_projects(conn) if r["name"] == "Test Project"][0]
    assert row["convs"] == 1


def test_backfill_ignores_superseded_conversations(conn):
    """docs/21_FIX_scope_consistency.md's bug class, found again in
    projects.backfill() itself: it must read v_conversations, not the base
    table — otherwise a superseded duplicate gets its own project_id and its
    own review/* tag_link alongside the live row that replaced it."""
    from chatvault import dedupe
    from chatvault.adapters.base import make_block, make_conversation, make_message

    def _load(native_id, started_at):
        cir = make_conversation(
            platform="cursor", native_id=native_id, title=native_id, project="myproj",
            started_at=started_at, ended_at=started_at, source_path="synthetic",
            raw={"workspace_uri": "/Users/tester/dev/myproj.code-workspace"},
            messages=[make_message(0, "user", None, started_at, [make_block(0, "text", "hi", raw={})], raw={})],
        )
        return loader.load_conversation(conn, cir, source_file_id=None)

    old = _load("dup1", "2026-01-01T00:00:00")
    new = _load("dup2", "2026-01-02T00:00:00")
    conn.commit()
    dedupe.supersede(conn, old.uid, new.uid)

    report = projects.backfill(conn)
    assert report.resolved == 1  # only the live replacement, not the superseded original

    old_row = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (old.conv_id,)).fetchone()
    assert old_row["project_id"] is None

    new_row = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (new.conv_id,)).fetchone()
    assert new_row["project_id"] is not None

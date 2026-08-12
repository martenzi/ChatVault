from __future__ import annotations

from chatvault import loader, tags
from chatvault.adapters.base import make_block, make_conversation, make_message


def _load(conn, native_id, title, started_at):
    cir = make_conversation(
        platform="synthetic", native_id=native_id, title=title, project=None,
        started_at=started_at, ended_at=started_at, source_path="synthetic", raw={},
        messages=[make_message(0, "user", None, started_at, [make_block(0, "text", "hello", raw={})], raw={})],
    )
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result


TAXONOMY_YAML = """
tech:
  macos:
    package-managers: [npm, homebrew]
    system-config:
  containers: [docker, podman]

purpose: [troubleshooting, learning]
"""


# ─────────────── tag browsing counts (docs/21_FIX_scope_consistency.md) ───────────────


def test_tag_tree_excludes_superseded_conversations(conn):
    """own-count must scope through v_conversations, same bug class as
    embed --status and projects list — a tag_link on a superseded duplicate
    must not inflate the count `tag tree`/`tag stats` show, or disagree with
    what `tag show` actually lists."""
    from chatvault import dedupe

    old = _load(conn, "sup1", "conv", "2026-01-01T00:00:00")
    new = _load(conn, "sup2", "conv", "2026-01-02T00:00:00")
    tags.tag_add(conn, "tech/ai", f"id IN ({old.conv_id}, {new.conv_id})")
    dedupe.supersede(conn, old.uid, new.uid)

    roots = tags.tag_tree(conn)
    assert tags.cumulative_count(roots["tech"].children["ai"]) == 1

    info = tags.stats(conn)
    assert info["tagged_convs"] == 1


# ─────────────── tag import-tree from YAML ───────────────


def test_import_tree_creates_every_node_as_a_tag(conn, tmp_path):
    path = tmp_path / "taxonomy.yml"
    path.write_text(TAXONOMY_YAML)

    names = tags.import_tree(conn, path)
    expected = {
        "tech", "tech/macos", "tech/macos/package-managers",
        "tech/macos/package-managers/npm", "tech/macos/package-managers/homebrew",
        "tech/macos/system-config", "tech/containers",
        "tech/containers/docker", "tech/containers/podman",
        "purpose", "purpose/troubleshooting", "purpose/learning",
    }
    assert set(names) == expected
    for name in expected:
        assert conn.execute("SELECT 1 FROM tags WHERE name = ?", (name,)).fetchone() is not None


def test_import_tree_creates_no_tag_links(conn, tmp_path):
    path = tmp_path / "taxonomy.yml"
    path.write_text(TAXONOMY_YAML)
    tags.import_tree(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 0


def test_import_tree_dry_run_writes_nothing(conn, tmp_path):
    path = tmp_path / "taxonomy.yml"
    path.write_text(TAXONOMY_YAML)
    names = tags.import_tree(conn, path, dry_run=True)
    assert len(names) > 0
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0


def test_import_tree_is_idempotent(conn, tmp_path):
    path = tmp_path / "taxonomy.yml"
    path.write_text(TAXONOMY_YAML)
    tags.import_tree(conn, path)
    before = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    tags.import_tree(conn, path)
    after = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    assert before == after


# ─────────────── tag rename ───────────────


def test_rename_moves_exact_tag_preserving_links(conn):
    r1 = _load(conn, "a1", "conv", "2026-01-01T00:00:00")
    tags.tag_add(conn, "tech/ai", f"id = {r1.conv_id}")

    n = tags.rename(conn, "tech/ai", "tech/llm")
    assert n == 1
    assert conn.execute("SELECT 1 FROM tags WHERE name = 'tech/ai'").fetchone() is None
    link = conn.execute(
        "SELECT 1 FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = 'tech/llm' AND tl.target_id = ?",
        (r1.conv_id,),
    ).fetchone()
    assert link is not None


def test_rename_moves_whole_branch(conn):
    r1 = _load(conn, "b1", "conv", "2026-01-01T00:00:00")
    tags.tag_add(conn, "tech/ai/agents", f"id = {r1.conv_id}")
    tags.get_or_create_tag(conn, "tech/ai")
    tags.get_or_create_tag(conn, "tech/ai/prompting")

    n = tags.rename(conn, "tech/ai", "tech/llm")
    assert n == 3  # tech/ai, tech/ai/agents, tech/ai/prompting
    names = {r[0] for r in conn.execute("SELECT name FROM tags WHERE name LIKE 'tech/%'")}
    assert names == {"tech/llm", "tech/llm/agents", "tech/llm/prompting"}


def test_rename_does_not_affect_unrelated_siblings(conn):
    tags.get_or_create_tag(conn, "tech/ai")
    tags.get_or_create_tag(conn, "tech/aiml-unrelated")  # shares prefix 'tech/ai' as a string, not a path segment

    tags.rename(conn, "tech/ai", "tech/llm")
    assert conn.execute("SELECT 1 FROM tags WHERE name = 'tech/aiml-unrelated'").fetchone() is not None


def test_rename_merges_into_existing_destination(conn):
    r1 = _load(conn, "c1", "conv one", "2026-01-01T00:00:00")
    r2 = _load(conn, "c2", "conv two", "2026-01-02T00:00:00")
    tags.tag_add(conn, "tech/ai", f"id = {r1.conv_id}")
    tags.tag_add(conn, "tech/llm", f"id = {r2.conv_id}")

    tags.rename(conn, "tech/ai", "tech/llm")

    assert conn.execute("SELECT 1 FROM tags WHERE name = 'tech/ai'").fetchone() is None
    llm_tag_id = conn.execute("SELECT id FROM tags WHERE name = 'tech/llm'").fetchone()[0]
    linked = {
        r[0] for r in conn.execute("SELECT target_id FROM tag_links WHERE tag_id = ?", (llm_tag_id,))
    }
    assert linked == {r1.conv_id, r2.conv_id}


# ─────────────── tag merge ───────────────


def test_merge_moves_links_and_deletes_source(conn):
    r1 = _load(conn, "d1", "conv one", "2026-01-01T00:00:00")
    r2 = _load(conn, "d2", "conv two", "2026-01-02T00:00:00")
    tags.tag_add(conn, "tech/nodejs", f"id = {r1.conv_id}")
    tags.tag_add(conn, "tech/node", f"id = {r2.conv_id}")

    n = tags.merge(conn, "tech/nodejs", "tech/node")
    assert n == 1
    assert conn.execute("SELECT 1 FROM tags WHERE name = 'tech/nodejs'").fetchone() is None
    target_id = conn.execute("SELECT id FROM tags WHERE name = 'tech/node'").fetchone()[0]
    linked = {r[0] for r in conn.execute("SELECT target_id FROM tag_links WHERE tag_id = ?", (target_id,))}
    assert linked == {r1.conv_id, r2.conv_id}


def test_merge_does_not_duplicate_an_existing_link_on_the_target(conn):
    r1 = _load(conn, "e1", "conv", "2026-01-01T00:00:00")
    tags.tag_add(conn, "tech/nodejs", f"id = {r1.conv_id}")
    tags.tag_add(conn, "tech/node", f"id = {r1.conv_id}")  # already tagged with both

    n = tags.merge(conn, "tech/nodejs", "tech/node")
    assert n == 0  # nothing NEW moved — the link already existed on the target
    target_id = conn.execute("SELECT id FROM tags WHERE name = 'tech/node'").fetchone()[0]
    count = conn.execute("SELECT COUNT(*) FROM tag_links WHERE tag_id = ?", (target_id,)).fetchone()[0]
    assert count == 1  # no duplicate row


def test_merge_does_not_recurse_into_children(conn):
    tags.get_or_create_tag(conn, "tech/ai")
    tags.get_or_create_tag(conn, "tech/ai/agents")
    tags.get_or_create_tag(conn, "tech/llm")

    tags.merge(conn, "tech/ai", "tech/llm")
    # the child tag is untouched — merge is not rename
    assert conn.execute("SELECT 1 FROM tags WHERE name = 'tech/ai/agents'").fetchone() is not None


def test_merge_raises_for_unknown_tags(conn):
    tags.get_or_create_tag(conn, "real/tag")
    try:
        tags.merge(conn, "does/not/exist", "real/tag")
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        tags.merge(conn, "real/tag", "also/missing")
        assert False, "expected ValueError"
    except ValueError:
        pass

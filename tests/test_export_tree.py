from __future__ import annotations

from pathlib import Path

from chatvault import export, loader, tags
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


# ─────────────── tree_relative_dir ───────────────


def test_tree_relative_dir_strips_double_star_prefix():
    assert export.tree_relative_dir("tech/**", "tech/macos/npm") == Path("macos/npm")


def test_tree_relative_dir_strips_single_star_prefix():
    assert export.tree_relative_dir("tech/*", "tech/macos") == Path("macos")


def test_tree_relative_dir_bare_root_tag_goes_to_out_root():
    assert export.tree_relative_dir("tech/**", "tech") == Path(".")


def test_tree_relative_dir_exact_tag_with_no_wildcard():
    assert export.tree_relative_dir("tech/macos/npm", "tech/macos/npm") == Path(".")


def test_tree_relative_dir_multi_segment_project_prefix():
    assert export.tree_relative_dir(
        "project/claude-web/**", "project/claude-web/PSA/troubleshooting"
    ) == Path("PSA/troubleshooting")


# ─────────────── get_conversations_for_tree ───────────────


def test_get_conversations_for_tree_matches_prefix_at_any_depth(conn):
    r1 = _load(conn, "a1", "conv one", "2026-01-01T00:00:00")
    r2 = _load(conn, "a2", "conv two", "2026-01-02T00:00:00")
    r3 = _load(conn, "a3", "unrelated", "2026-01-03T00:00:00")
    tags.tag_add(conn, "tech/macos", f"id = {r1.conv_id}")
    tags.tag_add(conn, "tech/macos/npm", f"id = {r2.conv_id}")
    tags.tag_add(conn, "purpose/reference", f"id = {r3.conv_id}")

    rows = export.get_conversations_for_tree(conn, "tech/**")
    ids = {r["id"] for r in rows}
    assert ids == {r1.conv_id, r2.conv_id}


def test_get_conversations_for_tree_one_row_per_matching_tag(conn):
    """A conversation with two tags under the same tree appears twice —
    once per matched tag — so it lands in both resulting subfolders."""
    r1 = _load(conn, "b1", "multi-tagged", "2026-01-01T00:00:00")
    tags.tag_add(conn, "tech/macos/npm", f"id = {r1.conv_id}")
    tags.tag_add(conn, "tech/containers/docker", f"id = {r1.conv_id}")

    rows = export.get_conversations_for_tree(conn, "tech/**")
    matching = [r for r in rows if r["id"] == r1.conv_id]
    assert len(matching) == 2
    assert {r["matched_tag"] for r in matching} == {"tech/macos/npm", "tech/containers/docker"}


def test_get_conversations_for_tree_no_matches_returns_empty(conn):
    _load(conn, "c1", "conv", "2026-01-01T00:00:00")
    rows = export.get_conversations_for_tree(conn, "tech/**")
    assert rows == []


def test_get_conversations_for_tree_excludes_conversations_outside_prefix(conn):
    r1 = _load(conn, "d1", "in tree", "2026-01-01T00:00:00")
    r2 = _load(conn, "d2", "out of tree", "2026-01-02T00:00:00")
    tags.tag_add(conn, "tech/macos", f"id = {r1.conv_id}")
    tags.tag_add(conn, "tech-unrelated-tag", f"id = {r2.conv_id}")  # shares string prefix, not a path segment

    rows = export.get_conversations_for_tree(conn, "tech/**")
    ids = {r["id"] for r in rows}
    assert ids == {r1.conv_id}


# ─────────────── acceptance: tree export produces real nested folders on disk ───────────────


def test_tree_export_writes_nested_folder_structure(conn, tmp_path):
    r1 = _load(conn, "e1", "npm conv", "2026-01-01T00:00:00")
    r2 = _load(conn, "e2", "docker conv", "2026-01-02T00:00:00")
    tags.tag_add(conn, "tech/macos/npm", f"id = {r1.conv_id}")
    tags.tag_add(conn, "tech/containers/docker", f"id = {r2.conv_id}")

    out_dir = tmp_path / "ByTopic"
    for row in export.get_conversations_for_tree(conn, "tech/**"):
        content = export.render_profile(conn, row, profile="read")
        conv = dict(row)
        conv["uid_short"] = conv["uid"].split(":")[-1][:12]
        target_dir = out_dir / export.tree_relative_dir("tech/**", row["matched_tag"])
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / export.filename_for(conv, "read")).write_text(content)

    npm_files = list((out_dir / "macos" / "npm").glob("*.md"))
    docker_files = list((out_dir / "containers" / "docker").glob("*.md"))
    assert len(npm_files) == 1
    assert len(docker_files) == 1
    assert "npm conv" in npm_files[0].read_text()
    assert "docker conv" in docker_files[0].read_text()


def test_tree_export_acceptance_two_independent_views(conn, tmp_path):
    """Spec §10: the same conversation appears in both a topic-tree export
    and a project-tree export, independently — proves 'one conversation,
    many places' without duplicating the underlying data (only the
    generated markdown view, which is disposable by design)."""
    r1 = _load(conn, "f1", "shared conv", "2026-01-01T00:00:00")
    tags.tag_add(conn, "tech/macos/npm", f"id = {r1.conv_id}")
    tags.tag_add(conn, "project/claude-web/psa", f"id = {r1.conv_id}")

    def _export_tree(tree_prefix, out_dir):
        for row in export.get_conversations_for_tree(conn, tree_prefix):
            content = export.render_profile(conn, row, profile="read")
            conv = dict(row)
            conv["uid_short"] = conv["uid"].split(":")[-1][:12]
            target_dir = out_dir / export.tree_relative_dir(tree_prefix, row["matched_tag"])
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / export.filename_for(conv, "read")).write_text(content)

    by_topic = tmp_path / "ByTopic"
    by_project = tmp_path / "ByProject"
    _export_tree("tech/**", by_topic)
    _export_tree("project/**", by_project)

    topic_files = list((by_topic / "macos" / "npm").glob("*.md"))
    project_files = list((by_project / "claude-web" / "psa").glob("*.md"))
    assert len(topic_files) == 1
    assert len(project_files) == 1
    assert topic_files[0].read_text() == project_files[0].read_text()  # same conversation, same content
    assert r1.conv_id  # sanity

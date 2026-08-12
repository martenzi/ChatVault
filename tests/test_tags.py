from __future__ import annotations

from chatvault import browse, ingest, loader, search, tags
from chatvault.adapters.base import make_block, make_conversation, make_message


def _load(conn, cir):
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result


def _cir(native_id, title, started_at, text="hello"):
    return make_conversation(
        platform="synthetic", native_id=native_id, title=title, project=None,
        started_at=started_at, ended_at=started_at, source_path="synthetic", raw={},
        messages=[make_message(0, "user", None, started_at, [make_block(0, "text", text, raw={})], raw={})],
    )


# ─────────────── hierarchical tag queries (acceptance: prefix query) ───────────────


def test_prefix_query_matches_exact_and_nested(conn):
    r1 = _load(conn, _cir("a1", "conv one", "2026-01-01T00:00:00"))
    r2 = _load(conn, _cir("a2", "conv two", "2026-01-02T00:00:00"))
    r3 = _load(conn, _cir("a3", "conv three", "2026-01-03T00:00:00"))

    tags.tag_add(conn, "tech/macos", "id = ?".replace("?", str(r1.conv_id)), source="manual")
    tags.tag_add(conn, "tech/macos/npm", "id = ?".replace("?", str(r2.conv_id)), source="manual")
    tags.tag_add(conn, "tech/containers/docker", "id = ?".replace("?", str(r3.conv_id)), source="manual")

    rows, total = browse.find(conn, tag="tech/macos/**")
    assert total == 2
    assert {r["id"] for r in rows} == {r1.conv_id, r2.conv_id}


def test_exact_tag_query_still_works_without_wildcard(conn):
    r1 = _load(conn, _cir("b1", "conv one", "2026-01-01T00:00:00"))
    r2 = _load(conn, _cir("b2", "conv two", "2026-01-02T00:00:00"))
    tags.tag_add(conn, "tech/macos", f"id = {r1.conv_id}", source="manual")
    tags.tag_add(conn, "tech/macos/npm", f"id = {r2.conv_id}", source="manual")

    rows, total = browse.find(conn, tag="tech/macos")
    assert total == 1
    assert rows[0]["id"] == r1.conv_id


def test_stats_by_tag_with_prefix(conn):
    r1 = _load(conn, _cir("c1", "conv one", "2026-01-01T00:00:00"))
    r2 = _load(conn, _cir("c2", "conv two", "2026-01-02T00:00:00"))
    r3 = _load(conn, _cir("c3", "conv three", "2026-01-03T00:00:00"))
    tags.tag_add(conn, "tech/macos/npm", f"id = {r1.conv_id}", source="manual")
    tags.tag_add(conn, "tech/containers/docker", f"id = {r2.conv_id}", source="manual")
    tags.tag_add(conn, "purpose/troubleshooting", f"id = {r3.conv_id}", source="manual")

    rows = search.stats(conn, "tag", prefix="tech/**")
    names = {r["key"] for r in rows}
    assert names == {"tech/macos/npm", "tech/containers/docker"}

    all_rows = search.stats(conn, "tag")
    assert {r["key"] for r in all_rows} == {"tech/macos/npm", "tech/containers/docker", "purpose/troubleshooting"}


# ─────────────── multi-tree (acceptance: one conversation, several trees) ───────────────


def test_multi_tree_tags_all_findable_independently(conn):
    r = _load(conn, _cir("d1", "npm broke docker", "2026-01-01T00:00:00"))
    tags.tag_add(conn, "tech/macos/npm", f"id = {r.conv_id}", source="manual")
    tags.tag_add(conn, "project/claude-web/psa", f"id = {r.conv_id}", source="manual")
    tags.tag_add(conn, "purpose/troubleshooting", f"id = {r.conv_id}", source="manual")

    for query in ("tech/macos/**", "project/claude-web/**", "purpose/troubleshooting"):
        rows, total = browse.find(conn, tag=query)
        assert total == 1
        assert rows[0]["id"] == r.conv_id


# ─────────────── tag import-from-folders (acceptance: folder import + unmatched reporting) ───────────────


def test_import_from_folders_matches_by_title_and_date(conn, tmp_path):
    r = _load(conn, _cir("e1", "Flexget Access on macOS", "2025-07-28T11:26:18Z"))

    root = tmp_path / "folders"
    (root / "tech" / "macos").mkdir(parents=True)
    (root / "tech" / "macos" / "2025-07-28--Flexget Access on macOS.md").write_text("x")

    report = tags.import_from_folders(conn, root)
    assert len(report.matched) == 1
    assert report.matched[0]["conv_uid"] == r.uid
    assert report.matched[0]["tag"] == "tech/macos/flexget-access-on-macos"
    assert report.unmatched == []

    links = conn.execute(
        "SELECT source FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = ? AND tl.target_id = ?",
        ("tech/macos/flexget-access-on-macos", r.conv_id),
    ).fetchone()
    assert links["source"] == "folder_import"


def test_import_from_folders_reports_unmatched_files_not_silently_dropped(conn, tmp_path):
    root = tmp_path / "folders"
    root.mkdir()
    (root / "nothing-like-this-exists-anywhere.md").write_text("x")

    report = tags.import_from_folders(conn, root)
    assert report.matched == []
    assert len(report.unmatched) == 1
    assert report.unmatched[0]["file"] == "nothing-like-this-exists-anywhere.md"
    assert "reason" in report.unmatched[0]


def test_import_from_folders_reports_ambiguous_matches_as_unmatched(conn, tmp_path):
    _load(conn, _cir("f1", "Setup guide", "2026-01-01T00:00:00"))
    _load(conn, _cir("f2", "Setup guide", "2026-06-01T00:00:00"))

    root = tmp_path / "folders"
    root.mkdir()
    (root / "Setup guide.md").write_text("x")

    report = tags.import_from_folders(conn, root)
    assert report.matched == []
    assert len(report.unmatched) == 1
    assert "ambiguous" in report.unmatched[0]["reason"]


def test_import_from_folders_dry_run_writes_nothing(conn, tmp_path):
    _load(conn, _cir("g1", "conv one", "2026-01-01T00:00:00"))
    root = tmp_path / "folders"
    root.mkdir()
    (root / "conv one.md").write_text("x")

    report = tags.import_from_folders(conn, root, dry_run=True)
    assert len(report.matched) == 1
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 0


def test_import_from_folders_prefix_is_prepended(conn, tmp_path):
    r = _load(conn, _cir("h1", "conv one", "2026-01-01T00:00:00"))
    root = tmp_path / "folders"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "conv one.md").write_text("x")

    report = tags.import_from_folders(conn, root, prefix="project/claude-web")
    assert report.matched[0]["tag"] == "project/claude-web/sub/conv-one"
    assert conn.execute(
        "SELECT 1 FROM tags WHERE name = 'project/claude-web/sub/conv-one'"
    ).fetchone() is not None
    assert r.conv_id  # sanity


# ─────────────── tag rules (acceptance: auto-apply on ingest, source='rule') ───────────────


def test_rule_apply_backfills_matching_conversations(conn):
    r1 = _load(conn, _cir("i1", "docker issue", "2026-01-01T00:00:00", text="my dockerfile is broken"))
    r2 = _load(conn, _cir("i2", "unrelated", "2026-01-02T00:00:00", text="nothing to do with containers"))

    tags.rule_add(conn, "tech/containers/docker", "blocks.text LIKE '%docker%'", min_hits=1)
    n = tags.apply_rules(conn)
    assert n == 1

    rows, total = browse.find(conn, tag="tech/containers/docker")
    assert total == 1
    assert rows[0]["id"] == r1.conv_id

    link = conn.execute(
        "SELECT source FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = ? AND tl.target_id = ?",
        ("tech/containers/docker", r1.conv_id),
    ).fetchone()
    assert link["source"] == "rule"
    assert r2.conv_id  # sanity, confirms the negative case had a real row to not-match


def test_rule_min_hits_threshold(conn):
    _load(conn, _cir("j1", "one mention", "2026-01-01T00:00:00", text="docker once"))
    r2 = _load(conn, _cir("j2", "three mentions", "2026-01-02T00:00:00"))
    # give r2 three separate blocks mentioning docker
    cir = make_conversation(
        platform="synthetic", native_id="j2b", title="three mentions v2", project=None,
        started_at="2026-01-03T00:00:00", ended_at="2026-01-03T00:00:00", source_path="synthetic", raw={},
        messages=[make_message(
            0, "user", None, "2026-01-03T00:00:00",
            [make_block(0, "text", "docker", raw={}), make_block(1, "text", "docker again", raw={}), make_block(2, "text", "docker once more", raw={})],
            raw={},
        )],
    )
    r3 = _load(conn, cir)

    tags.rule_add(conn, "tech/docker", "blocks.text LIKE '%docker%'", min_hits=3)
    tags.apply_rules(conn)

    rows, total = browse.find(conn, tag="tech/docker")
    assert total == 1
    assert rows[0]["id"] == r3.conv_id
    assert r2.conv_id  # sanity


def test_ingest_auto_applies_rules_to_newly_inserted_conversations(conn):
    from chatvault.adapters.base import make_conversation as mc, make_message as mm, make_block as mb  # noqa: N813

    tags.rule_add(conn, "purpose/troubleshooting", "blocks.text LIKE '%broken%'", min_hits=1)

    cir = mc(
        platform="claude_desktop", native_id="auto-1", title="a broken thing", project=None,
        started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:00", source_path="synthetic", raw={},
        messages=[mm(0, "user", None, "2026-01-01T00:00:00", [mb(0, "text", "this is broken", raw={})], raw={})],
    )
    load_result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()

    n = tags.apply_rules(conn, conv_ids=[load_result.conv_id])
    assert n == 1

    rows, total = browse.find(conn, tag="purpose/troubleshooting")
    assert total == 1
    assert rows[0]["id"] == load_result.conv_id


def test_ingest_path_end_to_end_auto_applies_rule(conn, tmp_path):
    """Full ingest.ingest_path integration — not just the loader/apply_rules
    functions in isolation. Spec v1.5 §9 item 5 / §10: 'a new ingest
    auto-applies matching rules with source=rule'."""
    tags.rule_add(conn, "tech/health", "blocks.text LIKE '%itching%'", min_hits=1)

    fixture = tmp_path / "conv.json"
    fixture.write_text(
        """
        [{
          "uuid": "rule-ingest-1",
          "name": "skin thing",
          "created_at": "2026-01-01T00:00:00Z",
          "updated_at": "2026-01-01T00:00:00Z",
          "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": "itching skin",
             "created_at": "2026-01-01T00:00:00Z",
             "content": [{"type": "text", "text": "itching skin, help"}]}
          ]
        }]
        """
    )

    ingest.ingest_path(conn, fixture)

    rows, total = browse.find(conn, tag="tech/health")
    assert total == 1
    link = conn.execute(
        "SELECT source FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = 'tech/health'"
    ).fetchone()
    assert link["source"] == "rule"


def test_rule_list_and_remove(conn):
    rule_id = tags.rule_add(conn, "x/y", "blocks.text LIKE '%z%'", min_hits=1)
    assert len(tags.rule_list(conn)) == 1
    assert tags.rule_remove(conn, rule_id) is True
    assert tags.rule_list(conn) == []
    assert tags.rule_remove(conn, rule_id) is False


# ─────────────── reversibility (acceptance: bulk-applied tags removable in bulk) ───────────────


def test_folder_import_tags_are_bulk_reversible_by_source(conn, tmp_path):
    r = _load(conn, _cir("k1", "conv one", "2026-01-01T00:00:00"))
    root = tmp_path / "folders"
    root.mkdir()
    (root / "conv one.md").write_text("x")
    tags.import_from_folders(conn, root)

    rows, total = browse.find(conn, tag="conv-one")
    assert total == 1

    n = tags.untag(conn, "conv-one", source="folder_import")
    assert n == 1
    rows, total = browse.find(conn, tag="conv-one")
    assert total == 0
    assert r.conv_id  # sanity


def test_rule_tags_are_bulk_reversible_by_source(conn):
    r1 = _load(conn, _cir("l1", "one", "2026-01-01T00:00:00", text="docker"))
    r2 = _load(conn, _cir("l2", "two", "2026-01-02T00:00:00", text="docker"))
    tags.rule_add(conn, "tech/docker", "blocks.text LIKE '%docker%'", min_hits=1)
    tags.apply_rules(conn)

    rows, total = browse.find(conn, tag="tech/docker")
    assert total == 2

    n = tags.untag(conn, "tech/docker", source="rule")
    assert n == 2
    rows, total = browse.find(conn, tag="tech/docker")
    assert total == 0
    assert r1.conv_id and r2.conv_id  # sanity


def test_untag_with_source_filter_does_not_touch_other_sources(conn):
    r = _load(conn, _cir("m1", "conv", "2026-01-01T00:00:00"))
    tags.tag_add(conn, "shared/tag", f"id = {r.conv_id}", source="manual")
    tags.tag_add(conn, "shared/tag", f"id = {r.conv_id}", source="rule")  # different link would need a different target normally, but tag_id/target dedupe by PK

    # only one link can exist per (tag_id, target_type, target_id) — the
    # second tag_add is a no-op due to the unique constraint, so 'manual' wins
    n = tags.untag(conn, "shared/tag", source="rule")
    assert n == 0
    rows, total = browse.find(conn, tag="shared/tag")
    assert total == 1

    n2 = tags.untag(conn, "shared/tag", source="manual")
    assert n2 == 1

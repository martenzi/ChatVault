from __future__ import annotations

import itertools
import json
import sqlite3

from chatvault import shapes

_uid_counter = itertools.count()


# ────────────────────────── walk_paths / normalisation ──────────────────────────


def test_walk_paths_flat_dict():
    paths = dict(shapes.walk_paths({"a": 1, "b": "x"}))
    assert paths == {"$.a": 1, "$.b": "x"}


def test_walk_paths_nested_dict_and_list():
    rec = {"result": {"metadata": {"renderedUserMessage": "hello"}}, "items": [{"text": "a"}, {"text": "b"}]}
    paths = dict(shapes.walk_paths(rec))
    assert paths["$.result.metadata.renderedUserMessage"] == "hello"
    assert paths["$.items[0].text"] == "a"
    assert paths["$.items[1].text"] == "b"


def test_walk_paths_empty_containers_are_leaves():
    rec = {"a": [], "b": {}}
    paths = dict(shapes.walk_paths(rec))
    assert paths["$.a"] == []
    assert paths["$.b"] == {}


def test_normalise_path_collapses_list_indices():
    assert shapes._normalise_path("$.items[0].text") == "$.items[].text"
    assert shapes._normalise_path("$.items[3].text") == "$.items[].text"
    assert shapes._normalise_path("$.a.b") == "$.a.b"


# ────────────────────────── PathStats aggregation ──────────────────────────


def test_path_stats_records_types_and_lengths():
    stats = shapes.PathStats(path="$.x")
    stats.record("hello")
    stats.record("worldly")
    stats.record(None)
    assert stats.count == 3
    assert stats.types["str"] == 2
    assert stats.types["null"] == 1
    assert stats.total_chars == len("hello") + len("worldly")
    assert stats.min_len == 5
    assert stats.max_len == 7
    assert stats.example == "hello"


def test_shape_signature_includes_discriminators():
    sig = shapes.shape_signature({"role": "user", "kind": "text", "text": "hi", "other": 1})
    assert "keys=kind,other,role,text" in sig
    assert "role='user'" in sig
    assert "kind='text'" in sig


# ────────────────────────── audit_blocks against a synthetic DB ──────────────────────────


def _insert_block(conn: sqlite3.Connection, *, platform: str, role: str, btype: str, raw: dict | str | None) -> int:
    conn.execute(
        "INSERT INTO conversations (uid, platform, ingested_at) VALUES (?, ?, datetime('now'))",
        (f"uid-{platform}-{role}-{btype}-{next(_uid_counter)}", platform),
    )
    conv_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO messages (conv_id, idx, role, content_hash) VALUES (?, 0, ?, 'h')",
        (conv_id, role),
    )
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    raw_json = raw if isinstance(raw, str) or raw is None else json.dumps(raw)
    conn.execute(
        "INSERT INTO blocks (msg_id, conv_id, idx, type, text, content_hash, raw_json) VALUES (?, ?, 0, ?, '', 'h', ?)",
        (msg_id, conv_id, btype, raw_json),
    )
    conn.commit()
    return msg_id


def test_audit_blocks_full_enumeration(conn):
    _insert_block(
        conn,
        platform="vscode",
        role="system",
        btype="unknown",
        raw={"result": {"metadata": {"renderedUserMessage": "the real content"}}, "kind": "inlineReference"},
    )
    _insert_block(
        conn,
        platform="vscode",
        role="assistant",
        btype="unknown",
        raw={"result": {"metadata": {"renderedUserMessage": "more content here"}}, "kind": "inlineReference"},
    )
    _insert_block(conn, platform="vscode", role="user", btype="text", raw={"text": "hi"})

    result = shapes.audit_blocks(conn, "vscode", type_filter="unknown")
    assert result.record_count == 2
    assert result.parse_errors == 0
    path = result.paths["$.result.metadata.renderedUserMessage"]
    assert path.count == 2
    assert path.total_chars == len("the real content") + len("more content here")
    # shape signature cross-tab sees exactly one signature, shared by both records
    assert len(result.signatures) == 1
    sig = next(iter(result.signatures.values()))
    assert sig.count == 2


def test_audit_blocks_handles_unparseable_raw_json(conn):
    _insert_block(conn, platform="grok", role="assistant", btype="unknown", raw="{not valid json")
    _insert_block(conn, platform="grok", role="assistant", btype="unknown", raw=None)
    result = shapes.audit_blocks(conn, "grok", type_filter="unknown")
    assert result.record_count == 2
    assert result.parse_errors == 2
    assert result.paths == {}


def test_audit_blocks_platform_isolation(conn):
    _insert_block(conn, platform="vscode", role="user", btype="unknown", raw={"a": "vscode-only"})
    _insert_block(conn, platform="cursor", role="user", btype="unknown", raw={"a": "cursor-only"})
    result = shapes.audit_blocks(conn, "vscode", type_filter="unknown")
    assert result.record_count == 1
    assert result.paths["$.a"].example == "vscode-only"


def test_render_shape_report_contains_ranked_paths(conn):
    _insert_block(conn, platform="windsurf", role="assistant", btype="unknown", raw={"big": "x" * 500, "small": "y"})
    result = shapes.audit_blocks(conn, "windsurf", type_filter="unknown")
    report = shapes.render_shape_report(result)
    assert "windsurf" in report
    big_idx = report.index("$.big")
    small_idx = report.index("$.small")
    assert big_idx < small_idx  # ranked by total char volume, descending


# ────────────────────────── reasoning sweep ──────────────────────────


def test_reasoning_sweep_finds_known_vocab_outside_thinking_type(conn):
    # A block classified as 'text' whose raw JSON carries a reasoning-shaped
    # key — exactly the silent-misclassification case spec §4.2b targets.
    _insert_block(
        conn,
        platform="hermes",
        role="assistant",
        btype="text",
        raw={"reasoning_content": "the model's hidden chain of thought", "text": "final answer"},
    )
    sweep = shapes.reasoning_sweep(conn, "hermes")
    assert sweep["records"] == 1
    hits = sweep["known_hits"]
    assert len(hits) == 1
    assert hits[0].block_type == "text"
    assert hits[0].path == "$.reasoning_content"
    assert hits[0].total_chars == len("the model's hidden chain of thought")


def test_reasoning_sweep_over_classification_check(conn):
    # thinking-typed block whose raw JSON has no reasoning-vocabulary path at all.
    _insert_block(conn, platform="deepseek", role="assistant", btype="thinking", raw={"weird_field": "some content"})
    _insert_block(
        conn,
        platform="deepseek",
        role="assistant",
        btype="thinking",
        raw={"thoughts": "properly sourced reasoning"},
    )
    sweep = shapes.reasoning_sweep(conn, "deepseek")
    assert sweep["thinking_total"] == 2
    assert sweep["thinking_with_known_vocab"] == 1
    assert sweep["thinking_without_known_vocab"] == 1


def test_reasoning_sweep_unaccounted_paths_exclude_text_already_rendered(conn):
    conn.execute(
        "INSERT INTO conversations (uid, platform, ingested_at) VALUES ('u1', 'gemini', datetime('now'))"
    )
    conv_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO messages (conv_id, idx, role, content_hash) VALUES (?, 0, 'assistant', 'h')", (conv_id,))
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    raw = {"text": "already shown", "hidden_field": "never rendered anywhere"}
    conn.execute(
        "INSERT INTO blocks (msg_id, conv_id, idx, type, text, content_hash, raw_json) VALUES (?, ?, 0, 'text', ?, 'h', ?)",
        (msg_id, conv_id, "already shown", json.dumps(raw)),
    )
    conn.commit()

    sweep = shapes.reasoning_sweep(conn, "gemini")
    unaccounted_paths = set(sweep["unaccounted_paths"].keys())
    assert "$.hidden_field" in unaccounted_paths
    assert "$.text" not in unaccounted_paths  # its value is literally the block's rendered text


def test_render_reasoning_report_flags_non_thinking_classification():
    sweep = {
        "platform": "hermes",
        "records": 1,
        "parse_errors": 0,
        "known_hits": [
            shapes.ReasoningPathHit(
                platform="hermes", path="$.reasoning_content", role="assistant", block_type="text", count=1, total_chars=42
            )
        ],
        "unaccounted_paths": {},
        "thinking_total": 0,
        "thinking_with_known_vocab": 0,
        "thinking_without_known_vocab": 0,
    }
    report = shapes.render_reasoning_report([sweep])
    assert "hermes" in report
    assert "$.reasoning_content" in report
    assert "text" in report  # the wrong blocks.type shows up in the cross-tab

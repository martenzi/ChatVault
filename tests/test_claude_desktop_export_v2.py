"""Claude Desktop official-export format of 2026-10 (manifest + ZIP parts).
Fixtures mirror real record shapes enumerated from the 2026-10-12 export
(docs/reports/claude_export_completeness_2026-10-12.md); text is invented."""
from __future__ import annotations

import json

from chatvault.adapters import claude_desktop as cd


def _conv(content, files=None, attachments=None):
    msg = {
        "uuid": "m1", "sender": "assistant", "created_at": "2026-10-12T10:00:00Z",
        "updated_at": "2026-10-12T10:00:00Z", "parent_message_uuid": "root", "text": "",
        "content": content, "files": files or [], "attachments": attachments or [],
    }
    return [{"uuid": "c1", "name": "t", "created_at": "2026-10-12T10:00:00Z",
             "updated_at": "2026-10-12T10:00:00Z", "chat_messages": [msg]}]


def _parse(tmp_path, content, **kw):
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(_conv(content, **kw)))
    conv = next(cd.parse(p))
    return conv["messages"][0]["blocks"]


def test_injected_prompt_block_becomes_injected_context(tmp_path):
    (b,) = _parse(tmp_path, [{"type": "injected_prompt_block", "prompt": "remember X",
                              "injection_source": "memory_block_head", "initial_turn_only": True}])
    assert (b["type"], b["text"]) == ("injected_context", "remember X")
    assert b["raw"]["injection_source"] == "memory_block_head"


def test_token_budget_is_meta_not_unknown(tmp_path):
    (b,) = _parse(tmp_path, [{"type": "token_budget", "remaining": None}])
    assert (b["type"], b["text"]) == ("meta", "")


def test_document_names_itself_from_files_and_is_not_doubled(tmp_path):
    blocks = _parse(
        tmp_path,
        [{"type": "document", "file_uuid": "f1"}],
        files=[{"file_uuid": "f1", "file_name": "REVIEW.md"}, {"file_uuid": "f2", "file_name": "other.txt"}],
    )
    atts = [b for b in blocks if b["type"] == "attachment"]
    assert sorted(b["attachment"]["filename"] for b in atts) == ["REVIEW.md", "other.txt"]
    assert all(b["attachment"]["availability"] == "missing" for b in atts)


def test_document_title_wins_over_file_name(tmp_path):
    (b,) = _parse(tmp_path, [{"type": "document", "file_uuid": "f1", "title": "T.md"}],
                  files=[{"file_uuid": "f1", "file_name": ""}])
    assert b["attachment"]["filename"] == "T.md"


def test_tool_result_appends_display_and_structured_content_searchably(tmp_path):
    (b,) = _parse(tmp_path, [{
        "type": "tool_result", "is_error": False, "content": [{"type": "text", "text": "ok"}],
        "display_content": {"type": "json_block", "json_block": "{\"unique_display_marker\": 1}"},
        "structured_content": {"artifact_id": "a-1", "title": "unique_structured_marker"},
    }])
    assert b["type"] == "tool_result"
    assert "--- display_content ---\n{\"unique_display_marker\": 1}" in b["text"]
    assert "--- structured_content ---" in b["text"] and "unique_structured_marker" in b["text"]


def test_structured_content_unwrapped_and_not_doubled_when_already_present(tmp_path):
    (b,) = _parse(tmp_path, [{
        "type": "tool_result", "is_error": False, "content": [{"type": "text", "text": "allowed dirs"}],
        "structured_content": {"content": "allowed dirs"}, "display_content": None,
    }])
    assert "--- structured_content ---" not in b["text"]  # text already in the dumped content


def test_empty_sections_emit_no_delimiter(tmp_path):
    (b,) = _parse(tmp_path, [{"type": "tool_result", "is_error": False, "content": [],
                              "display_content": None, "structured_content": None}])
    assert "---" not in b["text"]


def test_tool_use_display_content_and_tool_name(tmp_path):
    use = lambda name, integ: {"type": "tool_use", "name": name, "integration_name": integ,
                               "input": {"a": 1}, "display_content": {"type": "text", "text": "Reading source"}}
    b1, b2, b3 = _parse(tmp_path, [use("Bash", "Claude Code"), use("filesystem:read_text_file", "filesystem"),
                                   use("bash_tool", None)])
    assert "--- display_content ---\nReading source" in b1["text"]
    assert [b1["tool_name"], b2["tool_name"], b3["tool_name"]] == [
        "Claude Code:Bash", "filesystem:read_text_file", "bash_tool"]


def test_is_error_tool_result_becomes_error_block(tmp_path):
    ok, bad = _parse(tmp_path, [
        {"type": "tool_result", "is_error": False, "content": [{"type": "text", "text": "fine"}]},
        {"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "boom"}]},
    ])
    assert (ok["type"], bad["type"]) == ("tool_result", "error")
    assert "boom" in bad["text"]


def test_no_unknown_blocks_for_any_enumerated_type(tmp_path):
    blocks = _parse(tmp_path, [
        {"type": "text", "text": "hi"}, {"type": "thinking", "thinking": "hm", "summaries": [{"summary": "s"}]},
        {"type": "token_budget", "remaining": None}, {"type": "injected_prompt_block", "prompt": "p"},
        {"type": "document", "file_uuid": "f"},
        {"type": "tool_use", "name": "n", "input": {}}, {"type": "tool_result", "content": [], "is_error": False},
    ])
    assert "unknown" not in {b["type"] for b in blocks}
    assert all(b["raw"] for b in blocks)


# ---- artifacts (Phase 3) -------------------------------------------------

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402

from chatvault import ingest, loader  # noqa: E402

ART = "1bedac52-0da5-4d6e-b885-39e4f907a67d"
ORPHAN = "0ddbe723-c090-4462-be5d-7aa8690451c6"
CANVAS = "3db351de-df93-49cb-b848-2added3ac04c"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _artifact_tree(root: Path) -> Path:
    a = root / "artifacts"
    meta = lambda i, kind, vs, active: json.dumps(
        {"id": i, "kind": kind, "visibility": "private", "versions": vs, "active_version": active})
    _write(a / ART / "artifact.json", meta(ART, "artifact", [
        {"id": "v2", "title": "Essay 300 words"}, {"id": "v1", "title": "Statement A"}], "v2"))
    _write(a / ART / "versions/v1.html", "<p>statement A body</p>")
    _write(a / ART / "versions/v2.html", "<p>essay body</p>")
    _write(a / ORPHAN / "artifact.json", meta(ORPHAN, "artifact", [{"id": "o1", "title": "Orphan doc"}], "o1"))
    _write(a / ORPHAN / "versions/o1.html", "<p>nobody references me</p>")
    # canvas layout: only project/* is content; index.html/SKILL.md/artifact-type are scaffolding
    _write(a / CANVAS / "artifact.json", meta(CANVAS, "artifact", [{"id": "c1", "title": "Map"}], "c1"))
    _write(a / CANVAS / "versions/c1/project/Main.dc.html", "<x-dc>map content</x-dc>")
    _write(a / CANVAS / "versions/c1/project/canvas.json", "{\"v\":3}")
    _write(a / CANVAS / "versions/c1/index.html", "<html>editor shell</html>")
    _write(a / CANVAS / "versions/c1/SKILL.md", "editor skill boilerplate")
    _write(a / CANVAS / "versions/c1/artifact-type/app.js", "runtime boilerplate")
    _write(a / CANVAS / "versions/c1.files.json", "{\"files\": []}")
    return a


def test_parse_artifacts_ingests_content_not_scaffolding(tmp_path):
    a = _artifact_tree(tmp_path)
    assert cd.sniff_artifacts(a / ART / "artifact.json") and not cd.sniff_artifacts(a / ART / "versions/v1.html")
    (art,) = list(cd.parse_artifacts(a / CANVAS / "artifact.json"))
    names = sorted(x["filename"] for x in art["attachments"])
    assert names == ["Map — Main.dc.html", "Map — canvas.json"]
    assert all(x["role"] == "artifact" for x in art["attachments"])
    (flat,) = list(cd.parse_artifacts(a / ART / "artifact.json"))
    assert [x["filename"] for x in flat["attachments"]][0] == "Essay 300 words"  # active version first


def _conv_referencing(artifact_id: str, title: str) -> list:
    content = [{"type": "tool_result", "is_error": False, "content": [{"type": "text", "text": "saved"}],
                "structured_content": {"artifact_id": artifact_id, "title": title, "url": "u"}}]
    return _conv(content)


def test_artifacts_link_to_referencing_message_and_orphans_stay_unlinked(tmp_path, conn):
    a = _artifact_tree(tmp_path)
    conv_path = tmp_path / "conversations.json"
    conv_path.write_text(json.dumps(_conv_referencing(ART, "Essay 300 words")))

    for f in sorted(a.rglob("artifact.json")):
        (art,) = list(cd.parse_artifacts(f))
        loader.load_artifact(conn, art, None)
    for cir in cd.parse(conv_path):
        loader.load_conversation(conn, cir, None)
    res = loader.link_artifacts(conn)
    conn.commit()

    assert (res["artifacts"], res["referenced"], res["linked"]) == (3, 1, 1)
    assert sorted(o[0] for o in res["orphans"]) == sorted([CANVAS, ORPHAN])
    links = conn.execute(
        "SELECT a.filename, l.role, l.target_type FROM attachment_links l JOIN attachments a ON a.id = l.attachment_id"
    ).fetchall()
    assert [tuple(r) for r in links] == [("Essay 300 words", "artifact", "message")]  # title-matched, not Statement A
    # idempotent
    assert loader.link_artifacts(conn)["linked"] == 0


def test_unchanged_version_content_is_deduplicated(tmp_path, conn):
    a = _artifact_tree(tmp_path)
    _write(a / ART / "versions/v1.html", "<p>essay body</p>")  # v1 == v2 content
    meta = json.loads((a / ART / "artifact.json").read_text())
    meta["versions"][1]["title"] = "Essay 300 words"  # same filename too
    (a / ART / "artifact.json").write_text(json.dumps(meta))
    (art,) = list(cd.parse_artifacts(a / ART / "artifact.json"))
    r = loader.load_artifact(conn, art, None)
    assert (r["attachments"], r["created"]) == (2, 1)


def test_ingest_path_walks_artifacts_then_links(tmp_path, conn):
    _artifact_tree(tmp_path)
    (tmp_path / "conversations.json").write_text(json.dumps(_conv_referencing(ART, "Statement A")))
    s = ingest.ingest_path(conn, tmp_path)
    assert s.artifacts["referenced"] == 1 and s.artifacts["linked"] == 1
    assert len(s.artifacts["orphans"]) == 2
    again = ingest.ingest_path(conn, tmp_path)
    assert again.totals["inserted"] == 0 and again.artifacts["linked"] == 0

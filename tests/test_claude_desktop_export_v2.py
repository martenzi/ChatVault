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

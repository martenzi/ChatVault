from __future__ import annotations

from chatvault import export, loader
from chatvault.adapters.base import make_block, make_conversation, make_message


def _load(conn, cir):
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result


def _conv_with_blocks(blocks, native_id="p1", role="assistant"):
    msgs = [
        make_message(0, "user", None, "2026-01-01T00:00:00", [make_block(0, "text", "the question", raw={})], raw={}),
        make_message(1, role, None, "2026-01-01T00:00:01", blocks, raw={}),
    ]
    return make_conversation(
        platform="synthetic", native_id=native_id, title="profile test", project=None,
        started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:01",
        source_path="synthetic", raw={}, messages=msgs,
    )


def test_read_profile_always_collapses_tool_calls(conn):
    blocks = [
        make_block(0, "tool_use", "x" * 50, raw={}, tool_name="search_docs"),
        make_block(1, "text", "the actual answer", raw={}),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    content = export.render_profile(conn, row, profile="read")
    assert "x" * 50 not in content
    assert "ran search_docs(...)" in content
    assert "the actual answer" in content


def test_read_profile_drops_thinking(conn):
    blocks = [
        make_block(0, "thinking", "internal reasoning nobody should see", raw={}),
        make_block(1, "text", "final answer", raw={}),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    content = export.render_profile(conn, row, profile="read")
    assert "internal reasoning" not in content
    assert "final answer" in content


def test_lean_profile_keeps_short_tool_body(conn):
    blocks = [make_block(0, "tool_result", "short output", raw={}, tool_name="grep")]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    content = export.render_profile(conn, row, profile="lean")
    assert "short output" in content


def test_lean_profile_truncates_long_tool_body(conn):
    long_text = "y" * 1204
    blocks = [make_block(0, "tool_result", long_text, raw={}, tool_name="web_search")]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    content = export.render_profile(conn, row, profile="lean")
    assert long_text not in content
    assert "[tool: web_search, 1,204 chars omitted]" in content


def test_lean_profile_drops_thinking(conn):
    blocks = [
        make_block(0, "thinking", "chain of thought", raw={}),
        make_block(1, "text", "answer", raw={}),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    content = export.render_profile(conn, row, profile="lean")
    assert "chain of thought" not in content


def test_full_profile_keeps_everything_uncollapsed(conn):
    long_text = "z" * 1204
    blocks = [
        make_block(0, "thinking", "reasoning trace", raw={}),
        make_block(1, "tool_use", long_text, raw={}, tool_name="web_search"),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    content = export.render_profile(conn, row, profile="full")
    assert "reasoning trace" in content
    assert long_text in content


def test_lean_is_smaller_than_full_for_a_tool_heavy_conversation(conn):
    long_text = "w" * 3000
    blocks = [
        make_block(0, "thinking", "a lot of internal deliberation " * 20, raw={}),
        make_block(1, "tool_use", long_text, raw={}, tool_name="web_search"),
        make_block(2, "tool_result", long_text, raw={}, tool_name="web_search"),
        make_block(3, "text", "the synthesized answer", raw={}),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    lean = export.render_profile(conn, row, profile="lean")
    full = export.render_profile(conn, row, profile="full")
    assert len(lean) < len(full)


def test_blocks_override_restricts_to_given_types(conn):
    blocks = [
        make_block(0, "code", "print('hi')", raw={}),
        make_block(1, "tool_use", "x" * 10, raw={}, tool_name="run"),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    content = export.render_profile(conn, row, profile="full", blocks="code")
    assert "print('hi')" in content
    assert "run" not in content


def test_max_tool_chars_overrides_profile_budget(conn):
    blocks = [make_block(0, "tool_result", "z" * 100, raw={}, tool_name="grep")]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    content = export.render_profile(conn, row, profile="lean", max_tool_chars=50)
    assert "z" * 100 not in content
    assert "[tool: grep, 100 chars omitted]" in content


def test_full_is_always_at_least_as_large_as_read_and_lean(conn):
    """Spec v1.3 §6 'Profile sizes': full >= read and full >= lean, always."""
    blocks = [
        make_block(0, "thinking", "deliberation " * 30, raw={}),
        make_block(1, "tool_use", "a" * 2000, raw={}, tool_name="web_search"),
        make_block(2, "tool_result", "b" * 100, raw={}, tool_name="grep"),
        make_block(3, "code", "print('hello')", raw={}),
        make_block(4, "text", "the final synthesized answer", raw={}),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    read = export.render_profile(conn, row, profile="read")
    lean = export.render_profile(conn, row, profile="lean")
    full = export.render_profile(conn, row, profile="full")

    assert len(full) >= len(read)
    assert len(full) >= len(lean)


def test_lean_at_least_half_smaller_than_full_on_tool_heavy_conversation(conn):
    """Spec v1.3 §6: on a tool-heavy conversation, lean is >=50% smaller than full."""
    long_text = "c" * 5000
    blocks = [
        make_block(0, "thinking", "internal deliberation " * 100, raw={}),
        make_block(1, "tool_use", long_text, raw={}, tool_name="web_search"),
        make_block(2, "tool_result", long_text, raw={}, tool_name="web_search"),
        make_block(3, "tool_use", long_text, raw={}, tool_name="read_file"),
        make_block(4, "text", "the synthesized answer", raw={}),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    lean = export.render_profile(conn, row, profile="lean")
    full = export.render_profile(conn, row, profile="full")

    assert len(lean) <= len(full) * 0.5


def test_read_vs_lean_has_no_guaranteed_ordering(conn):
    """Spec v1.3 §4/§6: lean can legitimately be *larger* than read when a
    conversation has many small tool calls, since read always collapses to a
    stub while lean keeps short tool bodies in full. Not a defect."""
    blocks = [
        make_block(
            i, "tool_result",
            f"check {i}: nominal — all sub-systems reporting normal status, no anomalies detected " * 2,
            raw={}, tool_name="check",
        )
        for i in range(20)
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    read = export.render_profile(conn, row, profile="read")
    lean = export.render_profile(conn, row, profile="lean")

    assert len(lean) > len(read)


def test_lean_dialogue_integrity_keeps_prose_verbatim(conn):
    """Spec v1.3 §6 'Dialogue integrity': lean never trims text/code blocks,
    only machinery (thinking, tool bodies)."""
    long_prose = "This is a long piece of user/assistant dialogue. " * 50
    long_code = "x = 1\n" * 200
    blocks = [
        make_block(0, "text", long_prose, raw={}),
        make_block(1, "code", long_code, raw={}),
    ]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    lean = export.render_profile(conn, row, profile="lean")
    assert long_prose in lean
    assert long_code in lean


def test_unknown_profile_raises(conn):
    blocks = [make_block(0, "text", "hi", raw={})]
    result = _load(conn, _conv_with_blocks(blocks))
    row = export.get_conversations_by_ids(conn, [result.conv_id])[0]

    try:
        export.render_profile(conn, row, profile="bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass

from __future__ import annotations

from datetime import datetime, timezone

from chatvault.adapters import identifiers as ids

# ── §3.2 mandatory regression tests (EXTRACTION_v4.0_CHANGELOG.txt) ─────────


def test_windsurf_memory_injection_marker_detected():
    """Regression test #1: the Windsurf `<SYSTEM-RETRIEVED-MEMORY[uuid]>...`
    memory-dump tag must be caught by is_injected_context() so it can be
    classified as `injected_context`, not ordinary `text`."""
    text = (
        "<SYSTEM-RETRIEVED-MEMORY[3f9a1b2c-...]>\n"
        "Some retrieved memory content the user never typed.\n"
        "</SYSTEM-RETRIEVED-MEMORY[3f9a1b2c-...]>"
    )
    assert ids.is_injected_context("assistant", text) is True
    # Case-insensitive, matches on the lowercase marker substring.
    assert ids.is_injected_context("user", text.upper()) is True


def test_injected_context_not_triggered_for_ordinary_dialogue():
    assert ids.is_injected_context("assistant", "Sure, here's how to do that.") is False


def test_injected_context_gated_by_role():
    # boilerplate_chat() only ever fires for user/assistant/unknown roles.
    assert ids.is_injected_context("tool", "<environment_context>stuff</environment_context>") is False


def test_decode_utf8_counting_replacements_nonzero_on_invalid_byte():
    """Regression test #2: an invalid UTF-8 byte must surface as a non-zero
    replacement count, never silently vanish (never errors="ignore")."""
    raw = b"before" + b"\xff" + b"after"
    text, count = ids.decode_utf8_counting_replacements(raw)
    assert count == 1
    assert "�" in text
    assert text == "before�after"


def test_decode_utf8_counting_replacements_zero_on_valid_utf8():
    raw = "héllo wörld".encode("utf-8")
    text, count = ids.decode_utf8_counting_replacements(raw)
    assert count == 0
    assert text == "héllo wörld"


# ── normalise_role() ─────────────────────────────────────────────────────


def test_normalise_role_synonyms():
    assert ids.normalise_role("human") == "user"
    assert ids.normalise_role("assistant_response") == "assistant"
    assert ids.normalise_role("gemini") == "assistant"
    assert ids.normalise_role("bot") == "assistant"
    assert ids.normalise_role("ai") == "assistant"


def test_normalise_role_passthrough_and_case_fold():
    assert ids.normalise_role("System") == "system"
    assert ids.normalise_role(" Tool ") == "tool"


def test_normalise_role_none_is_unknown():
    assert ids.normalise_role(None) == "unknown"
    assert ids.normalise_role("") == "unknown"


def test_normalise_role_dict_valued():
    assert ids.normalise_role({"role": "human"}) == "user"
    assert ids.normalise_role({"name": "Assistant"}) == "assistant"
    assert ids.normalise_role({"type": "bot"}) == "assistant"
    assert ids.normalise_role({}) == "unknown"


# ── extract_content() ────────────────────────────────────────────────────


def test_extract_content_str():
    assert ids.extract_content("hello") == ("hello", None)
    assert ids.extract_content(None) == ("", None)


def test_extract_content_list_of_dicts_text_and_thinking():
    content = [
        {"type": "text", "text": "hi there"},
        {"type": "thinking", "thinking": "pondering..."},
    ]
    text, thinking = ids.extract_content(content)
    assert text == "hi there"
    assert thinking == "pondering..."


def test_extract_content_skips_skip_item_types():
    content = [
        {"type": "tool_use", "text": "should be dropped from text"},
        {"type": "text", "text": "kept"},
    ]
    text, _ = ids.extract_content(content)
    assert text == "kept"
    assert "dropped" not in text


def test_extract_content_gemini_cli_empty_type_case():
    """The itype=='' branch: Gemini-CLI {text: "..."} items carry no `type`
    field at all."""
    content = [{"text": "plain gemini cli chunk"}]
    text, _ = ids.extract_content(content)
    assert text == "plain gemini cli chunk"


def test_extract_content_empty_type_filters_referenced_file_markers():
    content = [{"text": "--- Content from referenced files --- noise"}]
    text, _ = ids.extract_content(content)
    assert text == ""


def test_extract_content_dict_nested_content_recursion():
    content = {"content": [{"type": "text", "text": "nested"}]}
    text, _ = ids.extract_content(content)
    assert text == "nested"


def test_extract_content_dict_input_output_text():
    assert ids.extract_content({"type": "input_text", "text": "in"}) == ("in", None)
    assert ids.extract_content({"type": "output_text", "text": "out"}) == ("out", None)


# ── extract_thinking() ───────────────────────────────────────────────────


def test_extract_thinking_top_level_keys():
    assert ids.extract_thinking({"thoughts": "a thought"}) == "a thought"
    assert ids.extract_thinking({"thinking": "another"}) == "another"
    assert ids.extract_thinking({"reasoning": "yet another"}) == "yet another"


def test_extract_thinking_redacted():
    rec = {"content": [{"type": "thinking", "redacted_thinking": "hidden"}]}
    assert ids.extract_thinking(rec) == "hidden"


def test_extract_thinking_nested_content_item_fallback():
    rec = {"content": [{"type": "reasoning", "text": "nested reasoning"}]}
    assert ids.extract_thinking(rec) == "nested reasoning"


def test_extract_thinking_list_of_subject_description_dicts():
    rec = {
        "thoughts": [
            {"subject": "Plan", "description": "Figure out the approach"},
            {"subject": "", "description": "no subject here"},
        ]
    }
    result = ids.extract_thinking(rec)
    assert result == "**Plan**\n\nFigure out the approach\n\nno subject here"


def test_extract_thinking_none_when_absent():
    assert ids.extract_thinking({}) is None


# ── clean_text() ──────────────────────────────────────────────────────────


def test_clean_text_crlf_and_nul():
    assert ids.clean_text("a\r\nb\rc\x00d") == "a\nb\ncd"


def test_clean_text_strips_final_tags_and_collapses_blanks():
    text = "<final>hello</final>\n\n\n\nworld"
    assert ids.clean_text(text) == "hello\n\nworld"


def test_clean_text_drops_thinkingsignature_lines():
    text = "keep this\nThinkingSignature: abc123\nkeep that"
    assert ids.clean_text(text) == "keep this\nkeep that"


def test_clean_text_drops_long_base64_and_hex_leak_lines():
    b64_leak = "A" * 61  # >=60 chars, valid base64 alphabet
    hex_leak = "a1b2c3" * 12  # >=60 hex chars
    text = f"real line\n{b64_leak}\n{hex_leak}\nmore real content"
    cleaned = ids.clean_text(text)
    assert b64_leak not in cleaned
    assert hex_leak not in cleaned
    assert "real line" in cleaned
    assert "more real content" in cleaned


def test_clean_text_na_variants_become_empty():
    assert ids.clean_text("N/A") == ""
    assert ids.clean_text("null") == ""


# ── strip_metadata_envelope() / strip_referenced_file_dump() ───────────────


def test_strip_metadata_envelope():
    text = 'Conversation info (untrusted metadata): ```json\n{"foo": 1}\n```\nreal text'
    assert ids.strip_metadata_envelope(text) == "real text"


def test_strip_metadata_envelope_timestamp_prefix():
    text = "[2026-09-07 12:00:00] the actual message"
    assert ids.strip_metadata_envelope(text) == "the actual message"


def test_strip_referenced_file_dump():
    text = "before\n--- Content from referenced files ---\nsome file dump\n--- End of content ---\nafter"
    result = ids.strip_referenced_file_dump(text)
    assert "some file dump" not in result
    assert "before" in result and "after" in result


# ── coerce_timestamp() ──────────────────────────────────────────────────────


def test_coerce_timestamp_epoch_seconds():
    dt = ids.coerce_timestamp(1_700_000_000)
    assert dt is not None
    assert dt.astimezone(timezone.utc).year == 2023


def test_coerce_timestamp_epoch_milliseconds_boundary():
    """The disambiguation boundary is exactly 1_000_000_000_000: values
    above it are treated as milliseconds and divided down."""
    seconds_value = 1_700_000_000
    ms_value = seconds_value * 1000  # 1,700,000,000,000 > boundary
    assert ms_value > 1_000_000_000_000
    dt_seconds = ids.coerce_timestamp(seconds_value)
    dt_ms = ids.coerce_timestamp(ms_value)
    assert dt_seconds is not None and dt_ms is not None
    assert dt_seconds.astimezone(timezone.utc) == dt_ms.astimezone(timezone.utc)


def test_coerce_timestamp_at_boundary_not_divided():
    # The boundary check is strict greater-than (`if s > 1_000_000_000_000`),
    # per the source. A value exactly AT the boundary is therefore treated
    # as raw seconds, not milliseconds — which is so far in the future
    # (~year 33869) it overflows Python's datetime range and correctly
    # comes back None, rather than being silently divided down to a
    # plausible-looking (but wrong) date.
    assert ids.coerce_timestamp(1_000_000_000_000) is None
    # One unit above the boundary IS divided, landing on a normal date.
    dt = ids.coerce_timestamp(1_000_000_000_001)
    assert dt is not None
    assert dt.astimezone(timezone.utc).year == 2001


def test_coerce_timestamp_digit_string():
    assert ids.coerce_timestamp("1700000000") == ids.coerce_timestamp(1700000000)


def test_coerce_timestamp_iso8601_with_z():
    dt = ids.coerce_timestamp("2026-09-07T12:00:00Z")
    assert dt is not None
    assert dt.astimezone(timezone.utc) == datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_coerce_timestamp_none_and_empty():
    assert ids.coerce_timestamp(None) is None
    assert ids.coerce_timestamp("") is None
    assert ids.coerce_timestamp("not a date") is None


# ── COLLECTION_KEYS / NON_MESSAGE_RECORD_TYPES / CONTENT_ITEM_TYPE_MAP ─────


def test_collection_keys_matches_source():
    assert ids.COLLECTION_KEYS == (
        "messages", "items", "turns", "conversation", "conversations", "entries", "mapping",
    )


def test_non_message_record_types_matches_source():
    assert ids.NON_MESSAGE_RECORD_TYPES == {
        "session", "session_meta", "event_msg", "turn_context",
        "thinking_level_change", "model_change", "custom", "info",
    }


def test_content_item_type_map_covers_all_skip_item_types():
    expected_keys = {
        "tool_call", "tool_result", "tool_use",
        "function_call", "function_call_output", "function_result",
        "custom_tool_call", "custom_tool_call_output",
        "image", "image_url", "input_image", "output_image",
        "audio", "refusal", "error", "toolcall",
    }
    assert set(ids.CONTENT_ITEM_TYPE_MAP.keys()) == expected_keys
    # New block types introduced by spec §3.3 for the two items with no
    # prior ChatVault equivalent.
    assert ids.CONTENT_ITEM_TYPE_MAP["refusal"] == "refusal"
    assert ids.CONTENT_ITEM_TYPE_MAP["audio"] == "audio"

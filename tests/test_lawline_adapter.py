from __future__ import annotations

from chatvault import adapters
from chatvault.adapters import lawline
from tests.conftest import FIXTURES

FIXTURE = FIXTURES / "lawline" / "export.json"


def _conv():
    (conv,) = list(lawline.parse(FIXTURE))
    return conv


def test_detected_and_threadid_is_native_id():
    assert adapters.detect(FIXTURE).name == "lawline"
    conv = _conv()
    assert conv["platform"] == "lawline"
    assert conv["native_id"] == "thread-fixture-0001"
    assert conv["started_at"] == "2026-05-31T11:33:29.000Z"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "system", "assistant"]


def test_assistant_parts_map_to_block_types():
    blocks = _conv()["messages"][1]["blocks"]
    assert [b["type"] for b in blocks] == [
        "meta", "thinking", "meta", "tool_use", "tool_result", "tool_use", "tool_result", "text",
    ]
    assert [b["idx"] for b in blocks] == list(range(len(blocks)))
    assert blocks[3]["tool_name"] == "sfs" and blocks[3]["text"] == '{"query":"q"}'
    # model-facing text wins over summary; summary/body used when absent
    assert blocks[4]["text"] == "Model-facing result text."
    assert blocks[6]["text"] == "Email body"
    assert blocks[2]["text"] == "Searching · Step one"
    assert _conv()["messages"][1]["model"] == "model-x"


def test_system_reminder_is_injected_context():
    (block,) = _conv()["messages"][2]["blocks"]
    assert block["type"] == "injected_context" and block["text"] == "Last step."


def test_unrecognised_part_is_unknown_not_guessed():
    (block,) = _conv()["messages"][3]["blocks"]
    assert block["type"] == "unknown" and block["text"] == "mystery"
    assert block["raw"]["type"] == "brand-new-part"


def test_non_lawline_json_not_claimed(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"chatSessions": [{"foo": 1}]}')
    assert not lawline.sniff(f)

from __future__ import annotations

from chatvault import adapters
from chatvault.adapters import openwebui
from tests.conftest import FIXTURES

WITH_ID = FIXTURES / "openwebui" / "chat_with_id.json"
SIMPLE = FIXTURES / "openwebui" / "chat_simple.json"


def test_detected_and_active_path_follows_current_id():
    assert adapters.detect(WITH_ID).name == "openwebui"
    (conv,) = list(openwebui.parse(WITH_ID))
    assert conv["platform"] == "openwebui"
    live = [m for m in conv["messages"] if not m.get("excluded")]
    assert [m["role"] for m in live] == ["user", "assistant", "user", "assistant"]
    # currentId (a3), not the file's linear copy (ends on a2)
    assert live[-1]["blocks"][0]["text"].startswith("CREATE TABLE")
    assert live[1]["model"] == "deepseek/deepseek-reasoner"


def test_reasoning_details_split_into_thinking_block():
    (conv,) = list(openwebui.parse(WITH_ID))
    blocks = conv["messages"][1]["blocks"]
    assert [b["type"] for b in blocks] == ["thinking", "text"]
    assert blocks[0]["text"] == "The user asks a basic database question.\nKeep it short."
    assert blocks[1]["text"] == "A primary key uniquely identifies a row."


def test_abandoned_sibling_kept_in_excluded_message():
    (conv,) = list(openwebui.parse(WITH_ID))
    ab = conv["messages"][-1]
    assert ab["excluded"] == 1 and ab["raw"] == {"abandoned_branch": True}
    assert "First draft" in ab["blocks"][0]["raw"]["content"]


def test_both_export_copies_share_one_native_id():
    (a,) = list(openwebui.parse(WITH_ID))
    (b,) = list(openwebui.parse(SIMPLE))
    assert a["native_id"] == b["native_id"] == "11111111-0000-0000-0000-000000000001"
    assert a["started_at"] == "2025-04-03T23:44:12+00:00" or a["started_at"].startswith("2025-04-03T23:44:12")


def test_other_json_not_claimed(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"created_at": 1, "title": "t", "content": {"history": []}}')
    assert not openwebui.sniff(f)

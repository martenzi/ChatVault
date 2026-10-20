from __future__ import annotations

from chatvault import adapters
from chatvault.adapters import chatgpt
from tests.conftest import FIXTURES

FIXTURE = FIXTURES / "chatgpt" / "codex.json"


def test_codex_task_detected_as_chatgpt_and_mapped():
    assert adapters.detect(FIXTURE).name == "chatgpt"
    (conv,) = list(chatgpt.parse(FIXTURE))
    assert conv["platform"] == "chatgpt" and conv["native_id"] == "task_e_fixture"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][0]["blocks"][0]["text"] == "Do the thing."
    types = [(b["type"], b["tool_name"], b["text"]) for b in conv["messages"][1]["blocks"]]
    assert types == [
        ("text", None, "Done."),
        ("meta", None, "src/x.py"),
        ("tool_result", "partial_repo_snapshot", "src/x.py"),
        ("tool_result", "pr", "Add x\n\nBody"),
        ("tool_result", "follow_up_diff", ""),
    ]

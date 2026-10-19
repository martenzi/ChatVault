from __future__ import annotations

from chatvault import adapters
from chatvault.adapters import notebooklm
from tests.conftest import FIXTURES

FIXTURE = next((FIXTURES / "notebooklm").rglob("Chat Session - *.html"))


def test_detected_and_chronological_order():
    assert adapters.detect(FIXTURE).name == "notebooklm"
    (conv,) = list(notebooklm.parse(FIXTURE))
    assert conv["platform"] == "notebooklm"
    assert conv["native_id"] == "11111111-2222-3333-4444-555555555555"
    assert conv["project"] == "Test Notebook"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "user", "assistant"]
    assert conv["messages"][0]["blocks"][0]["text"] == "First question?"
    assert conv["title"] == "First question?"
    assert "**bold**" in conv["messages"][3]["blocks"][0]["text"]


def test_non_chat_session_html_not_claimed(tmp_path):
    d = tmp_path / "NB" / "Sources"
    d.mkdir(parents=True)
    f = d / "Chat Session - x.html"
    f.write_text("MODEL: <p>x</p>\n\nUSER: <p>y</p>")
    assert not notebooklm.sniff(f)

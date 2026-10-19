from __future__ import annotations

import orjson

from chatvault import adapters
from chatvault.adapters import google_ai_mode
from tests.conftest import FIXTURES

FIXTURE = FIXTURES / "google_ai_mode" / "MyActivity.html"


def test_detected_as_google_ai_mode_not_gemini():
    assert adapters.detect(FIXTURE).name == "google_ai_mode"


def test_parse_shapes_and_stable_ids():
    convs = list(google_ai_mode.parse(FIXTURE))
    assert [c["platform"] for c in convs] == ["google_ai_mode"] * 3
    qa, only_query, event = convs
    assert [m["role"] for m in qa["messages"]] == ["user", "assistant"]
    # user's own words, not Google's rewrite (kept in raw)
    assert qa["messages"][0]["blocks"][0]["text"] == "teh capital of france"
    assert qa["messages"][0]["raw"]["rewritten_prompt"] == "the capital of france"
    assert "Paris" in qa["messages"][1]["blocks"][0]["text"]
    assert qa["started_at"] == "2026-10-16T17:39:41Z"
    assert [m["role"] for m in only_query["messages"]] == ["user"]
    assert only_query["messages"][0]["raw"]["shape_unclear"] is True
    assert event["raw"]["event_only"] and event["messages"][0]["role"] == "system"
    assert [c["native_id"] for c in convs] == [c["native_id"] for c in google_ai_mode.parse(FIXTURE)]


def test_json_variant_gets_same_native_id_as_html(tmp_path):
    rec = [{
        "header": "AI Mode", "title": "Searched for teh capital of france",
        "time": "2026-10-16T17:39:41.123Z", "products": ["AI Mode"], "activityControls": ["Web & App Activity"],
        "safeHtmlItem": [{"html": "<p><strong>Your prompt:</strong><br>\nthe capital of france</p>\n<p><strong>Search&#39;s response:</strong><br>\nParis.</p>"}],
    }]
    p = tmp_path / "MyActivity.json"
    p.write_bytes(orjson.dumps(rec, option=orjson.OPT_INDENT_2))
    assert adapters.detect(p).name == "google_ai_mode"
    (json_conv,) = list(google_ai_mode.parse(p))
    html_first = next(google_ai_mode.parse(FIXTURE))
    assert json_conv["native_id"] == html_first["native_id"]

"""Gemini Canvas -> artifact, the one title rule, and `repair gemini-canvas`.
Synthetic fixtures only: the same canvas is written as a JSON-variant record
and as an HTML-variant cell and must come out identical."""

from __future__ import annotations

import orjson
import pytest

from chatvault import attachments as attachments_mod
from chatvault import chunk_embeddings as ce
from chatvault import ingest, loader, repair, search, verify
from chatvault.adapters import gemini_takeout as gemini
from chatvault.adapters.base import make_block, make_conversation, make_message, normalize_title
from chatvault.config import Config
from chatvault.db import connect, migrate

BODY = "# Quarterly Plan\n\nThe zebra protocol needs 3 steps & a review.\n"
FENCED = "```markdown\n" + BODY + "```"
CANVAS_TIME_JSON = "2026-09-16T17:08:02.250Z"
CHAT_TIME = "2026-09-16T17:06:30Z"  # 1.5 minutes before the canvas


def _json_file(tmp_path, name="Quarterly Plan", content=FENCED, fname="MyActivity.json"):
    records = [
        {
            "header": "Gemini Apps", "title": "Prompted write a plan", "time": CHAT_TIME,
            "products": ["Gemini Apps"], "activityControls": ["Gemini Apps Activity"],
            "details": [{"name": "Gemini Apps", "url": "https://gemini.google.com/app/aa11bb22cc33"}],
            "safeHtmlItem": [{"html": "<p>Here is a plan.</p>"}],
        },
        {
            "header": "Gemini Apps", "title": f"Created Gemini Canvas titled {name}", "time": CANVAS_TIME_JSON,
            "products": ["Gemini Apps"], "activityControls": ["Gemini Apps Activity"],
            "subtitles": [{"name": content}],
        },
    ]
    path = tmp_path / fname
    path.write_bytes(orjson.dumps(records))
    return path


def _cell(zone, ts):
    return (
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
        '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">Gemini Apps<br></p></div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
        f"{zone}<br>{ts}<br></div>"
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
        '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"><b>Products:</b><br>&emsp;Gemini Apps<br>'
        '<b>Why is this here?</b><br>&emsp;<a href="https://myaccount.google.com/activitycontrols">here</a>.</div></div></div>\n'
    )


def _html_zone(name, content):
    body = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return f"Created Gemini Canvas titled\xa0{name}<br>{body}"


def _html_file(tmp_path, name="Quarterly Plan", content=FENCED, fname="MyActivity.html"):
    chat_cell = _cell("Prompted write a plan", "Sep 16, 2026, 7:06:30 PM CEST").replace(
        "<br></div>", "<br><p>Here is a plan.</p><br></div>", 1
    ).replace("Gemini Apps<br><b>Why", 'Gemini Apps<br><b>Details:</b><br>&emsp;https://gemini.google.com/app/aa11bb22cc33: '
              '<a href="https://gemini.google.com/app/aa11bb22cc33">x</a><br><b>Why', 1)
    path = tmp_path / fname
    path.write_text("<!DOCTYPE html><html><body>\n" + chat_cell + _cell(_html_zone(name, content), "Sep 16, 2026, 7:08:02 PM CEST") + "</body></html>")
    return path


def _canvas(path):
    return next(c for c in gemini.parse(path) if (c["native_id"] or "").startswith("canvas:"))


# ---- the title rule -----------------------------------------------------


def test_normalize_title_cases():
    assert normalize_title(None) is None
    assert normalize_title("  \n\t\xa0 ") is None
    assert normalize_title("a\xa0b\n\nc   d") == "a b c d"
    assert normalize_title("x" * 200) == "x" * 200  # exactly the limit: untouched
    cut = normalize_title("word " * 100)
    assert cut.endswith("…") and len(cut) <= 200 and "wor…" not in cut and cut.endswith("word…")
    huge = normalize_title("y" * 259_000)
    assert len(huge) == 200 and huge.endswith("…")  # no boundary: hard cut at 199 + ellipsis


def test_make_conversation_and_loader_enforce_it(conn):
    cir = make_conversation(
        platform="synthetic", native_id="t1", title="line one\nline two " + "z " * 400, project=None,
        started_at=None, ended_at=None, source_path="s", raw={},
        messages=[make_message(0, "user", None, None, [make_block(0, "text", "hi", raw={})], raw={})],
    )
    assert "\n" not in cir["title"] and len(cir["title"]) <= 200
    cir["title"] = "raw\nnewline\xa0title " + "q" * 500  # an adapter that bypasses make_conversation
    loader.load_conversation(conn, cir, source_file_id=None)
    stored = conn.execute("SELECT title FROM conversations").fetchone()[0]
    assert "\n" not in stored and len(stored) <= 200


def test_verify_fails_on_long_or_multiline_title(conn):
    cir = make_conversation(
        platform="synthetic", native_id="t2", title="fine", project=None, started_at=None, ended_at=None,
        source_path="s", raw={},
        messages=[make_message(0, "user", None, None, [make_block(0, "text", "hi", raw={})], raw={})],
    )
    loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    assert next(r for r in verify.run(conn) if r.name == "title rule").ok
    conn.execute("UPDATE conversations SET title = 'a' || char(10) || 'b'")
    assert not next(r for r in verify.run(conn) if r.name == "title rule").ok
    conn.execute("UPDATE conversations SET title = ?", ("x" * 201,))
    assert not next(r for r in verify.run(conn) if r.name == "title rule").ok


# ---- canvas shape, both variants -----------------------------------------


def test_json_and_html_variants_produce_the_same_canvas(tmp_path):
    a, b = _canvas(_json_file(tmp_path)), _canvas(_html_file(tmp_path))
    assert a["title"] == b["title"] == "Gemini Canvas: Quarterly Plan"
    assert a["native_id"] == b["native_id"]
    blocks_a, blocks_b = a["messages"][0]["blocks"], b["messages"][0]["blocks"]
    assert [x["type"] for x in blocks_a] == ["text", "attachment"]
    assert [x["text"] for x in blocks_a] == [x["text"] for x in blocks_b]
    assert blocks_a[0]["text"] == "Created Gemini Canvas “Quarterly Plan” — markdown, %d chars. Stored as artifact." % len(BODY.strip("\n"))
    att_a, att_b = blocks_a[1]["attachment"], blocks_b[1]["attachment"]
    for k in ("filename", "ext", "mime", "bytes", "content", "role", "availability"):
        assert att_a[k] == att_b[k]
    assert att_a["role"] == "artifact" and att_a["ext"] == "md" and att_a["filename"] == "Quarterly Plan.md"
    assert att_a["content"] == BODY.strip("\n")  # outer fence stripped, entities unescaped
    assert a["messages"][0]["role"] == "system"


def test_probable_source_chat_is_recorded_not_linked(tmp_path):
    for c in (_canvas(_json_file(tmp_path)), _canvas(_html_file(tmp_path))):
        src = c["messages"][0]["blocks"][0]["raw"]["probable_source_chat"]
        assert [s["native_id"] for s in src] == ["aa11bb22cc33"]
        assert src[0]["minutes_apart"] == pytest.approx(1.53, abs=0.05)


def test_empty_html_name_falls_back_to_html_title_then_heading(tmp_path):
    page = "```html\n<!DOCTYPE html><html><head><title>Generated widgets</title></head><body>x</body></html>\n```"
    c = _canvas(_html_file(tmp_path, name="", content=page))
    assert c["title"] == "Gemini Canvas: Generated widgets"
    att = c["messages"][0]["blocks"][1]["attachment"]
    assert att["ext"] == "html" and att["mime"] == "text/html" and att["filename"] == "Generated widgets.html"
    assert "```" not in att["content"]
    md = _canvas(_html_file(tmp_path, name="", content=FENCED, fname="b.html"))
    assert md["title"] == "Gemini Canvas: Quarterly Plan"
    plain = _canvas(_html_file(tmp_path, name="", content="just a line of prose", fname="c.html"))
    assert plain["title"] == "Gemini Canvas: just a line of prose"
    none = _canvas(_html_file(tmp_path, name="", content="", fname="d.html"))
    assert none["title"] == "Gemini Canvas: Untitled canvas"


def test_canvas_content_is_an_artifact_not_chat_text(conn, tmp_path):
    ingest.ingest_path(conn, _json_file(tmp_path))
    assert not search.search(conn, "zebra"), "canvas text must not be a chat-block hit"
    hits = attachments_mod.search(conn, "zebra")
    assert len(hits) == 1 and hits[0]["filename"] == "Quarterly Plan.md"
    role = conn.execute("SELECT role FROM attachment_links WHERE attachment_id = ?", (hits[0]["id"],)).fetchone()[0]
    assert role == "artifact"
    texts = [r[0] for r in conn.execute(
        "SELECT b.text FROM blocks b JOIN conversations c ON c.id = b.conv_id WHERE c.native_id LIKE 'canvas:%' AND b.type = 'text'"
    )]
    assert len(texts) == 1 and "zebra" not in texts[0]
    tag = conn.execute(
        "SELECT 1 FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = 'gemini/canvas'"
    ).fetchone()
    assert tag


def test_reingesting_the_other_variant_adds_no_conversation(conn, tmp_path):
    ingest.ingest_path(conn, _json_file(tmp_path))
    before = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    ingest.ingest_path(conn, _html_file(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == before


def test_other_events_hash_the_normalised_title_and_second():
    a = gemini.event_native_id("Cleared previous\xa0feedback", "2026-09-16T17:08:02.250Z")
    b = gemini.event_native_id("Cleared previous feedback", "2026-09-16T17:08:02Z")
    assert a == b


# ---- repair --------------------------------------------------------------


def _old_json_row(conn, tmp_path):
    """What the pre-fix adapter stored for the JSON-variant canvas."""
    record = orjson.loads(_json_file(tmp_path).read_bytes())[1]
    title = record["title"]
    cir = make_conversation(
        platform="gemini", native_id="event:oldjson0000000001", title=title, project=None,
        started_at=record["time"], ended_at=record["time"], source_path="old.json", raw={**record, "event_only": True},
        messages=[make_message(0, "system", None, record["time"], [
            make_block(0, "system", title, raw=record), make_block(1, "text", record["subtitles"][0]["name"], raw=record["subtitles"][0]),
        ], raw={**record, "event_only": True})],
    )
    return loader.load_conversation(conn, cir, source_file_id=None).conv_id


def _old_html_row(conn, tmp_path):
    cell = _cell(_html_zone("Quarterly Plan", FENCED), "Sep 16, 2026, 7:08:02 PM CEST")
    rec = gemini._parse_html_cell(cell.split('<div class="outer-cell', 1)[1])
    cir = make_conversation(
        platform="gemini", native_id="event:oldhtml000000002", title=rec["title"] + " " + "pad " * 100, project=None,
        started_at=rec["time"], ended_at=rec["time"], source_path="old.html", raw={"html_cell": True, "event_only": True},
        messages=[make_message(0, "system", None, rec["time"], [make_block(0, "system", rec["title"], raw=rec["raw_cell"])],
                               raw={"html_cell": True, "event_only": True})],
    )
    cir["title"] = rec["title"] + " " + "pad " * 100  # bypass: simulate an old uncapped title
    return loader.load_conversation(conn, cir, source_file_id=None).conv_id


@pytest.fixture
def old_vault(conn, tmp_path):
    ids = (_old_json_row(conn, tmp_path), _old_html_row(conn, tmp_path))
    conn.execute("UPDATE conversations SET title = title || ?  WHERE id = ?", ("x" * 300, ids[1]))  # long old title
    conn.commit()
    return ids


def test_repair_matches_adapter_output_folds_duplicates_and_is_idempotent(conn, tmp_path, old_vault):
    rep = repair.gemini_canvas(conn, apply=False)
    assert rep["canvases_live"] == 2 and len(rep["duplicate_pairs"]) == 1
    assert conn.execute("SELECT COUNT(*) FROM v_conversations WHERE native_id LIKE 'event:%'").fetchone()[0] == 2  # dry-run wrote nothing

    rep = repair.gemini_canvas(conn, apply=True)
    assert rep["tagged"] == 2
    live = conn.execute("SELECT * FROM v_conversations").fetchall()
    assert len(live) == 1
    kept = live[0]
    assert kept["id"] == old_vault[0] and kept["title"] == "Gemini Canvas: Quarterly Plan"
    folded = conn.execute("SELECT superseded_by, uid FROM conversations WHERE id = ?", (old_vault[1],)).fetchone()
    assert folded["superseded_by"] == kept["id"] and "#superseded-" in folded["uid"]

    # Same shape a fresh ingest of the new adapter gives.
    fresh = _canvas(_json_file(tmp_path, fname="fresh.json"))
    assert kept["native_id"] == fresh["native_id"]
    blocks = conn.execute(
        "SELECT b.type, b.text, b.excluded, b.idx FROM blocks b WHERE b.conv_id = ? ORDER BY b.idx", (kept["id"],)
    ).fetchall()
    live_blocks = [b for b in blocks if not b["excluded"]]
    assert [(b["type"], b["text"]) for b in live_blocks] == [
        (b["type"], b["text"]) for b in fresh["messages"][0]["blocks"]
    ]
    assert len(blocks) == 4 and all(b["excluded"] for b in blocks if b["idx"] >= 1000)  # old content kept, excluded
    reason = conn.execute("SELECT COUNT(*) FROM exclusion_log WHERE reason = ?", (repair.EXCLUDE_REASON,)).fetchone()[0]
    assert reason == 3  # two legacy blocks of the JSON row, one of the folded HTML row
    att = attachments_mod.search(conn, "zebra")
    assert len(att) == 1
    assert not search.search(conn, "zebra")

    # Re-ingesting the new adapter's output is now a no-op.
    ingest.ingest_path(conn, _json_file(tmp_path, fname="again.json"))
    assert conn.execute("SELECT COUNT(*) FROM v_conversations WHERE native_id LIKE 'canvas:%'").fetchone()[0] == 1

    snapshot = conn.execute("SELECT COUNT(*), SUM(id), SUM(length(title)) FROM conversations").fetchone()[:]
    again = repair.gemini_canvas(conn, apply=True)
    assert again["rows_to_rewrite"] == 0 and not again["duplicate_pairs"] and not again["titles_normalised"]
    assert conn.execute("SELECT COUNT(*), SUM(id), SUM(length(title)) FROM conversations").fetchone()[:] == snapshot


def test_repair_normalises_other_titles(conn):
    cir = make_conversation(
        platform="synthetic", native_id="n1", title="ok", project=None, started_at=None, ended_at=None,
        source_path="s", raw={}, messages=[make_message(0, "user", None, None, [make_block(0, "text", "hi", raw={})], raw={})],
    )
    cid = loader.load_conversation(conn, cir, source_file_id=None).conv_id
    conn.execute("UPDATE conversations SET title = ? WHERE id = ?", ("two\nlines\xa0here", cid))
    conn.commit()
    rep = repair.gemini_canvas(conn, apply=True)
    assert rep["titles_normalised_live"] == {"synthetic": 1}
    assert conn.execute("SELECT title FROM conversations WHERE id = ?", (cid,)).fetchone()[0] == "two lines here"


# ---- stale passages -------------------------------------------------------


def test_stale_passages_are_removed(conn, monkeypatch):
    from chatvault import embeddings
    from tests.test_embeddings import _fake_embed

    monkeypatch.setattr(embeddings, "embed_text", _fake_embed)
    long_text = "filler sentence about nothing in particular. " * 8
    cir = make_conversation(
        platform="synthetic", native_id="p1", title="t", project=None, started_at=None, ended_at=None,
        source_path="s", raw={},
        messages=[make_message(i, "user" if i % 2 == 0 else "assistant", None, None,
                               [make_block(0, "text", long_text + str(i), raw={})], raw={}) for i in range(3)],
    )
    cid = loader.load_conversation(conn, cir, source_file_id=None).conv_id
    conn.commit()
    assert ce.index_chunks(conn, conv_ids=[cid]).embedded == 3
    assert ce.status(conn)["stale"] == 0

    first = conn.execute("SELECT id FROM blocks WHERE conv_id = ? ORDER BY id LIMIT 1", (cid,)).fetchone()[0]
    conn.execute("UPDATE blocks SET excluded = 1 WHERE id = ?", (first,))
    second = conn.execute("SELECT id FROM blocks WHERE conv_id = ? ORDER BY id LIMIT 1 OFFSET 1", (cid,)).fetchone()[0]
    conn.execute("UPDATE blocks SET text = 'short now', char_count = 9 WHERE id = ?", (second,))
    conn.commit()
    assert ce.status(conn)["stale"] == 2
    assert ce.index_chunks(conn, conv_ids=[cid]).purged == 2
    assert ce.status(conn)["stale"] == 0
    assert conn.execute("SELECT COUNT(*) FROM block_embeddings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM block_embedding_meta").fetchone()[0] == 1

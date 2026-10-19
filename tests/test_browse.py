from __future__ import annotations

from chatvault import browse, export, ingest, loader
from chatvault.adapters.base import make_block, make_conversation, make_message
from tests.conftest import FIXTURES


def _load(conn, cir):
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result


def _cir(native_id, title, started_at, messages, platform="synthetic", project=None):
    return make_conversation(
        platform=platform,
        native_id=native_id,
        title=title,
        project=project,
        started_at=started_at,
        ended_at=started_at,
        source_path="synthetic",
        raw={},
        messages=messages,
    )


def _text_msg(idx, role, text, created_at):
    return make_message(idx, role, None, created_at, [make_block(0, "text", text, raw={})], raw={})


def test_find_no_query_returns_most_recent_first(conn):
    _load(conn, _cir("a", "Older one", "2026-01-01T00:00:00", [_text_msg(0, "user", "hi", "2026-01-01T00:00:00")]))
    _load(conn, _cir("b", "Newer one", "2026-02-01T00:00:00", [_text_msg(0, "user", "hi", "2026-02-01T00:00:00")]))

    rows, total = browse.find(conn)
    assert total == 2
    assert rows[0]["title"] == "Newer one"
    assert rows[1]["title"] == "Older one"


def test_find_prints_total_count_even_when_limited(conn):
    for i in range(5):
        _load(conn, _cir(f"c{i}", f"conv {i}", f"2026-01-0{i+1}T00:00:00", [_text_msg(0, "user", "hi", "2026-01-01T00:00:00")]))

    rows, total = browse.find(conn, limit=2)
    assert len(rows) == 2
    assert total == 5


def test_find_matches_titles_before_body_text(conn):
    _load(
        conn,
        _cir(
            "d1", "Something else entirely", "2026-01-01T00:00:00",
            [_text_msg(0, "user", "this message mentions protobuf deep inside the body", "2026-01-01T00:00:00")],
        ),
    )
    _load(
        conn,
        _cir(
            "d2", "Protobuf schema design", "2026-01-02T00:00:00",
            [_text_msg(0, "user", "unrelated body text", "2026-01-02T00:00:00")],
        ),
    )

    rows, total = browse.find(conn, "protobuf")
    assert total == 2
    assert rows[0]["title"] == "Protobuf schema design"
    assert rows[1]["title"] == "Something else entirely"


def test_find_project_column_and_attachment_count(conn):
    _load(
        conn,
        _cir(
            "e1", "Has an attachment", "2026-01-01T00:00:00",
            [make_message(0, "user", None, "2026-01-01T00:00:00",
                          [make_block(0, "text", "hi", raw={}), make_block(1, "attachment", "file.txt", raw={})],
                          raw={})],
            project="Second Brain",
        ),
    )

    rows, _total = browse.find(conn)
    assert rows[0]["project"] == "Second Brain"
    assert rows[0]["attachment_count"] == 1


def test_find_has_attachments_filter(conn):
    _load(conn, _cir("f1", "no attachment", "2026-01-01T00:00:00", [_text_msg(0, "user", "hi", "2026-01-01T00:00:00")]))
    _load(
        conn,
        _cir(
            "f2", "has attachment", "2026-01-02T00:00:00",
            [make_message(0, "user", None, "2026-01-02T00:00:00",
                          [make_block(0, "attachment", "file.txt", raw={})], raw={})],
        ),
    )

    rows, total = browse.find(conn, has_attachments=True)
    assert total == 1
    assert rows[0]["title"] == "has attachment"


def test_pick_cache_survives_when_db_unchanged(vault, conn):
    _load(conn, _cir("g1", "conv one", "2026-01-01T00:00:00", [_text_msg(0, "user", "hi", "2026-01-01T00:00:00")]))
    rows, _total = browse.find(conn)
    browse.save_result_cache(conn, vault, rows, {})

    conv_ids = browse.resolve_pick(conn, vault, "1")
    assert conv_ids == [rows[0]["id"]]


def test_pick_cache_invalidates_on_db_change(vault, conn):
    _load(conn, _cir("h1", "conv one", "2026-01-01T00:00:00", [_text_msg(0, "user", "hi", "2026-01-01T00:00:00")]))
    rows, _total = browse.find(conn)
    browse.save_result_cache(conn, vault, rows, {})

    # PRAGMA data_version only reflects writes from a *different* connection
    # (a real ingest between two `chatvault` CLI invocations is exactly
    # that — a separate process/connection), so use a second connection here
    # rather than the shared `conn` fixture to exercise that path honestly.
    from chatvault.db import connect

    other_conn = connect(vault)
    try:
        ingest.ingest_path(other_conn, FIXTURES / "claude_desktop" / "conversations.json")
    finally:
        other_conn.close()

    try:
        browse.resolve_pick(conn, vault, "1")
        assert False, "expected StaleCacheError"
    except browse.StaleCacheError:
        pass


def test_pick_without_a_prior_find_raises(vault, conn):
    try:
        browse.resolve_pick(conn, vault, "1")
        assert False, "expected StaleCacheError"
    except browse.StaleCacheError:
        pass


def test_pick_multiple_rows(vault, conn):
    _load(conn, _cir("i1", "one", "2026-01-01T00:00:00", [_text_msg(0, "user", "hi", "2026-01-01T00:00:00")]))
    _load(conn, _cir("i2", "two", "2026-01-02T00:00:00", [_text_msg(0, "user", "hi", "2026-01-02T00:00:00")]))
    rows, _total = browse.find(conn)
    browse.save_result_cache(conn, vault, rows, {})

    conv_ids = browse.resolve_pick(conn, vault, "1,2")
    assert len(conv_ids) == 2


def test_get_conversations_by_ids_preserves_pick_order(conn):
    _load(conn, _cir("j1", "one", "2026-01-01T00:00:00", [_text_msg(0, "user", "hi", "2026-01-01T00:00:00")]))
    _load(conn, _cir("j2", "two", "2026-01-02T00:00:00", [_text_msg(0, "user", "hi", "2026-01-02T00:00:00")]))
    rows, _total = browse.find(conn, sort="oldest")
    id_one, id_two = rows[0]["id"], rows[1]["id"]

    picked = export.get_conversations_by_ids(conn, [id_two, id_one])
    assert [r["id"] for r in picked] == [id_two, id_one]


def test_find_hides_injected_context_unless_included(conn):
    token = "zqxinjectedtoken"
    msg = make_message(
        0, "user", None, "2026-01-01T00:00:00",
        [make_block(0, "injected_context", f"memory says {token}", raw={})], raw={},
    )
    _load(conn, _cir("inj", "Unrelated title", "2026-01-01T00:00:00", [msg]))

    rows, total, hidden = browse.find_with_hidden(conn, query=token)
    assert total == 0 and hidden == 1

    rows, total, hidden = browse.find_with_hidden(conn, query=token, include_injected=True)
    assert total == 1 and hidden == 0
    assert browse.find(conn, query=token)[1] == 0


def test_keyword_search_excludes_injected_by_default(conn):
    from chatvault import search as search_mod

    token = "zqxkeywordtoken"
    msg = make_message(
        0, "user", None, "2026-01-01T00:00:00",
        [make_block(0, "injected_context", f"note {token}", raw={})], raw={},
    )
    _load(conn, _cir("inj2", "T", "2026-01-01T00:00:00", [msg]))
    assert search_mod.search(conn, token) == []
    assert len(search_mod.search(conn, token, include_injected=True)) == 1

"""chunk_embeddings against the same fake embedder as test_embeddings: passage
vectors, additive schema, best-chunk ranking, the whole-conversation switch."""

from __future__ import annotations

import pytest

from chatvault import chunk_embeddings as ce
from chatvault import embeddings, loader
from chatvault.adapters.base import make_block, make_conversation, make_message
from tests.test_embeddings import _fake_embed

pytestmark = pytest.mark.usefixtures("fake_embedder")


@pytest.fixture
def fake_embedder(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_text", _fake_embed)


def _conv(conn, native_id, title, texts):
    cir = make_conversation(
        platform="synthetic", native_id=native_id, title=title, project=None,
        started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:00", source_path="synthetic", raw={},
        messages=[
            make_message(i, "user" if i % 2 == 0 else "assistant", None, "2026-01-01T00:00:00",
                         [make_block(0, "text", t, raw={})], raw={})
            for i, t in enumerate(texts)
        ],
    )
    cid = loader.load_conversation(conn, cir, source_file_id=None).conv_id
    conn.commit()
    return cid


LONG = "filler sentence about nothing in particular. " * 8  # > 200 chars


def test_short_blocks_get_no_vector(conn):
    cid = _conv(conn, "c1", "t", ["ok", "thanks", LONG + "npm", "sure"])
    r = ce.index_chunks(conn, conv_ids=[cid])
    assert r.embedded == 1
    assert conn.execute("SELECT COUNT(*) FROM block_embedding_meta").fetchone()[0] == 1


def test_window_prepends_preceding_blocks_not_following(conn):
    blocks = [(1, "the question that prompted it"), (2, LONG + "answer")]
    out = dict(ce.chunk_texts(blocks, ce.ChunkSettings(window=3)))
    assert out[2].startswith("the question that prompted it\n") and out[2].endswith("answer")
    assert dict(ce.chunk_texts(blocks, ce.ChunkSettings(window=1)))[2].startswith("filler")


def test_rerun_skips_unchanged_and_force_reembeds(conn):
    cid = _conv(conn, "c2", "t", [LONG + "docker", LONG + "npm"])
    assert ce.index_chunks(conn, conv_ids=[cid]).embedded == 2
    again = ce.index_chunks(conn, conv_ids=[cid])
    assert (again.embedded, again.skipped) == (0, 2)
    assert ce.index_chunks(conn, conv_ids=[cid], force=True).embedded == 2


def test_status_pending_and_ready(conn):
    cid = _conv(conn, "c3", "t", [LONG + "npm"])
    assert ce.status(conn)["pending"] == 1 and not ce.chunks_ready(conn)
    ce.index_chunks(conn)
    assert ce.status(conn)["pending"] == 0 and ce.chunks_ready(conn)
    assert cid not in ce.pending_conv_ids(conn)


def test_rank_is_best_chunk_not_mean_and_snippet_shows_winner(conn):
    # A: one npm passage drowned in unrelated ones. B: a single mediocre npm+docker passage.
    a = _conv(conn, "a", "A", [LONG + "finance", LONG + "medical", "short neutral lead-in", LONG + "npm", LONG + "finance"])
    b = _conv(conn, "b", "B", [LONG + "npm docker"])
    ce.index_chunks(conn)
    matches: dict = {}
    rows = embeddings.semantic_search(conn, "npm", limit=5, matches=matches)
    assert [r["id"] for r in rows][0] == a          # best single chunk wins despite A's unrelated chunks
    assert "npm" in matches[a].snippet               # the winning passage, not the first one
    assert matches[a].distance < matches[b].distance


def test_whole_conversation_flag_and_fallback_use_old_index(conn):
    cid = _conv(conn, "w", "npm thing", [LONG + "npm"])
    embeddings.index_conversations(conn, conv_ids=[cid])
    # passage index not built: default path == old index, no snippets
    m1: dict = {}
    assert [r["id"] for r in embeddings.semantic_search(conn, "npm", matches=m1)] == [cid]
    assert m1[cid].snippet == ""
    ce.index_chunks(conn)
    m2: dict = {}
    embeddings.semantic_search(conn, "npm", matches=m2)
    assert m2[cid].snippet != ""
    m3: dict = {}
    embeddings.semantic_search(conn, "npm", matches=m3, whole_conversation=True)
    assert m3[cid].snippet == ""


def test_excluded_conversation_drops_out_of_passage_results(conn):
    cid = _conv(conn, "x", "t", [LONG + "npm"])
    ce.index_chunks(conn)
    conn.execute("UPDATE conversations SET excluded = 1 WHERE id = ?", (cid,))
    conn.commit()
    assert embeddings.semantic_search(conn, "npm", limit=5) == []

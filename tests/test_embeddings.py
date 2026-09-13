"""Unit tests for chatvault.embeddings — all against a FAKE embedder (no
Ollama dependency in the test suite, same philosophy as every other network-
adjacent boundary in this codebase). The fake maps text to a small
deterministic vector so cosine/L2 distance behaves predictably: texts that
share a topic keyword end up close together, texts that don't end up far
apart — enough structure to test ranking logic honestly without a real
model. Real-model verification lives in the manual/production-scratch
pass documented in STATUS.md, not here."""

from __future__ import annotations

import hashlib
import struct

import pytest

from chatvault import embeddings, loader
from chatvault.adapters.base import make_block, make_conversation, make_message

TOPICS = ["npm", "docker", "medical", "finance"]


def _fake_embed(text: str, model: str = embeddings.MODEL, base_url: str = "") -> list[float]:
    text_l = text.lower()
    vec = [0.0] * embeddings.DIM
    for i, topic in enumerate(TOPICS):
        vec[i] = 1.0 if topic in text_l else 0.0
    # deterministic low-magnitude noise from the text hash so identical
    # topic-vectors for different texts don't collide into literally the
    # same point (keeps distances a strict, sortable ordering)
    h = hashlib.sha256(text.encode("utf-8")).digest()
    for i in range(len(TOPICS), embeddings.DIM):
        vec[i] = (h[i % len(h)] / 255.0) * 0.01
    return vec


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_text", _fake_embed)


def _load(conn, native_id, title, started_at, text="hello"):
    cir = make_conversation(
        platform="synthetic", native_id=native_id, title=title, project=None,
        started_at=started_at, ended_at=started_at, source_path="synthetic", raw={},
        messages=[make_message(0, "user", None, started_at, [make_block(0, "text", text, raw={})], raw={})],
    )
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result


# ─────────────── representative_text ───────────────


def test_representative_text_includes_title_and_early_blocks(conn):
    r = _load(conn, "a1", "Fixing npm PATH", "2026-01-01T00:00:00", text="npm keeps pointing at the wrong binary")
    text = embeddings.representative_text(conn, r.conv_id)
    assert "Fixing npm PATH" in text
    assert "npm keeps pointing" in text


def test_representative_text_caps_length(conn):
    long_text = "word " * 2000
    r = _load(conn, "b1", "long one", "2026-01-01T00:00:00", text=long_text)
    text = embeddings.representative_text(conn, r.conv_id)
    assert len(text) <= embeddings.MAX_CHARS


def test_representative_text_excludes_thinking_and_tool_blocks(conn):
    cir = make_conversation(
        platform="synthetic", native_id="c1", title="conv", project=None,
        started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:00", source_path="synthetic", raw={},
        messages=[make_message(
            0, "assistant", None, "2026-01-01T00:00:00",
            [
                make_block(0, "thinking", "secret internal reasoning", raw={}),
                make_block(1, "tool_use", "npm install docker", raw={}, tool_name="run"),
                make_block(2, "text", "here is my answer", raw={}),
            ],
            raw={},
        )],
    )
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    text = embeddings.representative_text(conn, result.conv_id)
    assert "secret internal reasoning" not in text
    assert "here is my answer" in text


# ─────────────── index_conversations ───────────────


def test_index_conversations_embeds_and_records_metadata(conn):
    r = _load(conn, "d1", "npm conflict", "2026-01-01T00:00:00", text="npm is broken")
    result = embeddings.index_conversations(conn, conv_ids=[r.conv_id])
    assert result.embedded == 1
    assert result.failed == 0

    meta = conn.execute("SELECT * FROM embedding_meta WHERE conv_id = ?", (r.conv_id,)).fetchone()
    assert meta is not None
    assert meta["model"] == embeddings.MODEL

    vec_row = conn.execute("SELECT conv_id FROM conv_embeddings WHERE conv_id = ?", (r.conv_id,)).fetchone()
    assert vec_row is not None


def test_index_conversations_skips_unchanged_text(conn):
    r = _load(conn, "e1", "npm conflict", "2026-01-01T00:00:00", text="npm is broken")
    embeddings.index_conversations(conn, conv_ids=[r.conv_id])
    result2 = embeddings.index_conversations(conn, conv_ids=[r.conv_id])
    assert result2.embedded == 0
    assert result2.skipped == 1


def test_index_conversations_force_reembeds(conn):
    r = _load(conn, "f1", "npm conflict", "2026-01-01T00:00:00", text="npm is broken")
    embeddings.index_conversations(conn, conv_ids=[r.conv_id])
    result2 = embeddings.index_conversations(conn, conv_ids=[r.conv_id], force=True)
    assert result2.embedded == 1


def test_index_conversations_skips_empty_text(conn):
    cir = make_conversation(
        platform="synthetic", native_id="g1", title="", project=None,
        started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:00", source_path="synthetic", raw={},
        messages=[],
    )
    result_load = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    result = embeddings.index_conversations(conn, conv_ids=[result_load.conv_id])
    assert result.embedded == 0
    assert result.skipped == 1


def test_index_conversations_defaults_to_every_live_conversation(conn):
    _load(conn, "h1", "one", "2026-01-01T00:00:00", text="npm topic")
    _load(conn, "h2", "two", "2026-01-02T00:00:00", text="docker topic")
    result = embeddings.index_conversations(conn)
    assert result.embedded == 2


def test_index_conversations_stops_on_unavailable(conn, monkeypatch):
    def raise_unavailable(text, model=embeddings.MODEL, base_url=""):
        raise embeddings.EmbeddingUnavailable("ollama is down")

    monkeypatch.setattr(embeddings, "embed_text", raise_unavailable)
    r1 = _load(conn, "i1", "one", "2026-01-01T00:00:00", text="npm topic")
    r2 = _load(conn, "i2", "two", "2026-01-02T00:00:00", text="docker topic")
    result = embeddings.index_conversations(conn, conv_ids=[r1.conv_id, r2.conv_id])
    assert result.unavailable is True
    assert result.embedded == 0


# ─────────────── status ───────────────


def test_status_counts(conn):
    r1 = _load(conn, "j1", "one", "2026-01-01T00:00:00", text="npm topic")
    _load(conn, "j2", "two", "2026-01-02T00:00:00", text="docker topic")
    embeddings.index_conversations(conn, conv_ids=[r1.conv_id])
    data = embeddings.status(conn)
    assert data["total"] == 2
    assert data["embedded"] == 1
    assert data["pending"] == 1
    assert data["unembeddable"] == 0
    assert data["stale"] == 0


def test_status_counts_unembeddable_conversation(conn):
    cir = make_conversation(
        platform="synthetic", native_id="o1", title="", project=None,
        started_at="2026-01-01T00:00:00", ended_at="2026-01-01T00:00:00", source_path="synthetic", raw={},
        messages=[],
    )
    result_load = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    data = embeddings.status(conn)
    assert data["total"] == 1
    assert data["embedded"] == 0
    assert data["pending"] == 0
    assert data["unembeddable"] == 1


def test_status_counts_are_internally_consistent_after_supersede(conn):
    """embedded + pending + unembeddable must equal total, always — the
    exact scenario that broke it: dedupe.supersede() leaves an
    embedding_meta row behind for a conversation that's no longer live."""
    from chatvault import dedupe

    r1 = _load(conn, "p1", "one", "2026-01-01T00:00:00", text="npm topic")
    r2 = _load(conn, "p2", "two", "2026-01-02T00:00:00", text="npm topic")
    embeddings.index_conversations(conn, conv_ids=[r1.conv_id, r2.conv_id])

    dedupe.supersede(conn, r1.uid, r2.uid)

    data = embeddings.status(conn)
    assert data["embedded"] + data["pending"] + data["unembeddable"] == data["total"]
    assert data["embedded"] <= data["total"]
    assert data["stale"] == 1


# ─────────────── semantic_search ───────────────


def test_semantic_search_ranks_by_topic_similarity(conn):
    npm_conv = _load(conn, "k1", "npm PATH issue", "2026-01-01T00:00:00", text="npm keeps resolving to the wrong binary")
    docker_conv = _load(conn, "k2", "docker networking", "2026-01-02T00:00:00", text="docker containers cannot reach each other")
    medical_conv = _load(conn, "k3", "medical question", "2026-01-03T00:00:00", text="i have a medical concern about my knee")
    embeddings.index_conversations(conn)

    hits = embeddings.semantic_search(conn, "trouble with npm package resolution", limit=3)
    assert hits[0]["id"] == npm_conv.conv_id
    assert medical_conv.conv_id not in {h["id"] for h in hits[:1]}
    assert docker_conv.conv_id  # sanity


def test_semantic_search_excludes_excluded_conversations(conn):
    from chatvault import curation

    r1 = _load(conn, "l1", "npm one", "2026-01-01T00:00:00", text="npm topic")
    r2 = _load(conn, "l2", "npm two", "2026-01-02T00:00:00", text="npm topic")
    embeddings.index_conversations(conn)
    curation.exclude(conn, "conversation", f"id = {r1.conv_id}", "test exclusion")

    hits = embeddings.semantic_search(conn, "npm topic", limit=10)
    ids = {h["id"] for h in hits}
    assert r1.conv_id not in ids
    assert r2.conv_id in ids


def test_semantic_search_respects_limit(conn):
    for i in range(5):
        _load(conn, f"m{i}", f"npm conv {i}", f"2026-01-0{i+1}T00:00:00", text="npm topic")
    embeddings.index_conversations(conn)
    hits = embeddings.semantic_search(conn, "npm topic", limit=2)
    assert len(hits) == 2


def test_semantic_search_with_no_embeddings_returns_empty(conn):
    _load(conn, "n1", "conv", "2026-01-01T00:00:00", text="npm topic")
    hits = embeddings.semantic_search(conn, "anything", limit=10)
    assert hits == []


def test_semantic_search_return_total(conn):
    for i in range(5):
        _load(conn, f"o{i}", f"npm conv {i}", f"2026-01-0{i+1}T00:00:00", text="npm topic")
    embeddings.index_conversations(conn)

    rows, total = embeddings.semantic_search(conn, "npm topic", limit=2, return_total=True)
    assert len(rows) == 2
    assert total == 5


def test_semantic_search_return_total_with_no_embeddings(conn):
    _load(conn, "p1", "conv", "2026-01-01T00:00:00", text="npm topic")
    rows, total = embeddings.semantic_search(conn, "anything", limit=10, return_total=True)
    assert rows == []
    assert total == 0


# ─────────────── ingest wiring ───────────────


def test_ingest_path_does_not_embed_by_default(conn, tmp_path):
    from chatvault import ingest

    fixture = tmp_path / "conv.json"
    fixture.write_text(
        """
        [{"uuid": "embed-default-1", "name": "npm thing", "created_at": "2026-01-01T00:00:00Z",
          "updated_at": "2026-01-01T00:00:00Z",
          "chat_messages": [{"uuid": "m1", "sender": "human", "text": "npm broke",
            "created_at": "2026-01-01T00:00:00Z", "content": [{"type": "text", "text": "npm broke"}]}]}]
        """
    )
    summary = ingest.ingest_path(conn, fixture)
    assert summary.embedding is None
    assert conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0] == 0


def test_ingest_path_embeds_when_requested(conn, tmp_path):
    from chatvault import ingest

    fixture = tmp_path / "conv.json"
    fixture.write_text(
        """
        [{"uuid": "embed-on-1", "name": "npm thing", "created_at": "2026-01-01T00:00:00Z",
          "updated_at": "2026-01-01T00:00:00Z",
          "chat_messages": [{"uuid": "m1", "sender": "human", "text": "npm broke",
            "created_at": "2026-01-01T00:00:00Z", "content": [{"type": "text", "text": "npm broke"}]}]}]
        """
    )
    summary = ingest.ingest_path(conn, fixture, embed=True)
    assert summary.embedding is not None
    assert summary.embedding.embedded == 1
    assert conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0] == 1


def test_ingest_path_embed_failure_does_not_fail_the_ingest(conn, tmp_path, monkeypatch):
    from chatvault import ingest

    def raise_unavailable(text, model=embeddings.MODEL, base_url=""):
        raise embeddings.EmbeddingUnavailable("down")

    monkeypatch.setattr(embeddings, "embed_text", raise_unavailable)

    fixture = tmp_path / "conv.json"
    fixture.write_text(
        """
        [{"uuid": "embed-fail-1", "name": "npm thing", "created_at": "2026-01-01T00:00:00Z",
          "updated_at": "2026-01-01T00:00:00Z",
          "chat_messages": [{"uuid": "m1", "sender": "human", "text": "npm broke",
            "created_at": "2026-01-01T00:00:00Z", "content": [{"type": "text", "text": "npm broke"}]}]}]
        """
    )
    summary = ingest.ingest_path(conn, fixture, embed=True)
    assert summary.totals["inserted"] == 1  # the ingest itself still succeeded
    assert summary.embedding.unavailable is True

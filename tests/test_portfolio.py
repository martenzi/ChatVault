from __future__ import annotations

from chatvault import loader, portfolio, tags
from chatvault.adapters.base import make_block, make_conversation, make_message


def _load(conn, native_id, title, started_at, blocks_by_role):
    """blocks_by_role: list of (role, text) tuples, one message per entry."""
    messages = []
    for idx, (role, text) in enumerate(blocks_by_role):
        messages.append(make_message(idx, role, None, started_at, [make_block(0, "text", text, raw={})], raw={}))
    cir = make_conversation(
        platform="synthetic", native_id=native_id, title=title, project=None,
        started_at=started_at, ended_at=started_at, source_path="synthetic", raw={},
        messages=messages,
    )
    result = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return result


# A realistic-length technical conversation — needs enough repeated keyword
# occurrences to clear is_technical's 0.2 threshold (score = sum of capped
# per-keyword contributions / 10.0), same as it would need to in the real
# corpus.
TECHNICAL_USER_TEXT = (
    "I want to compare two llm models using ollama on docker. "
    "First I will set up the python script, then debug the api calls. "
    "Let's benchmark model performance and evaluate the prompt engineering approach. "
    "I need to troubleshoot the workflow pipeline and test the agent automation. "
    "This is a jupyter notebook for the mcp server config."
)
TECHNICAL_ASSISTANT_TEXT = (
    "Here's how to compare the llm models: use the api with docker and ollama. "
    "The python script will benchmark performance, evaluate prompt engineering, "
    "and test the workflow pipeline agent automation in your jupyter notebook."
)

MEDICAL_TEXT = (
    "medical medical medical medical medication adhd doctor läkare psychiatr "
    "health symptom dosage treatment sjukskrivning sick leave"
)


def _load_technical(conn, native_id="t1", title="LLM testing session"):
    return _load(
        conn, native_id, title, "2026-01-01T00:00:00",
        [("user", TECHNICAL_USER_TEXT), ("assistant", TECHNICAL_ASSISTANT_TEXT)],
    )


# ─────────────── text extraction ───────────────


def test_full_text_includes_title_and_all_roles(conn):
    r = _load(conn, "a1", "My Title", "2026-01-01T00:00:00", [("user", "hello there"), ("assistant", "hi back")])
    text = portfolio.full_text(conn, r.conv_id)
    assert "My Title" in text
    assert "hello there" in text
    assert "hi back" in text


def test_user_text_excludes_assistant_role(conn):
    r = _load(conn, "b1", "conv", "2026-01-01T00:00:00", [("user", "user says this"), ("assistant", "assistant says that")])
    text = portfolio.user_text(conn, r.conv_id)
    assert "user says this" in text
    assert "assistant says that" not in text


# ─────────────── is_technical (script 1, verbatim scoring) ───────────────


def test_is_technical_true_for_dense_technical_text(conn):
    is_tech, score, matched = portfolio.is_technical(TECHNICAL_USER_TEXT + TECHNICAL_ASSISTANT_TEXT, "LLM testing")
    assert is_tech is True
    assert score > 0.2
    assert matched


def test_is_technical_false_for_sparse_technical_text():
    is_tech, score, _matched = portfolio.is_technical("I used python once", "casual")
    assert is_tech is False


def test_is_technical_excludes_medical_content():
    is_tech, score, matched = portfolio.is_technical(MEDICAL_TEXT, "doctor visit")
    assert is_tech is False
    assert score == 0.0
    assert matched == []


def test_is_technical_exclusion_requires_more_than_three_occurrences():
    """Exact port detail: exclude only fires if the excluded keyword occurs
    more than 3 times in the full text — a single passing mention of a
    medical word in an otherwise technical conversation must not zero it out."""
    text = "medical " + TECHNICAL_USER_TEXT + TECHNICAL_ASSISTANT_TEXT
    is_tech, score, _matched = portfolio.is_technical(text, "LLM testing with one medical mention")
    assert is_tech is True  # 'medical' appears once, well under the >3 threshold


# ─────────────── extract_llm_testing_evidence ───────────────


def test_extract_llm_testing_evidence_finds_categories():
    evidence = portfolio.extract_llm_testing_evidence(TECHNICAL_USER_TEXT.lower())
    assert evidence is not None
    categories = {c["category"] for c in evidence["categories"]}
    assert "comparative_testing" in categories  # 'compare', 'benchmark', 'evaluate', 'test'
    assert "prompt_engineering" in categories  # 'prompt'
    assert evidence["testing_score"] > 0.2


def test_extract_llm_testing_evidence_none_when_no_indicators():
    assert portfolio.extract_llm_testing_evidence("nothing relevant here at all") is None


# ─────────────── reasoning patterns ───────────────


def test_analyze_reasoning_core_matches_systematic_and_debugging():
    text = "first i will debug this step by step, then troubleshoot and fix the error"
    results = portfolio.analyze_reasoning_core(text)
    assert "systematic_approach" in results
    assert "debugging_methodology" in results
    assert all(v > 0 for v in results.values())


def test_analyze_reasoning_extended_has_patterns_not_in_core():
    text = "i want to learn and understand how this works, and optimize it to be more efficient"
    results = portfolio.analyze_reasoning_extended(text)
    assert "learning_orientation" in results
    assert "efficiency_focus" in results
    # these two pattern names don't exist in the core (6-type) set at all
    assert "learning_orientation" not in portfolio.REASONING_PATTERNS_CORE
    assert "efficiency_focus" not in portfolio.REASONING_PATTERNS_CORE


def test_analyze_llm_exploration_matches_model_comparison():
    text = "should i use claude or gpt, which model is better"
    results = portfolio.analyze_llm_exploration(text)
    assert "model_comparison" in results


def test_reasoning_core_and_extended_are_distinct_dicts():
    """The overlap is deliberate but they must not be the same dictionary —
    debugging_methodology (core) and debugging_mindset (extended) have
    different indicator lists even though conceptually similar."""
    assert portfolio.REASONING_PATTERNS_CORE is not portfolio.REASONING_PATTERNS_EXTENDED
    assert "debugging_methodology" in portfolio.REASONING_PATTERNS_CORE
    assert "debugging_mindset" in portfolio.REASONING_PATTERNS_EXTENDED
    assert "debugging_methodology" not in portfolio.REASONING_PATTERNS_EXTENDED


# ─────────────── script 2's simpler gate ───────────────


def test_is_technical_v2_gate_requires_at_least_three_keywords():
    assert portfolio.is_technical_v2_gate("python docker api model workflow") is True
    assert portfolio.is_technical_v2_gate("python only") is False


# ─────────────── skill validations ───────────────


def test_match_skill_validations_finds_matching_skills():
    text = "i wrote a python script and used docker for deployment, then had to debug an error"
    matched = portfolio.match_skill_validations(text)
    dev_skills = matched.get("Development Skills", [])
    assert "Python Development" in dev_skills
    assert "Docker & Containerization" in dev_skills
    problem_skills = matched.get("Problem-Solving Skills", [])
    assert "Technical Debugging" in problem_skills


# ─────────────── analyze_conversation (end to end, one conversation) ───────────────


def test_analyze_conversation_technical_earns_expected_tag_families(conn):
    r = _load_technical(conn)
    earned = portfolio.analyze_conversation(conn, r.conv_id)

    assert "value/technical" in earned
    assert any(t.startswith("skill/llm-evaluation/") for t in earned)
    assert any(t.startswith("skill/reasoning-core/") for t in earned)
    assert any(t.startswith("skill/validated/") for t in earned)


def test_analyze_conversation_non_technical_earns_nothing_from_script1(conn):
    r = _load(conn, "c1", "chit chat", "2026-01-01T00:00:00", [("user", "how's the weather today, nice and sunny"), ("assistant", "yes lovely")])
    earned = portfolio.analyze_conversation(conn, r.conv_id)
    assert "value/technical" not in earned
    assert not any(t.startswith("skill/llm-evaluation/") for t in earned)


# ─────────────── run() — tag application ───────────────


def test_run_applies_tags_with_given_source(conn):
    r = _load_technical(conn)
    result = portfolio.run(conn, conv_ids=[r.conv_id], source="llm")

    assert result.conversations_scanned == 1
    assert result.technical_conversations == 1
    assert result.tag_links_written > 0

    row = conn.execute(
        "SELECT DISTINCT source FROM tag_links WHERE target_type = 'conversation' AND target_id = ?",
        (r.conv_id,),
    ).fetchall()
    assert {r["source"] for r in row} == {"llm"}


def test_run_dry_run_writes_nothing(conn):
    r = _load_technical(conn)
    result = portfolio.run(conn, conv_ids=[r.conv_id], dry_run=True)
    assert result.tag_links_written > 0  # computed
    assert conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0] == 0  # nothing written


def test_run_is_idempotent(conn):
    r = _load_technical(conn)
    portfolio.run(conn, conv_ids=[r.conv_id])
    n1 = conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0]
    portfolio.run(conn, conv_ids=[r.conv_id])
    n2 = conn.execute("SELECT COUNT(*) FROM tag_links").fetchone()[0]
    assert n1 == n2


def test_run_default_conv_ids_is_every_live_conversation(conn):
    r1 = _load_technical(conn, "d1")
    r2 = _load(conn, "d2", "chit chat", "2026-01-02T00:00:00", [("user", "hi"), ("assistant", "hello")])
    result = portfolio.run(conn)
    assert result.conversations_scanned == 2
    assert r1.conv_id and r2.conv_id  # sanity


# ─────────────── the acceptance requirement: full, selective undo ───────────────


def test_full_undo_via_untag_source_removes_exactly_what_was_added_and_nothing_else(conn):
    r1 = _load_technical(conn, "e1")
    r2 = _load_technical(conn, "e2")

    # a manual tag on the same conversation, predating the portfolio run —
    # must survive the undo untouched
    tags.tag_add(conn, "project/claude-web/manual-tag", f"id = {r1.conv_id}", source="manual")

    result = portfolio.run(conn, conv_ids=[r1.conv_id, r2.conv_id], source="llm")
    assert result.tag_links_written > 0

    llm_count_before = conn.execute("SELECT COUNT(*) FROM tag_links WHERE source = 'llm'").fetchone()[0]
    manual_count_before = conn.execute("SELECT COUNT(*) FROM tag_links WHERE source = 'manual'").fetchone()[0]
    assert llm_count_before == result.tag_links_written
    assert manual_count_before == 1

    removed = tags.untag(conn, source="llm")
    assert removed == llm_count_before

    assert conn.execute("SELECT COUNT(*) FROM tag_links WHERE source = 'llm'").fetchone()[0] == 0
    # the manual tag is untouched
    assert conn.execute("SELECT COUNT(*) FROM tag_links WHERE source = 'manual'").fetchone()[0] == 1
    manual_row = conn.execute(
        "SELECT t.name FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE tl.source = 'manual'"
    ).fetchone()
    assert manual_row["name"] == "project/claude-web/manual-tag"


def test_untag_source_only_requires_name_or_source():
    import pytest

    from chatvault.tags import untag as untag_fn

    # calling with neither name nor source is a programming error, not a
    # silent "delete everything" — must raise
    class _FakeConn:
        pass

    with pytest.raises(ValueError):
        untag_fn(_FakeConn())


# ─────────────── render_report ───────────────


def test_render_report_produces_readable_markdown(conn):
    r = _load_technical(conn)
    result = portfolio.run(conn, conv_ids=[r.conv_id])
    report = portfolio.render_report(result)

    assert "# Portfolio Analysis" in report
    assert "## LLM Testing Evidence" in report
    assert "## Reasoning Profile — Core" in report
    assert "## Reasoning Profile — Extended" in report
    assert "## Skills Validation" in report
    assert "conversation(s)" in report


def test_render_report_shows_confidence_tiers(conn):
    # three separate technical conversations all matching 'Python Development'
    for i in range(3):
        _load_technical(conn, f"f{i}", f"session {i}")
    result = portfolio.run(conn)
    report = portfolio.render_report(result)
    assert "HIGH" in report or "MEDIUM" in report

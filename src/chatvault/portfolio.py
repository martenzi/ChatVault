from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from chatvault import export as export_mod
from chatvault import tags as tags_mod

# ═══════════════════════════════════════════════════════════════════════
# Ported from technical_portfolio_extractor.py and reasoning_profile_
# analyzer.py, Build Order Step 8. Every keyword list, pattern dictionary,
# and threshold below is copied verbatim from the original scripts — this
# is proven, hand-tuned logic the user built and trusted for real resume
# work; nothing here is re-tuned or "improved." What changed from the
# originals:
#   - Input: SQLite queries against v_blocks/v_messages (role field) instead
#     of walking a directory of markdown files and regexing out
#     '"sender": "human"...' JSON-shaped fragments. The regex was fragile by
#     the original author's own admission (three different fallback
#     patterns stacked in extract_user_messages); the role column is exact.
#   - Output: chatvault tags (source='llm', always — see run()) instead of
#     analysis_results.json + three/four markdown reports. The reports
#     still exist, via render_report() / --report, but as a live, re-
#     runnable view over the tags rather than the source of truth.
#   - Nothing about the scoring, matching, or thresholds changed.
#
# Both scripts' REASONING_PATTERNS overlap (6 types vs. an overlapping-but-
# distinct 10-type superset) but are NOT the same dictionary — kept as two
# separate tag subtrees (skill/reasoning-core/* from script 1,
# skill/reasoning-extended/* from script 2) rather than merged, per
# instruction: the user built both for a reason, and merging would silently
# drop one script's specific indicator set.
# ═══════════════════════════════════════════════════════════════════════


# ─────────────── technical_portfolio_extractor.py — verbatim ───────────────

TECHNICAL_KEYWORDS = {
    "high_priority": [
        "llm", "ai", "model", "prompt", "benchmark", "ollama", "claude", "gpt",
        "api", "docker", "python", "script", "code", "jupyter", "notebook",
        "mcp", "server", "agent", "workflow", "pipeline", "debug", "troubleshoot",
        "langchain", "embedding", "vector", "neo4j", "database", "sql",
        "conda", "environment", "git", "github", "automation", "batch",
        "export", "extract", "parse", "json", "yaml", "config",
        "test", "eval", "metric", "token", "context window", "inference",
        "openai", "anthropic", "mistral", "qwen", "huggingface",
    ],
    "medium_priority": [
        "terminal", "command", "bash", "shell", "macos", "linux", "vps",
        "install", "setup", "configure", "integration", "toolkit",
        "data", "analysis", "process", "transform", "migration",
        "architecture", "design", "system", "modular", "component",
    ],
    "exclude_topics": [
        "medication", "adhd", "sick leave", "sjukskrivning", "doctor", "läkare",
        "psychiatr", "medical", "health", "symptom", "dosage", "treatment",
        "tinder", "dating", "flirt", "relationship", "match",
        "a-kassa", "csn", "tax", "skatteverket", "deklaration", "k10",
        "veteranpoolen", "bolag", "småa", "salary", "lön",
        "barber", "haircut", "parking", "böter",
    ],
}

LLM_TESTING_INDICATORS = {
    "comparative_testing": [
        "compare", "comparison", "versus", "vs", "better than", "outperform",
        "benchmark", "evaluate", "test", "trial", "experiment",
    ],
    "model_analysis": [
        "bottleneck", "limitation", "context window", "token limit",
        "rate limit", "performance", "speed", "latency", "quality",
        "reasoning", "accuracy", "error", "hallucination",
    ],
    "prompt_engineering": [
        "prompt", "few-shot", "zero-shot", "chain of thought", "cot",
        "system prompt", "instruction", "template", "format",
    ],
    "infrastructure": [
        "ollama", "self-host", "local", "api", "endpoint", "docker",
        "deployment", "configuration", "setup",
    ],
    "workflow_development": [
        "agentic", "agent", "workflow", "pipeline", "automation",
        "chain", "orchestration", "multi-step",
    ],
}

# script 1's reasoning set — 6 types
REASONING_PATTERNS_CORE = {
    "systematic_approach": [
        "step by step", "first", "then", "next", "finally",
        "approach", "methodology", "strategy", "plan",
    ],
    "hypothesis_testing": [
        "hypothesis", "theory", "assume", "if...then", "test whether",
        "verify", "validate", "confirm", "disprove",
    ],
    "debugging_methodology": [
        "troubleshoot", "debug", "diagnose", "investigate", "root cause",
        "error", "fix", "solve", "resolution", "workaround",
    ],
    "architectural_thinking": [
        "architecture", "design", "structure", "modular", "component",
        "interface", "abstraction", "pattern", "separation of concerns",
    ],
    "experimental_mindset": [
        "experiment", "try", "explore", "prototype", "proof of concept",
        "iterate", "refine", "optimize", "improve",
    ],
    "meta_cognition": [
        "reverse engineer", "understand why", "underlying", "principle",
        "mechanism", "how it works", "conceptual",
    ],
}

# script 1's skill_validations mapping — used both for tagging
# (skill/validated/<group>/<skill>) and for the --report confidence tiers.
SKILL_VALIDATIONS = {
    "LLM/AI Skills": {
        "LLM Model Evaluation": ["benchmark", "compare", "evaluate", "model", "performance"],
        "Prompt Engineering": ["prompt", "few-shot", "instruction", "template"],
        "Agentic Workflow Design": ["agent", "workflow", "pipeline", "agentic"],
        "Self-Hosted LLM Deployment": ["ollama", "self-host", "local", "docker"],
    },
    "Development Skills": {
        "Python Development": ["python", "script", "jupyter", "notebook"],
        "API Integration": ["api", "endpoint", "integration", "rest"],
        "Docker & Containerization": ["docker", "container", "deployment"],
        "Automation & Scripting": ["automat", "script", "batch", "cron"],
    },
    "Problem-Solving Skills": {
        "Technical Debugging": ["debug", "troubleshoot", "fix", "error", "resolve"],
        "System Configuration": ["config", "setup", "install", "environment"],
        "Documentation": ["document", "readme", "guide", "explain"],
    },
}


# ─────────────── reasoning_profile_analyzer.py — verbatim ───────────────

# script 2's reasoning set — 10 types (overlaps with REASONING_PATTERNS_CORE
# but is a distinct dict with different indicator lists in places — e.g.
# 'debugging_mindset' here vs. 'debugging_methodology' above — plus four
# patterns script 1 doesn't have at all: efficiency_focus,
# learning_orientation, quality_awareness, risk_awareness).
REASONING_PATTERNS_EXTENDED = {
    "systematic_approach": {
        "indicators": [
            "step by step", "first", "then", "next", "finally", "before i",
            "approach", "methodology", "strategy", "plan", "organize",
            "sequence", "order", "prioritize", "systematic",
        ],
        "resume_value": "Methodical problem-solving",
    },
    "hypothesis_testing": {
        "indicators": [
            "hypothesis", "theory", "assume", "if", "test whether", "maybe",
            "could be", "might be", "suspect", "thinking it might",
            "verify", "validate", "confirm", "check if", "try to see",
        ],
        "resume_value": "Scientific method application",
    },
    "debugging_mindset": {
        "indicators": [
            "troubleshoot", "debug", "diagnose", "investigate", "root cause",
            "error", "fix", "solve", "why is", "what happened", "broken",
            "not working", "issue", "problem", "figure out",
        ],
        "resume_value": "Advanced debugging skills",
    },
    "architectural_thinking": {
        "indicators": [
            "architecture", "design", "structure", "modular", "component",
            "interface", "abstraction", "pattern", "separation",
            "workflow", "pipeline", "system", "framework",
        ],
        "resume_value": "Systems architecture thinking",
    },
    "experimental_mindset": {
        "indicators": [
            "experiment", "try", "explore", "prototype", "proof of concept",
            "iterate", "refine", "optimize", "improve", "test out",
            "see what happens", "curious", "wondering",
        ],
        "resume_value": "Experimental iteration",
    },
    "meta_cognition": {
        "indicators": [
            "reverse engineer", "understand why", "underlying", "principle",
            "mechanism", "how it works", "conceptual", "fundamentally",
            "bottleneck", "limitation", "constraint",
        ],
        "resume_value": "Deep conceptual analysis",
    },
    "efficiency_focus": {
        "indicators": [
            "efficient", "optimize", "faster", "better way", "automate",
            "streamline", "reduce", "minimize", "simplify", "avoid",
            "waste", "unnecessary", "overhead",
        ],
        "resume_value": "Efficiency optimization",
    },
    "learning_orientation": {
        "indicators": [
            "learn", "understand", "explain", "how does", "what is",
            "why does", "teach me", "new to", "getting into", "exploring",
            "study", "research", "documentation", "example",
        ],
        "resume_value": "Continuous learning",
    },
    "quality_awareness": {
        "indicators": [
            "correct", "accurate", "proper", "best practice", "standard",
            "reliable", "robust", "stable", "consistent", "clean",
            "maintainable", "readable", "well-structured",
        ],
        "resume_value": "Quality-focused development",
    },
    "risk_awareness": {
        "indicators": [
            "risk", "concern", "worry", "careful", "safe", "secure",
            "backup", "fallback", "what if", "edge case", "limitation",
            "caveat", "potential issue", "side effect",
        ],
        "resume_value": "Risk assessment",
    },
}

LLM_EXPLORATION_PATTERNS = {
    "model_comparison": {
        "indicators": [
            "compare", "versus", "vs", "better", "which model", "different model",
            "claude", "gpt", "ollama", "mistral", "llama", "qwen",
        ],
    },
    "capability_testing": {
        "indicators": [
            "can you", "are you able", "test if", "try to", "see if",
            "capability", "limit", "context window", "token",
        ],
    },
    "workflow_building": {
        "indicators": [
            "workflow", "pipeline", "automation", "integrate", "chain",
            "agentic", "agent", "tool", "mcp", "api",
        ],
    },
    "prompt_experimentation": {
        "indicators": [
            "prompt", "instruction", "system message", "few-shot",
            "template", "format", "structure", "output format",
        ],
    },
}

# script 2's own, simpler "is this worth analyzing" gate — deliberately kept
# separate from TECHNICAL_KEYWORDS/is_technical() above: it's a different,
# looser check from a different script, presence-based (>=3 of 19 keywords
# appearing at all) rather than weighted-count-based. A conversation can
# pass this gate without passing is_technical(), or vice versa — both
# scripts' original independent behavior, preserved as-is.
TECH_KEYWORDS_V2 = [
    "script", "code", "python", "api", "docker", "llm", "model",
    "workflow", "debug", "error", "config", "install", "server",
    "ollama", "claude", "gpt", "prompt", "test", "jupyter",
]


DEFAULT_SOURCE = "llm"


def _slug(text: str) -> str:
    return export_mod.slugify(text)


# ─────────────── text extraction (replaces the original regex) ───────────────


def full_text(conn: sqlite3.Connection, conv_id: int) -> str:
    """Title + every text block from every role — what the original scripts
    scanned the whole markdown file for (is_technical, LLM-testing evidence,
    skill validations all look at 'the conversation', not just one side of
    it)."""
    conv = conn.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    title = (conv["title"] if conv else "") or ""
    rows = conn.execute(
        """
        SELECT b.text FROM v_blocks b JOIN v_messages m ON m.id = b.msg_id
        WHERE b.conv_id = ? AND b.type = 'text'
        ORDER BY m.idx, b.idx
        """,
        (conv_id,),
    ).fetchall()
    return title + "\n" + "\n".join(r["text"] for r in rows if r["text"])


def user_text(conn: sqlite3.Connection, conv_id: int) -> str:
    """User-only text blocks — the exact replacement for both scripts'
    regex-based extract_user_messages(): the original hunted for
    '"sender": "human"...' JSON fragments inside markdown by three stacked
    fallback patterns; the role column already tells us this precisely."""
    rows = conn.execute(
        """
        SELECT b.text FROM v_blocks b JOIN v_messages m ON m.id = b.msg_id
        WHERE b.conv_id = ? AND b.type = 'text' AND m.role = 'user'
        ORDER BY m.idx, b.idx
        """,
        (conv_id,),
    ).fetchall()
    return "\n".join(r["text"] for r in rows if r["text"])


# ─────────────── technical_portfolio_extractor.py logic — verbatim ───────────────


def is_technical(text: str, title: str | None) -> tuple[bool, float, list[str]]:
    """Exact port of ConversationAnalyzer.is_technical_conversation. Only
    change: 'filename' -> 'title' (conversations don't have filenames);
    scoring and the 0.2 threshold are untouched."""
    content_lower = text.lower()
    title_lower = (title or "").lower()

    for exclude in TECHNICAL_KEYWORDS["exclude_topics"]:
        if exclude in content_lower[:2000] or exclude in title_lower:
            if content_lower.count(exclude) > 3:
                return False, 0.0, []

    matched = []
    score = 0.0

    for keyword in TECHNICAL_KEYWORDS["high_priority"]:
        count = content_lower.count(keyword)
        if count > 0:
            matched.append(f"{keyword}:{count}")
            score += min(count * 0.1, 1.0)

    for keyword in TECHNICAL_KEYWORDS["medium_priority"]:
        count = content_lower.count(keyword)
        if count > 0:
            matched.append(f"{keyword}:{count}")
            score += min(count * 0.05, 0.5)

    score = min(score, 10.0) / 10.0
    return score > 0.2, score, matched


def extract_llm_testing_evidence(content_lower: str) -> dict | None:
    """Exact port of ConversationAnalyzer.extract_llm_testing_evidence's
    scoring (categories + testing_score). Sample-context snippets from the
    original are dropped here — not needed to decide which tags apply, only
    useful for prose in a report, and render_report() re-derives its own
    short excerpts separately rather than duplicating that regex here."""
    categories = []
    testing_score = 0.0
    for category, indicators in LLM_TESTING_INDICATORS.items():
        matches = [ind for ind in indicators if ind in content_lower]
        if matches:
            categories.append({"category": category, "matches": matches})
            testing_score += len(matches) * 0.1
    testing_score = min(testing_score, 1.0)
    if not categories:
        return None
    return {"categories": categories, "testing_score": testing_score}


def analyze_reasoning_core(user_text_lower: str) -> dict[str, int]:
    """Exact port of ConversationAnalyzer.analyze_reasoning_patterns'
    scoring (6-type REASONING_PATTERNS). Returns {pattern_type: strength}
    for every pattern with at least one indicator match — the original's
    'thinking_style' (top-3 by strength) is a report-time view over this,
    not a separate gate, so every matched pattern here is tag-eligible."""
    results: dict[str, int] = {}
    for pattern_type, indicators in REASONING_PATTERNS_CORE.items():
        strength = sum(user_text_lower.count(ind) for ind in indicators if ind in user_text_lower)
        if strength > 0:
            results[pattern_type] = strength
    return results


def match_skill_validations(content_lower: str) -> dict[str, list[str]]:
    """Exact port of the skill_validations keyword mapping. group -> list of
    matched skill names. The original computed this once, aggregated across
    the whole corpus, for a HIGH/MEDIUM confidence report; here it's
    evaluated per-conversation so it can become a tag (skill/validated/...),
    and the same HIGH/MEDIUM tiers fall out naturally from counting how many
    conversations carry that tag afterward — see render_report()."""
    matched: dict[str, list[str]] = {}
    for group, skills in SKILL_VALIDATIONS.items():
        for skill, keywords in skills.items():
            if any(kw in content_lower for kw in keywords):
                matched.setdefault(group, []).append(skill)
    return matched


# ─────────────── reasoning_profile_analyzer.py logic — verbatim ───────────────


def is_technical_v2_gate(content_lower: str) -> bool:
    """Exact port of process_conversations' inline tech_score check — a
    simpler presence-based pre-filter deciding whether script 2 bothers
    analyzing a conversation at all. Deliberately independent of
    is_technical() above (different script, different threshold, different
    keyword list) — not itself a tag, just a gate."""
    tech_score = sum(1 for kw in TECH_KEYWORDS_V2 if kw in content_lower)
    return tech_score >= 3


def analyze_reasoning_extended(user_text_lower: str) -> dict[str, int]:
    """Exact port of analyze_message_patterns' scoring (10-type
    REASONING_PATTERNS from reasoning_profile_analyzer.py). Returns
    {pattern_type: total_matches}."""
    results: dict[str, int] = {}
    for pattern_type, info in REASONING_PATTERNS_EXTENDED.items():
        total = sum(user_text_lower.count(ind) for ind in info["indicators"] if ind in user_text_lower)
        if total > 0:
            results[pattern_type] = total
    return results


def analyze_llm_exploration(user_text_lower: str) -> dict[str, int]:
    """Exact port of analyze_llm_exploration's scoring (4-type
    LLM_EXPLORATION_PATTERNS)."""
    results: dict[str, int] = {}
    for pattern_type, info in LLM_EXPLORATION_PATTERNS.items():
        total = sum(user_text_lower.count(ind) for ind in info["indicators"] if ind in user_text_lower)
        if total > 0:
            results[pattern_type] = total
    return results


# ─────────────── orchestration — tag taxonomy mapping ───────────────
#
#   value/technical                          <- is_technical() gate (script 1)
#   skill/llm-evaluation/<category>           <- LLM_TESTING_INDICATORS (script 1, testing_score > 0.2)
#   skill/reasoning-core/<pattern>             <- REASONING_PATTERNS_CORE (script 1, 6 types)
#   skill/validated/<group>/<skill>           <- SKILL_VALIDATIONS (script 1)
#   skill/reasoning-extended/<pattern>        <- REASONING_PATTERNS_EXTENDED (script 2, 10 types)
#   purpose/llm-exploration/<pattern>         <- LLM_EXPLORATION_PATTERNS (script 2)
#
# skill/reasoning-core vs skill/reasoning-extended is a deliberate split,
# not a naming accident: script 1's 6-pattern set and script 2's 10-pattern
# set overlap conceptually but are different dictionaries with different
# indicator lists (compare 'debugging_methodology' vs 'debugging_mindset').
# Merging them would silently drop one script's specific matches in favor
# of the other's; keeping both subtrees means neither script's judgment is
# lost, and `stats --by tag --prefix "skill/reasoning-*"` still lets you
# see them together when you want to.
#
# LLM_TESTING_INDICATORS (skill/llm-evaluation, script 1) and
# LLM_EXPLORATION_PATTERNS (purpose/llm-exploration, script 2) land in
# different trees on purpose: script 1's categories describe testing
# *actions taken* (comparative_testing, infrastructure work — "what does
# this prove you can do", the skill tree's own definition per v1.5 §2);
# script 2's categories describe *why the conversation exists* (model
# comparison curiosity, capability probing — "why did I open this chat",
# the purpose tree's own definition) — not a merge of the same data twice.
#
# Code-artifact extraction (script 1's extract_code_artifacts) is computed
# for --report but intentionally NOT tagged per-artifact: the requested tag
# taxonomy is conversation-level (per instruction 6 — this heuristic
# shouldn't over-claim precision it doesn't have), and one tag per code
# block would multiply into thousands of near-duplicate tags for no
# retrieval benefit `value/technical` + the skill tags don't already give.


def analyze_conversation(conn: sqlite3.Connection, conv_id: int) -> list[str]:
    """Runs both scripts' full, independent pipelines against one
    conversation and returns every tag name it earns. Read-only — callers
    decide whether/how to write the result."""
    text = full_text(conn, conv_id)
    content_lower = text.lower()
    u_text = user_text(conn, conv_id).lower()
    conv = conn.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    title = conv["title"] if conv else None

    earned: list[str] = []

    is_tech, _score, _matched = is_technical(text, title)
    if is_tech:
        earned.append("value/technical")

        llm_evidence = extract_llm_testing_evidence(content_lower)
        if llm_evidence and llm_evidence["testing_score"] > 0.2:
            for cat in llm_evidence["categories"]:
                earned.append(f"skill/llm-evaluation/{_slug(cat['category'])}")

        for pattern in analyze_reasoning_core(u_text):
            earned.append(f"skill/reasoning-core/{_slug(pattern)}")

        for group, skills in match_skill_validations(content_lower).items():
            for skill in skills:
                earned.append(f"skill/validated/{_slug(group)}/{_slug(skill)}")

    if is_technical_v2_gate(content_lower):
        for pattern in analyze_reasoning_extended(u_text):
            earned.append(f"skill/reasoning-extended/{_slug(pattern)}")
        for pattern in analyze_llm_exploration(u_text):
            earned.append(f"purpose/llm-exploration/{_slug(pattern)}")

    return earned


@dataclass
class PortfolioResult:
    conversations_scanned: int = 0
    technical_conversations: int = 0
    tag_links_written: int = 0
    # tag name -> conv_ids that earned it, across the whole run — the input
    # render_report() needs for per-tag counts and the skill/validated
    # HIGH/MEDIUM confidence tiers, without a second pass over the DB.
    tag_conv_ids: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))


def _apply_tag(conn: sqlite3.Connection, tag_name: str, conv_id: int, source: str, now: str) -> bool:
    tag_id = tags_mod.get_or_create_tag(conn, tag_name)
    cur = conn.execute(
        """
        INSERT INTO tag_links (tag_id, target_type, target_id, source, created_at)
        VALUES (?, 'conversation', ?, ?, ?)
        ON CONFLICT(tag_id, target_type, target_id) DO NOTHING
        """,
        (tag_id, conv_id, source, now),
    )
    return cur.rowcount > 0


def run(
    conn: sqlite3.Connection,
    conv_ids: list[int] | None = None,
    dry_run: bool = False,
    source: str = DEFAULT_SOURCE,
) -> PortfolioResult:
    """Analyzes conv_ids (default: every live conversation) and tags them
    (source='llm' by default — deliberately, not a new source value: the
    v1.4/Tagging-Workflow specs already anticipated exactly this scenario,
    'tag_links.source=llm... which the schema already supports', for
    AI-assisted bulk tagging reviewed by hand). Every tag this writes is
    undoable in one shot via `chatvault tag untag --source llm` regardless
    of tag name, independent of anything tagged manually or by another
    source on the same conversations.

    Commits once per conversation, not once per tag — a full-corpus run
    applies several tags to many conversations, and committing per tag_add
    call (the plain `tags.tag_add` does exactly that) would multiply into
    thousands of needless commits. Same reasoning as
    embeddings.index_conversations' incremental-commit fix: interruptible,
    but not needlessly slow.

    dry_run=True computes and counts everything without writing, for
    --dry-run and for tests that want the analysis without touching the DB."""
    ids = conv_ids if conv_ids is not None else [r[0] for r in conn.execute("SELECT id FROM v_conversations").fetchall()]
    result = PortfolioResult()

    for conv_id in ids:
        result.conversations_scanned += 1
        earned = analyze_conversation(conn, conv_id)
        if not earned:
            continue
        if "value/technical" in earned:
            result.technical_conversations += 1

        now = datetime.now(timezone.utc).isoformat()
        for tag_name in earned:
            result.tag_conv_ids[tag_name].add(conv_id)
            if dry_run:
                result.tag_links_written += 1
            elif _apply_tag(conn, tag_name, conv_id, source, now):
                result.tag_links_written += 1

        if not dry_run:
            conn.commit()

    return result


# ─────────────── --report — a human-readable view over the same data ───────────────
# Not a byte-identical recreation of the four original markdown reports —
# those included per-match sample-context snippets pulled by regex, which
# this port deliberately drops (see extract_llm_testing_evidence). This is
# a reasonable-fidelity summary of the same underlying evidence: which
# categories/patterns/skills matched, and how many conversations each one
# touched. Live and re-runnable, per the whole point of porting this into
# tags rather than a JSON snapshot (v1.5 §6).


def _bullet_list(tag_conv_ids: dict[str, set[int]], prefix: str) -> list[str]:
    lines = []
    for tag_name in sorted(tag_conv_ids):
        if not tag_name.startswith(prefix):
            continue
        n = len(tag_conv_ids[tag_name])
        label = tag_name[len(prefix):]
        lines.append(f"- **{label}** — {n} conversation(s)")
    return lines


def render_report(result: PortfolioResult) -> str:
    lines = [
        "# Portfolio Analysis",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()}",
        "",
        f"- Conversations scanned: {result.conversations_scanned}",
        f"- Technical conversations (`value/technical`): {result.technical_conversations}",
        f"- Tag applications: {result.tag_links_written}",
        "",
        "---",
        "",
        "## LLM Testing Evidence",
        "",
        "*From technical_portfolio_extractor.py's LLM_TESTING_INDICATORS —",
        "categories of evidence that you've tested/evaluated LLMs, not just used them.*",
        "",
    ]
    lines += _bullet_list(result.tag_conv_ids, "skill/llm-evaluation/") or ["*(none found)*"]

    lines += [
        "",
        "## Reasoning Profile — Core",
        "",
        "*6-pattern set from technical_portfolio_extractor.py's REASONING_PATTERNS.*",
        "",
    ]
    lines += _bullet_list(result.tag_conv_ids, "skill/reasoning-core/") or ["*(none found)*"]

    lines += [
        "",
        "## Reasoning Profile — Extended",
        "",
        "*10-pattern set from reasoning_profile_analyzer.py's REASONING_PATTERNS —",
        "overlaps conceptually with the core set above but is a distinct dictionary,",
        "kept separate rather than merged.*",
        "",
    ]
    lines += _bullet_list(result.tag_conv_ids, "skill/reasoning-extended/") or ["*(none found)*"]

    lines += [
        "",
        "## LLM Exploration Patterns",
        "",
        "*From reasoning_profile_analyzer.py's LLM_EXPLORATION_PATTERNS —",
        "why you were talking to an LLM in the first place.*",
        "",
    ]
    lines += _bullet_list(result.tag_conv_ids, "purpose/llm-exploration/") or ["*(none found)*"]

    lines += [
        "",
        "## Skills Validation",
        "",
        "*Confidence: **HIGH** (>=3 conversations show evidence), **MEDIUM** (>=1).*",
        "",
    ]
    by_group: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for tag_name, conv_ids in result.tag_conv_ids.items():
        if not tag_name.startswith("skill/validated/"):
            continue
        rest = tag_name[len("skill/validated/"):]
        group_slug, _, skill_slug = rest.partition("/")
        by_group[group_slug].append((skill_slug, len(conv_ids)))

    if not by_group:
        lines.append("*(none found)*")
    else:
        for group_slug in sorted(by_group):
            lines.append(f"### {group_slug}")
            lines.append("")
            for skill_slug, n in sorted(by_group[group_slug]):
                confidence = "HIGH" if n >= 3 else "MEDIUM"
                lines.append(f"- **{skill_slug}** — {confidence} ({n} conversation(s))")
            lines.append("")

    return "\n".join(lines)

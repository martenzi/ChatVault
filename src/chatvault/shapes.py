"""chatvault audit-shapes — a permanent diagnostic for what a platform's raw JSON
actually contains, as opposed to what an adapter assumed it contains.

Built for docs/50_SPEC_ingestion_fidelity_v1.md §4.2 and §4.2b. The standing
instruction that governs this module (spec §1.2) is: read every record fully,
never sample, never grep-and-conclude, never trust an adapter's assumptions as
a description of the data. Every function here walks *every* key path of
*every* record in scope — there is no LIMIT, no head, no early-exit sampling
anywhere in this module. If that instruction and a caller's convenience ever
conflict, the instruction wins.

This module is read-only: it only ever executes SELECT against the live
database. It must never write to blocks, messages, conversations, or any
other table.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path

# Starting-point vocabulary for the reasoning-locator sweep (spec §4.2b step 1).
# Explicitly NOT a definition — step 2 (every string-valued path not already
# contributing to blocks.text, ranked by volume) is what actually finds the
# answer. This list only seeds the "known reasoning vocabulary" cross-tab.
REASONING_KEY_SUFFIXES: tuple[str, ...] = (
    "thinking",
    "thought",
    "thoughts",
    "reasoning",
    "reasoning_content",
    "reasoningtext",
    "redacted_thinking",
    "chain_of_thought",
    "cot",
    "analysis",
    "deliberation",
    "scratchpad",
    "internal",
    "reflection",
)


# ────────────────────────── key-path walking ──────────────────────────


def walk_paths(value: object, prefix: str = "$") -> list[tuple[str, object]]:
    """Walk `value` to full depth, returning every (path, leaf_value) pair.

    A "leaf" is anything that is not itself a dict or a non-empty list/tuple —
    i.e. strings, numbers, bools, None, and empty containers are all leaves.
    Lists are walked by index (`path[0]`, `path[1]`, ...) so that occurrence
    counts and lengths are still comparable across records with different
    list lengths; the aggregation step normalises list indices away (see
    `_normalise_path`) so `items[0].text` and `items[3].text` count as the
    same path.
    """
    out: list[tuple[str, object]] = []
    if isinstance(value, dict):
        if not value:
            out.append((prefix, value))
            return out
        for key, sub in value.items():
            out.extend(walk_paths(sub, f"{prefix}.{key}"))
        return out
    if isinstance(value, (list, tuple)):
        if not value:
            out.append((prefix, value))
            return out
        for i, sub in enumerate(value):
            out.extend(walk_paths(sub, f"{prefix}[{i}]"))
        return out
    out.append((prefix, value))
    return out


def _normalise_path(path: str) -> str:
    """Collapse list indices so `items[0].text` and `items[3].text` aggregate
    as the same path (`items[].text`). This is what makes counts meaningful
    across records whose lists vary in length."""
    out = []
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == "[":
            j = path.index("]", i)
            out.append("[]")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list" if value else "empty_list"
    if isinstance(value, dict):
        return "dict" if value else "empty_dict"
    return type(value).__name__


# ────────────────────────── aggregation structures ──────────────────────────


@dataclass
class PathStats:
    path: str
    count: int = 0
    types: dict[str, int] = field(default_factory=dict)
    str_lengths: list[int] = field(default_factory=list)
    total_chars: int = 0
    example: str | None = None

    def record(self, value: object) -> None:
        self.count += 1
        t = _type_name(value)
        self.types[t] = self.types.get(t, 0) + 1
        if isinstance(value, str):
            self.str_lengths.append(len(value))
            self.total_chars += len(value)
            if self.example is None and value.strip():
                ex = value.strip().replace("\n", " ")
                self.example = ex[:117] + "..." if len(ex) > 120 else ex

    @property
    def min_len(self) -> int | None:
        return min(self.str_lengths) if self.str_lengths else None

    @property
    def median_len(self) -> float | None:
        return statistics.median(self.str_lengths) if self.str_lengths else None

    @property
    def max_len(self) -> int | None:
        return max(self.str_lengths) if self.str_lengths else None


@dataclass
class ShapeSignature:
    signature: str
    count: int = 0
    example_block_id: int | None = None


@dataclass
class ShapeAuditResult:
    platform: str
    type_filter: str | None
    record_count: int
    parse_errors: int
    paths: dict[str, PathStats]
    signatures: dict[str, ShapeSignature]


def _discriminator_value(rec: dict, key: str) -> str | None:
    v = rec.get(key)
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return None


def shape_signature(rec: dict) -> str:
    """The sorted set of top-level keys, plus discriminator fields
    (kind/type/role) if present — per spec §4.2 step 5."""
    keys = sorted(rec.keys()) if isinstance(rec, dict) else []
    parts = ["keys=" + ",".join(keys)]
    for disc in ("kind", "type", "role"):
        if isinstance(rec, dict) and disc in rec:
            parts.append(f"{disc}={_discriminator_value(rec, disc)!r}")
    return " | ".join(parts)


def audit_blocks(
    conn: sqlite3.Connection,
    platform: str,
    type_filter: str | None = None,
) -> ShapeAuditResult:
    """Full enumeration of every key path in every raw_json record for the
    given platform (optionally filtered to a single blocks.type), joined
    through v_blocks -> v_conversations. No LIMIT, no sampling — every row
    in scope is parsed and walked to full depth."""
    query = """
        SELECT b.id, b.raw_json, b.type, m.role
        FROM v_blocks b
        JOIN v_conversations c ON c.id = b.conv_id
        JOIN messages m ON m.id = b.msg_id
        WHERE c.platform = ?
    """
    params: list[object] = [platform]
    if type_filter is not None:
        query += " AND b.type = ?"
        params.append(type_filter)

    paths: dict[str, PathStats] = {}
    signatures: dict[str, ShapeSignature] = {}
    record_count = 0
    parse_errors = 0

    cur = conn.execute(query, params)
    for row in cur:
        record_count += 1
        raw = row["raw_json"]
        if raw is None or raw == "":
            parse_errors += 1
            continue
        try:
            rec = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parse_errors += 1
            continue

        for path, value in walk_paths(rec):
            npath = _normalise_path(path)
            stats = paths.setdefault(npath, PathStats(path=npath))
            stats.record(value)

        if isinstance(rec, dict):
            sig = shape_signature(rec)
        else:
            sig = f"non-dict-record:{_type_name(rec)}"
        sig_stats = signatures.setdefault(sig, ShapeSignature(signature=sig))
        sig_stats.count += 1
        if sig_stats.example_block_id is None:
            sig_stats.example_block_id = row["id"]

    return ShapeAuditResult(
        platform=platform,
        type_filter=type_filter,
        record_count=record_count,
        parse_errors=parse_errors,
        paths=paths,
        signatures=signatures,
    )


# ────────────────────────── markdown rendering ──────────────────────────


def render_shape_report(result: ShapeAuditResult) -> str:
    lines: list[str] = []
    title = f"# Shape audit — {result.platform}"
    if result.type_filter:
        title += f" (type={result.type_filter})"
    lines.append(title)
    lines.append("")
    lines.append(f"- Records enumerated: **{result.record_count}**")
    lines.append(f"- Records with unparseable/empty `raw_json`: **{result.parse_errors}**")
    lines.append(f"- Distinct key paths (list indices normalised): **{len(result.paths)}**")
    lines.append(f"- Distinct shape signatures: **{len(result.signatures)}**")
    lines.append("")

    lines.append("## Shape signatures (top-level keys + kind/type/role), by count")
    lines.append("")
    lines.append("| count | example block id | signature |")
    lines.append("|---:|---:|---|")
    for sig in sorted(result.signatures.values(), key=lambda s: -s.count):
        lines.append(f"| {sig.count} | {sig.example_block_id} | `{sig.signature}` |")
    lines.append("")

    lines.append("## Leaf key paths ranked by total character volume")
    lines.append("")
    lines.append("(This is the direct measure of how much real content is currently")
    lines.append("sitting behind this shape — the higher the volume, the more content")
    lines.append("is at stake in whatever classification decision governs this path.)")
    lines.append("")
    lines.append("| path | occurrences | types | total chars | min/median/max len | example |")
    lines.append("|---|---:|---|---:|---|---|")
    string_paths = [p for p in result.paths.values() if p.total_chars > 0]
    for p in sorted(string_paths, key=lambda p: -p.total_chars):
        types_str = ", ".join(f"{t}×{c}" for t, c in sorted(p.types.items(), key=lambda kv: -kv[1]))
        lm = f"{p.min_len}/{p.median_len:.0f}/{p.max_len}" if p.str_lengths else "-"
        ex = (p.example or "").replace("|", "\\|")
        lines.append(f"| `{p.path}` | {p.count} | {types_str} | {p.total_chars} | {lm} | {ex} |")
    lines.append("")

    non_string = [p for p in result.paths.values() if p.total_chars == 0]
    if non_string:
        lines.append("## Zero-volume leaf paths (non-string, or string but always empty)")
        lines.append("")
        lines.append("| path | occurrences | types |")
        lines.append("|---|---:|---|")
        for p in sorted(non_string, key=lambda p: -p.count):
            types_str = ", ".join(f"{t}×{c}" for t, c in sorted(p.types.items(), key=lambda kv: -kv[1]))
            lines.append(f"| `{p.path}` | {p.count} | {types_str} |")
        lines.append("")

    return "\n".join(lines)


# ────────────────────────── reasoning-locator sweep (§4.2b) ──────────────────────────


@dataclass
class ReasoningPathHit:
    platform: str
    path: str
    role: str
    block_type: str
    count: int
    total_chars: int


def _matches_reasoning_suffix(path: str) -> bool:
    # final segment after the last '.' or '[' boundary
    tail = path
    for sep in (".", "]"):
        if sep in tail:
            tail = tail.rsplit(sep, 1)[-1]
    tail = tail.lstrip(".").lower()
    return tail in REASONING_KEY_SUFFIXES


def reasoning_sweep(conn: sqlite3.Connection, platform: str) -> dict:
    """Spec §4.2b, run for one platform across ALL blocks regardless of
    current type. Returns a dict with:
      - known_vocab_hits: paths matching REASONING_KEY_SUFFIXES, cross-tabbed
        by (path, role, blocks.type) with counts/char volume.
      - unaccounted_string_paths: every string leaf path whose text is NOT a
        substring of the block's own `text` column, ranked by total char
        volume (candidates for anything dropped/misclassified, not just
        reasoning).
      - thinking_block_source: for blocks currently typed 'thinking', whether
        their raw_json contains a path matching the known vocabulary at all
        (possible over-classification if not).
    """
    query = """
        SELECT b.id, b.raw_json, b.type AS block_type, b.text AS block_text, m.role
        FROM v_blocks b
        JOIN v_conversations c ON c.id = b.conv_id
        JOIN messages m ON m.id = b.msg_id
        WHERE c.platform = ?
    """
    known_hits: dict[tuple[str, str, str], ReasoningPathHit] = {}
    unaccounted: dict[str, PathStats] = {}
    thinking_with_vocab = 0
    thinking_without_vocab = 0
    thinking_total = 0
    total_records = 0
    parse_errors = 0

    for row in conn.execute(query, [platform]):
        total_records += 1
        raw = row["raw_json"]
        if raw is None or raw == "":
            parse_errors += 1
            continue
        try:
            rec = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parse_errors += 1
            continue

        block_text = row["block_text"] or ""
        role = row["role"] or ""
        block_type = row["block_type"] or ""

        rec_has_vocab_hit = False
        for path, value in walk_paths(rec):
            if not isinstance(value, str) or not value:
                continue
            npath = _normalise_path(path)
            if _matches_reasoning_suffix(npath):
                rec_has_vocab_hit = True
                key = (npath, role, block_type)
                hit = known_hits.setdefault(
                    key, ReasoningPathHit(platform=platform, path=npath, role=role, block_type=block_type, count=0, total_chars=0)
                )
                hit.count += 1
                hit.total_chars += len(value)
            # step 2: does this string contribute to the block's rendered text?
            if value not in block_text:
                stats = unaccounted.setdefault(npath, PathStats(path=npath))
                stats.record(value)

        if block_type == "thinking":
            thinking_total += 1
            if rec_has_vocab_hit:
                thinking_with_vocab += 1
            else:
                thinking_without_vocab += 1

    return {
        "platform": platform,
        "records": total_records,
        "parse_errors": parse_errors,
        "known_hits": list(known_hits.values()),
        "unaccounted_paths": unaccounted,
        "thinking_total": thinking_total,
        "thinking_with_known_vocab": thinking_with_vocab,
        "thinking_without_known_vocab": thinking_without_vocab,
    }


def render_reasoning_report(sweeps: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Reasoning-locator sweep (spec §4.2b)")
    lines.append("")
    lines.append(
        "Run across every block of every platform regardless of current `blocks.type`. "
        "Two independent methods per platform: (1) known reasoning-vocabulary key-path "
        "matching, (2) every string leaf NOT already reflected in the block's rendered "
        "`text`, ranked by total character volume — this second method is what actually "
        "answers the question, the vocabulary list is only a seed."
    )
    lines.append("")

    lines.append("## Cross-tab: content matching known reasoning vocabulary, by current blocks.type")
    lines.append("")
    lines.append("Every row here where `block_type != thinking` is a defect candidate per §4.2b step 3 —")
    lines.append("reasoning-shaped content that is not classified as `thinking`.")
    lines.append("")
    lines.append("| platform | path | role | blocks.type | occurrences | total chars |")
    lines.append("|---|---|---|---|---:|---:|")
    any_known_hits = False
    for sweep in sweeps:
        for hit in sorted(sweep["known_hits"], key=lambda h: -h.total_chars):
            any_known_hits = True
            lines.append(
                f"| {hit.platform} | `{hit.path}` | {hit.role} | {hit.block_type} | {hit.count} | {hit.total_chars} |"
            )
    if not any_known_hits:
        lines.append("| *(none found)* | | | | | |")
    lines.append("")

    lines.append("## Over-classification check: blocks typed `thinking` without known-vocabulary support")
    lines.append("")
    lines.append("| platform | thinking blocks | with known-vocab path | without (possible over-classification) |")
    lines.append("|---|---:|---:|---:|")
    for sweep in sweeps:
        lines.append(
            f"| {sweep['platform']} | {sweep['thinking_total']} | "
            f"{sweep['thinking_with_known_vocab']} | {sweep['thinking_without_known_vocab']} |"
        )
    lines.append("")

    lines.append("## Per-platform: top unaccounted string paths by character volume")
    lines.append("")
    lines.append(
        "String-valued leaf paths whose content does not currently appear anywhere in the "
        "block's rendered `text`. High volume here is a candidate for dropped/misclassified "
        "content of any kind, not only reasoning — this satisfies §4.2b step 2's 'read it "
        "entirely' requirement independent of the vocabulary list above."
    )
    lines.append("")
    for sweep in sweeps:
        lines.append(f"### {sweep['platform']}")
        lines.append("")
        lines.append(f"- Records enumerated: {sweep['records']} (parse errors: {sweep['parse_errors']})")
        lines.append("")
        top = sorted(sweep["unaccounted_paths"].values(), key=lambda p: -p.total_chars)[:25]
        if not top:
            lines.append("*(no unaccounted string content — every string leaf already appears in some block's text)*")
            lines.append("")
            continue
        lines.append("| path | occurrences | total chars | min/median/max len | example |")
        lines.append("|---|---:|---:|---|---|")
        for p in top:
            lm = f"{p.min_len}/{p.median_len:.0f}/{p.max_len}" if p.str_lengths else "-"
            ex = (p.example or "").replace("|", "\\|")
            lines.append(f"| `{p.path}` | {p.count} | {p.total_chars} | {lm} | {ex} |")
        lines.append("")

    return "\n".join(lines)


def write_report(text: str, out: Path | None) -> None:
    if out is None:
        print(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

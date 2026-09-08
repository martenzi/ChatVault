from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

from chatvault import export as export_mod
from chatvault.brain.core import BrainError, known_kinds, now, resolve_item

# `brain distill` (docs/40_SPEC_second_brain_v0.md §3c, ported from the
# never-built `stash extract`; docs/46_AUDIT_second_brain_landscape.md §4d).
#
# Deliberately its own command, not a `brain promote` flag — the user was
# explicit that these are different *intentions*, not variations of one
# operation: `promote` copies a conversation into the Brain verbatim (a
# transfer); `distill` produces the user's own distilled knowledge FROM a
# conversation, LLM-assisted under the hood but a separate deliberate act,
# not a copy.

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "mistral:latest"
_TIMEOUT_S = 300
NOTHING_DURABLE_MARKER = "NOTHING_DURABLE"

# The spec's own extraction prompt (40_SPEC §3c), verbatim, plus one
# machine-parseable instruction appended so "say so, don't manufacture
# content" (the honesty constraint 46_AUDIT §6 calls out by name) has an
# unambiguous signal this code can act on, rather than trying to pattern-
# match free text for something like "no durable facts were found."
def _build_prompt(source_text: str, label: str = "CONVERSATION") -> str:
    return f"""From this {label.lower()}, extract only durable factual content:

- Facts established (with the reasoning if it isn't obvious)
- Decisions made, and what was rejected
- Methods that worked — enough detail to repeat them
- Things that turned out to be false or dead ends

Exclude entirely: pleasantries, restatements, the model's process narration,
anything superseded later in the same {label.lower()}, and anything that is
only true about that moment.

Write as standalone notes. A reader must not need the original {label.lower()}
to understand them. If a claim depends on context, state the context.
If nothing durable is present, say so — do not manufacture content.

Machine-readable instruction: if nothing durable is present, respond with
EXACTLY the single line `{NOTHING_DURABLE_MARKER}` and nothing else.
Otherwise, respond with the standalone notes as markdown ONLY — no preamble,
no meta-commentary about this extraction process, no closing remarks.

--- {label} ---
{source_text}
"""


class DistillUnavailable(RuntimeError):
    """Ollama isn't reachable, or the model isn't pulled."""


def ollama_generate(prompt: str, model: str = DEFAULT_MODEL, base_url: str = OLLAMA_GENERATE_URL) -> str:
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
    req = urllib.request.Request(base_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise DistillUnavailable(
            f"couldn't reach Ollama at {base_url} ({exc}) — "
            f"is `ollama serve` running, and has `{model}` been pulled (`ollama pull {model}`)?"
        ) from exc
    response = data.get("response")
    if response is None:
        raise DistillUnavailable(f"Ollama returned no response for model {model!r}: {data}")
    return response.strip()


def _source_meta(conv_row: sqlite3.Row) -> str:
    return json.dumps(
        {
            "platform": conv_row["platform"],
            "project": conv_row["project"],
            "started_at": conv_row["started_at"],
            "model_primary": conv_row["model_primary"],
            "message_count": conv_row["message_count"],
        }
    )


def resolve_conversation(vault_conn_ro: sqlite3.Connection, uid: str) -> sqlite3.Row:
    row = vault_conn_ro.execute("SELECT * FROM v_conversations WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        raise BrainError(f"no conversation with uid {uid!r} in the vault")
    return row


def distill(
    brain_conn: sqlite3.Connection,
    vault_conn_ro: sqlite3.Connection,
    uid: str,
    why: str,
    model: str = DEFAULT_MODEL,
    kind: str = "note",
    generate=ollama_generate,
) -> dict:
    """Feeds the `lean` export of the conversation identified by `uid` to a
    local model with the strict extraction prompt, and — unless the model
    says nothing durable was present — stores the result as a new Brain
    item. `uid` is internal-only provenance (stored on the item, never
    something a user types to invoke this — see resolve_conversation_ref in
    brain_cli.py for the number/title addressing the user actually types).

    Every call creates a fresh item, deliberately not deduplicated against a
    prior distill of the same conversation: distilling is a creative,
    model-assisted act the user may deliberately repeat (a different model,
    a second pass after noticing something was missed), unlike `promote`
    which is a mechanical, idempotent copy."""
    if why is None or not why.strip():
        raise BrainError("--why is required and must not be empty")
    if kind not in known_kinds(brain_conn):
        raise BrainError(f"kind must be one of {sorted(known_kinds(brain_conn))}, got {kind!r}")

    row = resolve_conversation(vault_conn_ro, uid)
    conversation_text = export_mod.render_profile(vault_conn_ro, row, profile="lean")
    prompt = _build_prompt(conversation_text)

    response = generate(prompt, model=model)

    if response.strip() == NOTHING_DURABLE_MARKER:
        return {"action": "nothing_durable", "title": row["title"] or uid}

    ts = now()
    cur = brain_conn.execute(
        """
        INSERT INTO items (kind, title, why, body, source_kind, source_uid, source_meta, status, created_at, origin_detail)
        VALUES (?, ?, ?, ?, 'chatvault_distilled', ?, ?, 'active', ?, ?)
        """,
        (kind, f"Distilled — {row['title'] or uid}", why.strip(), response, uid, _source_meta(row), ts, f"summarise:{model}"),
    )
    brain_conn.commit()
    return {"item_id": cur.lastrowid, "action": "distilled", "title": row["title"] or uid}


def distill_entry(
    brain_conn: sqlite3.Connection,
    ref,
    why: str,
    model: str = DEFAULT_MODEL,
    kind: str = "note",
    generate=ollama_generate,
) -> dict:
    """`brain summarise entry <ref>` (docs/51_SPEC_brain_parity_and_naming.md
    §1.3/§8 Q4) — the obvious next request once `summarise conversation`
    exists: the same AI-assisted extraction, run over an *existing Brain
    entry* (its why + body) instead of a vault conversation. Same panel,
    same `--model`, same disclosure, same 'say so, don't manufacture
    content' honesty constraint. Every call creates a fresh item — not
    deduplicated, for the same reason `distill` isn't (a deliberate,
    repeatable creative act, unlike `copy conversation`'s mechanical,
    idempotent copy)."""
    if why is None or not why.strip():
        raise BrainError("--why is required and must not be empty")
    if kind not in known_kinds(brain_conn):
        raise BrainError(f"kind must be one of {sorted(known_kinds(brain_conn))}, got {kind!r}")

    item = resolve_item(brain_conn, ref)
    source_text = "\n\n".join(part for part in (item["why"], item["body"]) if part) or item["title"]
    prompt = _build_prompt(source_text, label="ENTRY")

    response = generate(prompt, model=model)

    if response.strip() == NOTHING_DURABLE_MARKER:
        return {"action": "nothing_durable", "title": item["title"]}

    ts = now()
    cur = brain_conn.execute(
        """
        INSERT INTO items (kind, title, why, body, source_kind, source_uid, status, created_at, origin_detail)
        VALUES (?, ?, ?, ?, 'brain_distilled', ?, 'active', ?, ?)
        """,
        (kind, f"Summary — {item['title']}", why.strip(), response, str(item["id"]), ts, f"summarise:{model}"),
    )
    brain_conn.commit()
    return {"item_id": cur.lastrowid, "action": "distilled", "title": item["title"]}

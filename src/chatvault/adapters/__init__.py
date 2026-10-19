from __future__ import annotations

from pathlib import Path
from types import ModuleType

from chatvault.adapters import claude_code, claude_desktop

_REGISTRY: dict[str, ModuleType] = {
    "claude_code": claude_code,
    "claude_desktop": claude_desktop,
}


def register_late_adapters() -> None:
    """Import adapters that depend on modules defined later in the build."""
    from chatvault.adapters import (
        antigravity,
        chatgpt,
        chatgpt_legacy_recovered,
        cursor,
        cursor_legacy,
        cursor_legacy_copilot,
        deepseek,
        devin_cli,
        devin_desktop_acp,
        gemini_takeout,
        gemini_workspace,
        generic_jsonl,
        google_ai_mode,
        grok,
        hermes,
        notebooklm,
        openclaw,
        vscode_legacy,
        windsurf_legacy,
    )

    # chatgpt (official mapping/current_node export) before
    # chatgpt_legacy_recovered (DOM-scraped, no "mapping" key). chatgpt.sniff
    # explicitly rejects files containing "fragments" (DeepSeek's shape) so
    # it stays exclusive regardless of registry order — a real DeepSeek
    # export was found to satisfy chatgpt's old "mapping"-only sniff too
    # (both are node-map trees), which silently mislabeled every DeepSeek
    # file as chatgpt until that check was tightened. Don't assume any two
    # adapters' sniffs are disjoint from a code comment alone — the registry
    # order here was never actually validated against real files until this
    # bug was found; see tests/test_adapter_registry.py for the sweep that
    # now checks every pair against the real corpus and fixtures.
    _REGISTRY.setdefault("chatgpt", chatgpt)
    _REGISTRY.setdefault("chatgpt_legacy_recovered", chatgpt_legacy_recovered)
    _REGISTRY.setdefault("windsurf_legacy", windsurf_legacy)
    _REGISTRY.setdefault("cursor_legacy", cursor_legacy)
    _REGISTRY.setdefault("vscode_legacy", vscode_legacy)
    _REGISTRY.setdefault("cursor_legacy_copilot", cursor_legacy_copilot)
    _REGISTRY.setdefault("openclaw", openclaw)
    _REGISTRY.setdefault("hermes", hermes)
    # deepseek: "mapping" + "fragments" together. chatgpt.sniff now excludes
    # "fragments" explicitly, so this pair is safe in either registry order.
    _REGISTRY.setdefault("deepseek", deepseek)
    # grok: "leaf_response_id" + "parent_response_id" — verified empirically
    # against the full real raw/ corpus (see test_adapter_registry.py) to be
    # the only adapter matching Grok's export.
    _REGISTRY.setdefault("grok", grok)
    # gemini_takeout: "Gemini Apps" + "activityControls" + "safeHtmlItem"
    # together — a Google Takeout "My Activity" export, verified empirically
    # to be the only adapter matching this shape.
    _REGISTRY.setdefault("gemini_takeout", gemini_takeout)
    # gemini_workspace: "conversation_turns" + "user_turn"/"system_turn" in a
    # .txt file — Takeout's "Gemini in Workspace/Conversation History" export.
    _REGISTRY.setdefault("gemini_workspace", gemini_workspace)
    # devin_cli / devin_desktop_acp: both sniff on a unique top-level
    # "schema" tag their own extraction script stamps
    # ("devin_cli_session_v1" / "devin_desktop_acp_conversation_v1") on the
    # first line only -- disjoint from each other and from every other
    # adapter's sniff by construction, verified by
    # tests/test_adapter_registry.py's sweep.
    _REGISTRY.setdefault("devin_cli", devin_cli)
    _REGISTRY.setdefault("devin_desktop_acp", devin_desktop_acp)
    # cursor (current version, state.vscdb + conversation-search.db):
    # sniffs on its own "cursor_conversation_v1" schema tag, same disjoint-
    # by-construction pattern as the two devin adapters above. Distinct
    # platform name from cursor_legacy/cursor_legacy_copilot (different
    # export era entirely, see docs/reports/cursor_shapes_2026-09-09.md).
    _REGISTRY.setdefault("cursor", cursor)
    # antigravity (modern .db format, IDE + agy CLI): sniffs on its own
    # "antigravity_conversation_v1" schema tag, same disjoint-by-construction
    # pattern as the adapters above. See
    # docs/reports/antigravity_shapes_2026-09-09.md -- no .proto schema ships
    # with the app, so raw is protobuf bytes the extraction script never
    # interprets; all decoding happens here at parse time via targeted
    # (non-recursive) field-path reads.
    _REGISTRY.setdefault("antigravity", antigravity)
    # google_ai_mode: Takeout "My Activity/AI Mode" (header "AI Mode"), a
    # separate platform from gemini by user decision. Sniffs on the AI Mode
    # header cell; gemini_takeout needs "Gemini Apps" — verified disjoint
    # against all 8 real AI Mode files.
    _REGISTRY.setdefault("google_ai_mode", google_ai_mode)
    # notebooklm: Takeout NotebookLM "Chat History/Chat Session - <uuid>.html",
    # a plain-text USER:/MODEL: transcript; sniffs on file name, parent
    # directory and the leading role tag. Chat sessions only.
    _REGISTRY.setdefault("notebooklm", notebooklm)
    # Deliberately last: permissive fallback, must not shadow specific adapters.
    _REGISTRY.setdefault("generic_jsonl", generic_jsonl)


def get(adapter_name: str) -> ModuleType:
    if adapter_name not in _REGISTRY:
        register_late_adapters()
    return _REGISTRY[adapter_name]


def detect(path: Path) -> ModuleType | None:
    register_late_adapters()
    # Try every specific adapter before the generic fallback, which is
    # registered last and must stay last in this ordering too.
    ordered = [n for n in _REGISTRY if n != "generic_jsonl"] + (
        ["generic_jsonl"] if "generic_jsonl" in _REGISTRY else []
    )
    for adapter_name in ordered:
        mod = _REGISTRY[adapter_name]
        try:
            if mod.sniff(path):
                return mod
        except Exception:
            continue
    return None


def all_names() -> list[str]:
    register_late_adapters()
    return list(_REGISTRY.keys())


def detect_projects(path: Path) -> ModuleType | None:
    """A small number of adapters also expose sniff_projects/parse_projects
    for a structurally distinct file (e.g. Claude's separate projects.json,
    unrelated in shape to its conversations export). Checked separately from
    detect() so a plain conversations-adapter never has to know about it."""
    register_late_adapters()
    for mod in _REGISTRY.values():
        sniff_fn = getattr(mod, "sniff_projects", None)
        parse_fn = getattr(mod, "parse_projects", None)
        if sniff_fn is None or parse_fn is None:
            continue
        try:
            if sniff_fn(path):
                return mod
        except Exception:
            continue
    return None


def detect_artifacts(path: Path) -> ModuleType | None:
    """Like detect_projects, for `artifacts/<id>/artifact.json` (Claude's
    official export). Kept out of detect() so no conversation adapter ever
    claims an artifact's files."""
    register_late_adapters()
    for mod in _REGISTRY.values():
        sniff_fn = getattr(mod, "sniff_artifacts", None)
        parse_fn = getattr(mod, "parse_artifacts", None)
        if sniff_fn is None or parse_fn is None:
            continue
        try:
            if sniff_fn(path):
                return mod
        except Exception:
            continue
    return None

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
        chatgpt,
        chatgpt_legacy_recovered,
        cursor_legacy,
        cursor_legacy_copilot,
        deepseek,
        gemini_takeout,
        generic_jsonl,
        grok,
        hermes,
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

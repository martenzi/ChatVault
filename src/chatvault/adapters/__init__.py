from __future__ import annotations

from pathlib import Path
from types import ModuleType

from chatvault.adapters import claude_code, claude_web

_REGISTRY: dict[str, ModuleType] = {
    "claude_code": claude_code,
    "claude_web": claude_web,
}


def register_late_adapters() -> None:
    """Import adapters that depend on modules defined later in the build."""
    from chatvault.adapters import (
        chatgpt,
        chatgpt_legacy_recovered,
        cursor_legacy,
        cursor_legacy_copilot,
        generic_jsonl,
        hermes,
        openclaw,
        vscode_legacy,
        windsurf_legacy,
    )

    # chatgpt (official mapping/current_node export) before
    # chatgpt_legacy_recovered (DOM-scraped, no "mapping" key) — order
    # matters only if a file could satisfy both sniffs, which it can't
    # (disjoint required keys), but keep the more authoritative source first.
    _REGISTRY.setdefault("chatgpt", chatgpt)
    _REGISTRY.setdefault("chatgpt_legacy_recovered", chatgpt_legacy_recovered)
    _REGISTRY.setdefault("windsurf_legacy", windsurf_legacy)
    _REGISTRY.setdefault("cursor_legacy", cursor_legacy)
    _REGISTRY.setdefault("vscode_legacy", vscode_legacy)
    _REGISTRY.setdefault("cursor_legacy_copilot", cursor_legacy_copilot)
    _REGISTRY.setdefault("openclaw", openclaw)
    _REGISTRY.setdefault("hermes", hermes)
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

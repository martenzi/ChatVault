from __future__ import annotations

from pathlib import Path
from types import ModuleType

from chatvault.adapters import claude_code, claude_web

_REGISTRY: dict[str, ModuleType] = {
    "claude_code": claude_code,
    "claude_web": claude_web,
}


def register_late_adapters() -> None:
    """Import adapters that depend on modules defined later in the build (chatgpt, generic_jsonl)."""
    from chatvault.adapters import chatgpt, generic_jsonl

    _REGISTRY.setdefault("chatgpt", chatgpt)
    _REGISTRY.setdefault("generic_jsonl", generic_jsonl)


def get(adapter_name: str) -> ModuleType:
    if adapter_name not in _REGISTRY:
        register_late_adapters()
    return _REGISTRY[adapter_name]


def detect(path: Path) -> ModuleType | None:
    register_late_adapters()
    # Try specific adapters before the generic fallback.
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

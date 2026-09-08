from __future__ import annotations

# Short, human-typed aliases for canonical `platform` column values. "claude"
# deliberately expands to both — the user's own call: broadest net, matches
# how people actually say it, rather than picking one arbitrarily.
ALIASES: dict[str, tuple[str, ...]] = {
    "gpt": ("chatgpt",),
    "claude": ("claude_desktop", "claude_code"),
    "ds": ("deepseek",),
    "gem": ("gemini",),
    "wind": ("windsurf",),
}


def resolve_platform_alias(name: str) -> tuple[str, ...]:
    """Expand a platform alias to its canonical name(s); pass through
    anything not in the table unchanged, so a typo just yields zero matches
    downstream, same as today."""
    return ALIASES.get(name, (name,))

from __future__ import annotations

import os

import pytest

from chatvault import adapters
from tests.conftest import FIXTURES


def test_every_fixture_matches_exactly_one_conversation_adapter():
    """Regression test for the bug where a real DeepSeek export (both
    "mapping" and "fragments") also satisfied chatgpt.sniff's old
    "mapping"-only check, and chatgpt won because it's earlier in
    adapters._REGISTRY. A comment claiming two sniffs are "disjoint" is not
    verification — this walks every adapter fixture and confirms only its
    own adapter (or, for legacy/generic fallbacks that are deliberately
    permissive, an allowed set) claims it."""
    adapters.register_late_adapters()
    names = [n for n in adapters._REGISTRY if n != "generic_jsonl"]

    for fixture_dir in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        adapter_name = fixture_dir.name
        if adapter_name not in adapters._REGISTRY:
            continue
        for file_path in fixture_dir.rglob("*"):
            if not file_path.is_file():
                continue
            matched = []
            for name in names:
                mod = adapters._REGISTRY[name]
                try:
                    if mod.sniff(file_path):
                        matched.append(name)
                except Exception:
                    continue
            # <= 1 rather than == 1: some fixtures (e.g. claude_web's
            # projects.json) are routed entirely through sniff_projects()
            # instead of sniff(), and legitimately match no conversation
            # adapter at all. What must never happen is *two* adapters
            # both claiming a conversation-shaped file.
            assert len(matched) <= 1 and (not matched or matched[0] == adapter_name), (
                f"{file_path} matched {matched}, expected at most [{adapter_name!r}] — "
                "two adapters' sniffs overlap on this file"
            )


def test_deepseek_export_is_not_misdetected_as_chatgpt():
    path = FIXTURES / "deepseek" / "conversations.json"
    mod = adapters.detect(path)
    assert mod is not None
    assert mod.name == "deepseek"


def test_registry_sweep_against_real_raw_corpus_if_present():
    """Same sweep as above, but against the user's real raw/ corpus when one
    is present on disk — this is the actual methodology that caught the
    chatgpt/deepseek overlap (fixtures alone wouldn't have; the bug only
    showed up against a real 320-conversation DeepSeek export). Skipped in
    environments with no raw/ directory (e.g. CI checkouts) since raw/ is
    gitignored user data, not a fixture."""
    raw_dir = FIXTURES.parent.parent / "raw"
    if not raw_dir.is_dir():
        pytest.skip("no raw/ corpus present in this environment")

    adapters.register_late_adapters()
    names = [n for n in adapters._REGISTRY if n != "generic_jsonl"]

    overlaps = []
    for root, _dirs, files in os.walk(raw_dir):
        for f in files:
            if not (f.endswith(".json") or f.endswith(".jsonl")):
                continue
            from pathlib import Path

            file_path = Path(root, f)
            matched = []
            for name in names:
                mod = adapters._REGISTRY[name]
                try:
                    if mod.sniff(file_path):
                        matched.append(name)
                except Exception:
                    continue
            if len(matched) > 1:
                overlaps.append((str(file_path), matched))

    assert overlaps == [], f"multiple adapters matched the same real file: {overlaps}"

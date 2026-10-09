from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import orjson

KNOWN_EXTENSIONS = {".json", ".jsonl", ".vscdb"}


@dataclass
class FileReport:
    path: Path
    platform: str
    adapter: str
    bytes: int
    conversation_count: int | None = None
    sample_title: str | None = None
    note: str = ""


@dataclass
class InventoryReport:
    files: list[FileReport] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)

    def totals_by_platform(self) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = {}
        for f in self.files:
            bucket = totals.setdefault(f.platform, {"files": 0, "bytes": 0, "conversations": 0})
            bucket["files"] += 1
            bucket["bytes"] += f.bytes
            bucket["conversations"] += f.conversation_count or 0
        return totals


def _peek_jsonl_first_line(path: Path) -> dict | None:
    try:
        with path.open("rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                return orjson.loads(line)
    except (orjson.JSONDecodeError, OSError):
        return None
    return None


# Adapters whose JSONL shape is "line 0 is a meta record, everything else is
# one event/message" — always exactly one conversation per file. Counting
# sessionId (absent) or raw lines for these would count messages instead,
# wildly inflating the total. Keyed by the `extraction` fingerprint each
# exporter stamps into its own meta line (tools/corpus_full/export_*.py,
# tools/cascade_decrypt/cascade_export_full.py).
_EXTRACTION_TO_PLATFORM_ADAPTER = {
    "protobuf-wire-full-v1": ("windsurf", "windsurf_legacy"),
    "protobuf-wire-v1": ("windsurf", "windsurf_legacy"),  # thin export; do not ingest, see CORPUS_FULL_SOT
    "cursor-composer-full-v1": ("cursor", "cursor_legacy"),
    "vscode-chatsessions-full-v1": ("vscode", "vscode_legacy"),
    "cursor-interactive-sessions-full-v1": ("cursor_copilot", "cursor_legacy_copilot"),
    "cursor-prompts-full-v1": ("cursor_copilot", "cursor_legacy_copilot"),
}
_ONE_CONVERSATION_PER_FILE_ADAPTERS = {
    "windsurf_legacy",
    "cursor_legacy",
    "vscode_legacy",
    "cursor_legacy_copilot",
}


def _detect_jsonl(path: Path, deep: bool) -> FileReport:
    first = _peek_jsonl_first_line(path)
    platform, adapter = "unknown", "generic_jsonl"
    if first is not None:
        keys = set(first.keys())
        extraction_match = (
            _EXTRACTION_TO_PLATFORM_ADAPTER.get(first.get("extraction"))
            if first.get("type") == "meta"
            else None
        )
        # Windsurf's older thin export predates the "extraction" field having
        # this exact string on some files; keep the structural fallback.
        legacy_windsurf_fallback = (
            first.get("type") == "meta"
            and (
                first.get("source") == "windsurf_legacy_cascade"
                or ("conversation_id" in keys and "cascade_file" in keys)
            )
        )
        if extraction_match:
            platform, adapter = extraction_match
        elif legacy_windsurf_fallback:
            platform, adapter = "windsurf", "windsurf_legacy"
        # Claude Code session lines always co-occur with sessionId, uuid, cwd
        # and version (verified against real ~/.claude/**/*.jsonl files).
        # NOTE: this only peeks the first line, so a session file that opens
        # with a {"type": "summary", ...} line (no sessionId/cwd/version)
        # will fall through to generic_jsonl here even though the deep
        # adapter-level sniff() in adapters/claude_code.py (which scans
        # several lines) would still correctly claim it during actual ingest.
        elif {"sessionId", "uuid", "cwd", "version"}.issubset(keys):
            platform, adapter = "claude_code", "claude_code"
        # Finally check for generic JSONL
        elif {"role", "content"} & keys or {"author", "content"} <= keys:
            platform, adapter = "generic", "generic_jsonl"

    conv_count = None
    if deep:
        if adapter in _ONE_CONVERSATION_PER_FILE_ADAPTERS:
            conv_count = 1
        else:
            sessions: set[str] = set()
            lines = 0
            try:
                with path.open("rb") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        lines += 1
                        try:
                            obj = orjson.loads(line)
                        except orjson.JSONDecodeError:
                            continue
                        sid = obj.get("sessionId") if isinstance(obj, dict) else None
                        if sid:
                            sessions.add(sid)
            except OSError:
                pass
            conv_count = len(sessions) if sessions else lines

    return FileReport(
        path=path,
        platform=platform,
        adapter=adapter,
        bytes=path.stat().st_size,
        conversation_count=conv_count,
    )


def _detect_json(path: Path, deep: bool) -> FileReport:
    platform, adapter = "unknown", "unknown"
    conv_count = None
    sample_title = None
    note = ""

    if not deep:
        # Cheap peek: read a small prefix and look for tell-tale keys, without
        # parsing the whole (possibly huge) file.
        try:
            with path.open("rb") as fh:
                prefix = fh.read(65536)
        except OSError:
            prefix = b""
        if b'"mapping"' in prefix:
            platform, adapter = "chatgpt", "chatgpt"
        elif b'"chat_messages"' in prefix:
            platform, adapter = "claude_desktop", "claude_desktop"
        elif b'"accountUserId"' in prefix and b'"isArchived"' in prefix:
            platform, adapter = "chatgpt", "chatgpt_legacy_recovered"
        return FileReport(path=path, platform=platform, adapter=adapter, bytes=path.stat().st_size, note=note)

    try:
        data = orjson.loads(path.read_bytes())
    except (orjson.JSONDecodeError, OSError) as exc:
        return FileReport(
            path=path, platform="unknown", adapter="unknown", bytes=path.stat().st_size, note=f"parse error: {exc}"
        )

    items = data if isinstance(data, list) else [data]
    conv_count = len(items)
    if items:
        first = items[0] if isinstance(items[0], dict) else {}
        if "mapping" in first:
            platform, adapter = "chatgpt", "chatgpt"
            sample_title = first.get("title")
        elif "chat_messages" in first:
            platform, adapter = "claude_desktop", "claude_desktop"
            sample_title = first.get("name") or first.get("title")
        elif "accountUserId" in first and "isArchived" in first:
            platform, adapter = "chatgpt", "chatgpt_legacy_recovered"
            sample_title = first.get("title")
            # This shape has no per-conversation timestamp/model to sanity
            # check; conv_count here is just len(items), which is correct
            # for the one-file-per-list case but a single-conversation dict
            # file already reports 1 via `items = [data]` above.

    return FileReport(
        path=path,
        platform=platform,
        adapter=adapter,
        bytes=path.stat().st_size,
        conversation_count=conv_count,
        sample_title=sample_title,
        note=note,
    )


def _detect_vscdb(path: Path) -> FileReport:
    return FileReport(
        path=path,
        platform="vscode_state",
        adapter="vscode_state",
        bytes=path.stat().st_size,
        note="Phase 3 adapter — detection only, not yet ingestible",
    )


def scan(directory: Path, deep: bool = False) -> InventoryReport:
    """Read-only scan of a directory tree. Never writes anything."""
    from chatvault.ingest import _under_ingested_archive

    report = InventoryReport()
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if _under_ingested_archive(path, directory):
            continue
        suffix = path.suffix.lower()
        if suffix not in KNOWN_EXTENSIONS:
            report.skipped.append(path)
            continue
        if suffix == ".jsonl":
            report.files.append(_detect_jsonl(path, deep))
        elif suffix == ".json":
            report.files.append(_detect_json(path, deep))
        elif suffix == ".vscdb":
            report.files.append(_detect_vscdb(path))
    return report

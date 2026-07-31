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


def _detect_jsonl(path: Path, deep: bool) -> FileReport:
    first = _peek_jsonl_first_line(path)
    platform, adapter = "unknown", "generic_jsonl"
    if first is not None:
        keys = set(first.keys())
        # Windsurf/Cascade legacy export: meta line self-declares its origin
        # via "source": "windsurf_legacy_cascade" (see EXTRACTION_METHOD.md).
        # Structural fallback (type=="meta" + conversation_id + cascade_file)
        # covers exports where that string is missing or renamed.
        if first.get("type") == "meta" and (
            first.get("source") == "windsurf_legacy_cascade"
            or ("conversation_id" in keys and "cascade_file" in keys)
        ):
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
        if adapter == "windsurf_legacy":
            # One meta line -> exactly one conversation per file; counting
            # sessionId (absent) or raw lines here would count messages
            # instead, wildly inflating the total.
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
            platform, adapter = "claude_web", "claude_web"
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
            platform, adapter = "claude_web", "claude_web"
            sample_title = first.get("name") or first.get("title")

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
    report = InventoryReport()
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
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

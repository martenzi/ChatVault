from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

import yaml

from chatvault.brain.core import BrainError, sha256_text
from chatvault.brain.entries import add_entry

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, m.group(2)


def _extract_title(body: str, fallback: str) -> tuple[str, str]:
    m = _H1_RE.search(body)
    if not m:
        return fallback, body
    title = m.group(1).strip()
    rest = (body[: m.start()] + body[m.end():]).strip()
    return title, rest


def _path_tags(root: Path, file_path: Path) -> list[str]:
    """The folder path becomes tags (§3, BRAIN_FAQ.md §10) — each level of
    nesting is its own hierarchical tag segment, same slash convention as
    every other tag in the Brain."""
    rel_parts = file_path.relative_to(root).parent.parts
    tags = []
    acc = []
    for part in rel_parts:
        acc.append(part)
        tags.append("/".join(acc))
    return tags


def import_markdown(conn: sqlite3.Connection, directory: Path, kind: str = "note", extra_tag: str | None = None) -> list[dict]:
    """This is how the hoard gets in (BRAIN_FAQ.md §10, §3): every .md file
    under `directory` becomes one item. Front-matter -> meta, the H1 ->
    title (falling back to the filename), the folder path -> tags. `why`
    has no source in a bulk markdown import, so it's generated from the
    relative path — good enough to locate the file later, and editable
    afterward with `brain edit entry`."""
    directory = Path(directory)
    if not directory.is_dir():
        raise BrainError(f"{directory} is not a directory")

    results = []
    for file_path in sorted(directory.rglob("*.md")):
        text = file_path.read_text(encoding="utf-8", errors="replace")
        meta, body = _split_frontmatter(text)
        title, body = _extract_title(body, fallback=file_path.stem)
        rel = file_path.relative_to(directory)
        tags = _path_tags(directory, file_path)
        if extra_tag:
            tags.append(extra_tag)

        import json as _json

        result = add_entry(
            conn,
            title=title,
            why=f"imported from {rel}",
            kind=meta.pop("kind", kind),
            body=body,
            tags=tags,
            meta_json=_json.dumps(meta) if meta else None,
            source_path=str(file_path),
        )
        item_id = result["item_id"]
        conn.execute("UPDATE items SET body_sha256 = ? WHERE id = ?", (sha256_text(body), item_id))
        conn.commit()
        results.append({**result, "path": str(rel)})
    return results


def _load_mapping(map_path: Path) -> dict:
    mapping = yaml.safe_load(Path(map_path).read_text())
    if not isinstance(mapping, dict) or "fields" not in mapping:
        raise BrainError("--map file must have a top-level 'fields' mapping (item field -> source key)")
    return mapping


def _row_to_entry_kwargs(row: dict, mapping: dict) -> dict:
    fields = mapping.get("fields", {})
    defaults = mapping.get("defaults", {})
    kwargs = dict(defaults)
    for item_field, source_key in fields.items():
        if source_key in row and row[source_key] not in (None, ""):
            kwargs[item_field] = row[source_key]
    tag_field = mapping.get("tag_field")
    tag_delimiter = mapping.get("tag_delimiter", ",")
    if tag_field and row.get(tag_field):
        kwargs["tags"] = [t.strip() for t in str(row[tag_field]).split(tag_delimiter) if t.strip()]
    return kwargs


def import_yaml(conn: sqlite3.Connection, file_path: Path, map_path: Path) -> list[dict]:
    mapping = _load_mapping(map_path)
    data = yaml.safe_load(Path(file_path).read_text())
    if isinstance(data, dict):
        rows = list(data.values())
    elif isinstance(data, list):
        rows = data
    else:
        raise BrainError(f"{file_path} must contain a YAML list or mapping of records")

    results = []
    for row in rows:
        kwargs = _row_to_entry_kwargs(row, mapping)
        title = kwargs.pop("title", None)
        why = kwargs.pop("why", None)
        if title is None or why is None:
            raise BrainError(f"row missing required title/why after mapping: {row!r}")
        results.append(add_entry(conn, title=title, why=why, **kwargs))
    return results


def import_csv(conn: sqlite3.Connection, file_path: Path, map_path: Path) -> list[dict]:
    mapping = _load_mapping(map_path)
    with open(file_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    results = []
    for row in rows:
        kwargs = _row_to_entry_kwargs(row, mapping)
        title = kwargs.pop("title", None)
        why = kwargs.pop("why", None)
        if title is None or why is None:
            raise BrainError(f"row missing required title/why after mapping: {row!r}")
        results.append(add_entry(conn, title=title, why=why, **kwargs))
    return results

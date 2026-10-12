from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters.base import make_attachment, make_block, make_conversation, make_message
from chatvault.adapters.identifiers import decode_utf8_counting_replacements

platform = "claude_desktop"
name = "claude_desktop"

_SENDER_TO_ROLE = {"human": "user", "assistant": "assistant", "system": "system"}


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    return b'"chat_messages"' in prefix


def sniff_projects(path: Path) -> bool:
    """Claude's separate `projects.json` export — a list of {name, docs, ...},
    structurally unrelated to the conversations file. Routed through a
    different ingest path (chatvault.loader.load_project), not the
    conversation pipeline sniff() above feeds."""
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    return b'"prompt_template"' in prefix and b'"docs"' in prefix and b'"chat_messages"' not in prefix


def _display_text(d) -> str:
    """Text of a tool_use/tool_result `display_content` dict. Shapes enumerated
    from the 2026-10-12 export (143 results + 137 uses, all dicts): text 56,
    json_block 175, table 9, code_block 20, rich_link 11, rich_content 6,
    file 3. Prose-bearing shapes yield their text; the rest are JSON-dumped so
    nothing is invisible to FTS."""
    if not isinstance(d, dict):
        return "" if d in (None, "") else json.dumps(d, default=str)
    kind = d.get("type")
    if kind == "text" and isinstance(d.get("text"), str):
        return d["text"]
    if kind == "json_block" and isinstance(d.get("json_block"), str):
        return d["json_block"]
    if kind == "code_block" and isinstance(d.get("code"), str):
        return d["code"]
    return json.dumps(d, default=str)


def _structured_text(sc) -> str:
    """Text of a tool_result `structured_content` dict (114 of 693). 108 carry
    only {content}, which is unwrapped; the rest (memory ops, artifact refs)
    are JSON-dumped."""
    if sc in (None, ""):
        return ""
    if isinstance(sc, dict) and set(sc) == {"content"} and isinstance(sc["content"], str):
        return sc["content"]
    return json.dumps(sc, default=str)


def _with_sections(base: str, item: dict, keys: tuple[str, ...]) -> str:
    """Append alternate renderings of one tool event to `base`, each under a
    strippable `--- key ---` delimiter. A section is skipped when empty or when
    its text already appears in `base` (no doubling)."""
    out = base
    for key in keys:
        val = item.get(key)
        text = _display_text(val) if key == "display_content" else _structured_text(val)
        if text and text not in out:
            out += f"\n\n--- {key} ---\n{text}"
    return out


def _tool_name(item: dict) -> str | None:
    """Prefix the integration so MCP/built-in tools are distinguishable. MCP
    names are already namespaced in `name` ("filesystem:read_text_file", 91
    of 696 uses), so only un-namespaced names are composed ("Claude Code:Bash");
    `integration_name` is null for 121 uses (plain `bash_tool`, `view`), which
    stay as-is."""
    name = item.get("name")
    integ = item.get("integration_name")
    if name and integ and ":" not in name:
        return f"{integ}:{name}"
    return name


def _blocks_from_content(content, fallback_text: str, files_by_uuid: dict | None = None) -> list[dict]:
    if not content:
        if fallback_text:
            return [make_block(0, "text", fallback_text, raw={"text": fallback_text})]
        return []

    blocks: list[dict] = []
    for idx, item in enumerate(content):
        if not isinstance(item, dict):
            blocks.append(make_block(idx, "unknown", "", raw={"value": item}))
            continue
        item_type = item.get("type")
        if item_type == "text":
            blocks.append(make_block(idx, "text", item.get("text", ""), raw=item))
        elif item_type == "thinking":
            blocks.append(make_block(idx, "thinking", item.get("thinking", ""), raw=item))
        elif item_type == "tool_use":
            blocks.append(
                make_block(
                    idx,
                    "tool_use",
                    _with_sections(json.dumps(item.get("input", {}), default=str), item, ("display_content",)),
                    raw=item,
                    tool_name=_tool_name(item),
                )
            )
            # docs/audit-shapes/reasoning-sweep.md line 13: `$.input.thought`
            # — 405 occurrences, 278,219 total chars — is real reasoning
            # prose nested inside a tool call's `input`, currently invisible
            # as reasoning because `input` is only ever surfaced as the
            # `tool_use` block's JSON-dumped text (above). Verified against
            # production (`SELECT ... WHERE platform='claude_desktop' AND
            # type='tool_use' AND raw_json LIKE '%"thought"%'`): every
            # instance has `tool_name ==
            # "sequential-thinking:sequentialthinking"`, an MCP tool whose
            # `input` is `{thought, thoughtNumber, totalThoughts,
            # nextThoughtNeeded}` — `thoughtNumber`/`totalThoughts`/
            # `nextThoughtNeeded` are legitimate tool-call bookkeeping (this
            # genuinely is a tool call), so the `tool_use` block above is not
            # wrong and is kept as-is. `thought` itself is reasoning content
            # rather than a tool argument in the ordinary sense, so it
            # additionally gets its own `thinking` block instead of staying
            # buried in the tool_use block's JSON dump. Split, not re-typed —
            # both blocks share the same `raw=item` (spec §4.4 rule 4:
            # `raw_json` is never modified by this phase, only what gets
            # *extracted* from it).
            thought = item.get("input", {}).get("thought") if isinstance(item.get("input"), dict) else None
            if isinstance(thought, str) and thought.strip():
                blocks.append(make_block(idx, "thinking", thought, raw=item))
        elif item_type == "tool_result":
            inner = item.get("content")
            text = inner if isinstance(inner, str) else json.dumps(inner, default=str)
            text = _with_sections(text, item, ("display_content", "structured_content"))
            # is_error true on 43 of 693 results (2026-10-12 export).
            blocks.append(make_block(idx, "error" if item.get("is_error") is True else "tool_result", text, raw=item))
        elif item_type in ("image",):
            blocks.append(make_block(idx, "image", "", raw=item))
        elif item_type == "injected_prompt_block":
            # 16 items, `prompt` = the injected text; the provider labels it
            # explicitly (`injection_source`, kept in raw), so no marker
            # heuristic (identifiers.is_injected_context) is used.
            blocks.append(make_block(idx, "injected_context", item.get("prompt") or "", raw=item))
        elif item_type == "token_budget":
            # 31 items, payload {remaining: null}: structural, no text.
            blocks.append(make_block(idx, "meta", "", raw=item))
        elif item_type == "document":
            # 6 items: {file_uuid} (+ `title` on 1). All 6 file_uuids match an
            # entry in the same message's `files`, whose file_name names it
            # (empty for 5) — recorded as existing, content unavailable.
            fname = item.get("title") or (files_by_uuid or {}).get(item.get("file_uuid")) or None
            payload = make_attachment(
                filename=fname, ext=_ext(fname), availability="missing", role="upload", raw=item
            )
            blocks.append(make_block(idx, "attachment", "", raw=item, attachment=payload))
        else:
            blocks.append(make_block(idx, "unknown", "", raw=item))

    for i, b in enumerate(blocks):
        b["idx"] = i
    return blocks


def _ext(filename: str | None) -> str | None:
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return None


def _attachment_blocks(cm: dict, start_idx: int, skip_uuids: frozenset = frozenset()) -> list[dict]:
    blocks: list[dict] = []
    seen_names: set[str] = set()
    for att in cm.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        seen_names.add(att.get("file_name"))
        text = att.get("extracted_content") or ""
        payload = make_attachment(
            filename=att.get("file_name"),
            ext=_ext(att.get("file_name")) or (att.get("file_type") or None),
            bytes_=att.get("file_size"),
            content=text or None,
            availability="text" if text else "missing",
            role="upload",
            raw=att,
        )
        blocks.append(make_block(start_idx + len(blocks), "attachment", text, raw=att, attachment=payload))
    for f in cm.get("files") or []:
        if not isinstance(f, dict):
            continue
        # "files" duplicates "attachments" by file_name when the latter has extracted
        # text; only add it when it's the only record of that file.
        if f.get("file_name") in seen_names:
            continue
        # Already emitted as a `document` content item (6 of 92 files).
        if f.get("file_uuid") in skip_uuids:
            continue
        payload = make_attachment(
            filename=f.get("file_name"),
            ext=_ext(f.get("file_name")),
            availability="missing",
            role="upload",
            raw=f,
        )
        blocks.append(make_block(start_idx + len(blocks), "attachment", "", raw=f, attachment=payload))
    return blocks


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    conversations = data if isinstance(data, list) else [data]

    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        chat_messages = conv.get("chat_messages") or []
        messages = []
        for idx, cm in enumerate(chat_messages):
            role = _SENDER_TO_ROLE.get(cm.get("sender"), cm.get("sender") or "unknown")
            files_by_uuid = {
                f.get("file_uuid"): f.get("file_name") for f in cm.get("files") or [] if isinstance(f, dict)
            }
            blocks = _blocks_from_content(cm.get("content"), cm.get("text", ""), files_by_uuid)
            doc_uuids = frozenset(
                i.get("file_uuid") for i in cm.get("content") or [] if isinstance(i, dict) and i.get("type") == "document"
            )
            blocks += _attachment_blocks(cm, len(blocks), doc_uuids)
            messages.append(make_message(idx, role, None, cm.get("created_at"), blocks, raw=cm))

        yield make_conversation(
            platform=platform,
            native_id=conv.get("uuid"),
            title=conv.get("name"),
            project=None,
            started_at=conv.get("created_at"),
            ended_at=conv.get("updated_at"),
            source_path=str(path),
            raw={k: v for k, v in conv.items() if k != "chat_messages"},
            messages=messages,
        )


def parse_projects(path: Path) -> Iterator[dict]:
    """Claude Projects export: a list of {uuid, name, description,
    prompt_template, docs: [{uuid, filename, content, created_at}], ...}.
    No conversation reference anywhere in this shape (spec §B.2) — projects
    are ingested as standalone records; conv->project mapping is a separate,
    deliberately unhurried decision."""
    data = orjson.loads(path.read_bytes())
    projects = data if isinstance(data, list) else [data]

    for proj in projects:
        if not isinstance(proj, dict):
            continue

        docs = []
        for d in proj.get("docs") or []:
            if not isinstance(d, dict):
                continue
            content = d.get("content") or ""
            docs.append(
                make_attachment(
                    filename=d.get("filename"),
                    ext=_ext(d.get("filename")),
                    bytes_=len(content.encode("utf-8")) if content else None,
                    content=content or None,
                    availability="text" if content else "missing",
                    role="project_knowledge",
                    raw=d,
                )
            )

        yield {
            "platform": platform,
            "native_id": proj.get("uuid"),
            "name": proj.get("name") or "Untitled project",
            "description": proj.get("description"),
            "instructions": proj.get("prompt_template"),
            "created_at": proj.get("created_at"),
            "raw": {k: v for k, v in proj.items() if k != "docs"},
            "docs": docs,
        }


def sniff_artifacts(path: Path) -> bool:
    """`artifacts/<uuid>/artifact.json` from the official export's `frames`
    ZIP: {id, kind, visibility, versions[], active_version, ...}. Structurally
    unrelated to conversations.json; routed via adapters.detect_artifacts()."""
    if path.name != "artifact.json":
        return False
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    return b'"active_version"' in prefix and b'"versions"' in prefix


def _read_text(path: Path) -> str:
    text, _ = decode_utf8_counting_replacements(path.read_bytes())
    return text


def parse_artifacts(path: Path) -> Iterator[dict]:
    """One dict per artifact: its version files as role='artifact' attachments.

    Layouts enumerated from the 2026-10-12 export (10 artifacts, 171 files):
      - flat `versions/<V>.html` (8 artifacts, 9 files) -> ingested;
      - `page.md` beside artifact.json for kind='page' (1) -> ingested;
      - `versions/<V>/project/{Main.dc.html,canvas.json}` for canvas artifacts
        (1 artifact, 7 versions) -> ingested. In the same directories
        `index.html` (1 distinct hash over 8 versions), `SKILL.md` (1 distinct)
        and `artifact-type/**` (every file 1 distinct hash over 8 versions)
        are the editor's scaffolding, not the user's content -> NOT ingested;
      - `versions/<V>.files.json` -> a file manifest, kept in raw only.
    Linking to conversations happens later (loader.link_artifacts), because
    artifact.json carries no conversation reference."""
    root = path.parent
    meta = orjson.loads(path.read_bytes())
    art_id = meta.get("id") or root.name
    versions = meta.get("versions") or []
    titles = {v.get("id"): v.get("title") for v in versions if isinstance(v, dict)}
    active = meta.get("active_version")
    artifact_raw = {k: v for k, v in meta.items() if k != "versions"}
    comments = root / "comments.json"
    if comments.is_file():
        artifact_raw["comments"] = orjson.loads(comments.read_bytes())

    attachments: list[dict] = []

    def add(filename: str, file_path: Path, version_id: str | None, rel: str) -> None:
        text = _read_text(file_path)
        version = next((v for v in versions if v.get("id") == version_id), {})
        attachments.append(
            make_attachment(
                filename=filename,
                ext=_ext(file_path.name),
                bytes_=file_path.stat().st_size,
                content=text or None,
                availability="text" if text else "missing",
                role="artifact",
                raw={"artifact": artifact_raw, "version": version, "path": rel},
            )
        )

    vdir = root / "versions"
    if vdir.is_dir():
        for vid in sorted(titles, key=lambda i: (i != active, i or "")):
            title = titles[vid] or vid
            flat = vdir / f"{vid}.html"
            if flat.is_file():
                add(title, flat, vid, f"versions/{vid}.html")
            proj = vdir / vid / "project"
            if proj.is_dir():
                for f in sorted(p for p in proj.iterdir() if p.is_file()):
                    add(f"{title} \u2014 {f.name}", f, vid, f"versions/{vid}/project/{f.name}")
    page = root / "page.md"
    if page.is_file():
        vid = active if active in titles else (versions[0].get("id") if versions else None)
        add(titles.get(vid) or art_id, page, vid, "page.md")

    yield {
        "platform": platform,
        "native_id": art_id,
        "kind": meta.get("kind"),
        "raw": artifact_raw,
        "attachments": attachments,
    }

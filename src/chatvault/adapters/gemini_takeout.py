from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters._html_lite import html_to_text
from chatvault.adapters.base import make_attachment, make_block, make_conversation, make_message

platform = "gemini"
name = "gemini_takeout"

# Corrected 2026-08-12 after a prior conclusion in this project (twice
# reached, twice wrong) that this export held no real conversation
# content. It does. Verified by full-file measurement, not sampling:
#
#   - 654 distinct conversations (grouped by the id in details[0].url,
#     https://gemini.google.com/app/<id>), 368 of them multi-turn, up to
#     31 turns in one conversation.
#   - `title` on a "Prompted ..." record is the COMPLETE user message, not
#     a truncated summary — median 212 chars, but observed up to 1,000,009
#     chars (a pasted crash log), with no truncation marker found.
#   - `safeHtmlItem[0].html` is the complete assistant response — median
#     3,783 chars, max 124,461 chars, 0 records found ending in an
#     ellipsis/truncation marker across the whole file.
#   - 91 records have no `/app/<id>` in `details` at all: these are
#     product events (77 "Created Gemini Canvas titled ...", 11 "Selected
#     preferred draft", handful of feedback events), not dialogue turns.
#     Real activity, kept, but structurally distinct — see
#     _parse_standalone_event below.
#
# Still genuinely true, not an error: there is no `model` field anywhere
# in this export (checked every record). model_primary is left NULL.

_APP_URL_ID_RE = re.compile(r"/app/([a-f0-9]+)")


def sniff(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    if path.name != "MyActivity.json":
        # This exact filename is what Google Takeout produces for every
        # "My Activity" product category, Gemini included — but the
        # content marker below is what actually disambiguates it, so a
        # renamed file still sniffs correctly; this is just a fast-path.
        pass
    try:
        with path.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError:
        return False
    return b'"Gemini Apps"' in prefix and b'"activityControls"' in prefix and b'"safeHtmlItem"' in prefix


def _conversation_id(record: dict) -> str | None:
    for d in record.get("details") or []:
        url = d.get("url") or ""
        m = _APP_URL_ID_RE.search(url)
        if m:
            return m.group(1)
    return None


def _attachment_blocks(record: dict, idx_start: int) -> list[dict]:
    blocks = []
    idx = idx_start
    for fname in record.get("attachedFiles") or []:
        blocks.append(
            make_block(
                idx,
                "attachment",
                "",
                raw={"filename": fname, "source": "attachedFiles"},
                attachment=make_attachment(
                    filename=fname,
                    availability="missing",  # resolved against gemini_app_uploaded_attachments/ by ingest, not the adapter
                    role="upload",
                    raw={"source_field": "attachedFiles"},
                ),
            )
        )
        idx += 1
    img = record.get("imageFile")
    if img:
        blocks.append(
            make_block(
                idx,
                "attachment",
                "",
                raw={"filename": img, "source": "imageFile"},
                attachment=make_attachment(
                    filename=img,
                    availability="missing",
                    role="upload",
                    raw={"source_field": "imageFile"},
                ),
            )
        )
        idx += 1
    return blocks


def _turn_messages(record: dict, msg_idx: int) -> list[dict]:
    """One activity record with an /app/<id> is one user prompt + (usually)
    one assistant response. Returns 0-2 messages: a record can legitimately
    have only the prompt (response not captured, e.g. an in-flight or
    errored turn — confirmed some of these exist) or, rarely, only a
    response with no "Prompted " title."""
    messages = []
    title = record.get("title") or ""
    time_ = record.get("time")

    if title.startswith("Prompted "):
        user_text = title[len("Prompted "):]
        blocks = [make_block(0, "text", user_text, raw={"title": title})]
        blocks.extend(_attachment_blocks(record, len(blocks)))
        messages.append(make_message(msg_idx, "user", None, time_, blocks, raw={"title": title}))
        msg_idx += 1

    html_items = record.get("safeHtmlItem") or []
    if html_items:
        html = html_items[0].get("html", "")
        text = html_to_text(html)
        if text:
            blocks = [make_block(0, "text", text, raw={"html": html})]
            messages.append(make_message(msg_idx, "assistant", None, time_, blocks, raw={"html_source": "safeHtmlItem"}))
            msg_idx += 1

    return messages


def _parse_standalone_event(record: dict) -> dict:
    """One of the 91 no-id records: a real product event (Canvas created,
    draft selected, feedback given), not a dialogue turn. No conversation
    to attach it to, so it becomes its own single-message conversation
    rather than being dropped — same "lose nothing, flag for later"
    handling as everywhere else in this codebase. `raw.event_only = True`
    is the marker a tag rule can match on later if the user wants a
    `review/gemini-event` (or similar) tag applied at ingest; the adapter
    itself does not apply tags, consistent with every other adapter."""
    title = record.get("title") or "(untitled Gemini event)"
    time_ = record.get("time")
    native_id = "event:" + hashlib.sha1(f"{title}|{time_}".encode("utf-8")).hexdigest()[:16]

    blocks = [make_block(0, "system", title, raw=record)]
    subtitles = record.get("subtitles") or []
    for i, st in enumerate(subtitles, start=1):
        name = st.get("name") if isinstance(st, dict) else str(st)
        if name:
            blocks.append(make_block(i, "text", name, raw=st))

    message = make_message(0, "system", None, time_, blocks, raw={**record, "event_only": True})

    return make_conversation(
        platform=platform,
        native_id=native_id,
        title=title,
        project=None,
        started_at=time_,
        ended_at=time_,
        source_path="",  # filled in by caller with the real path
        raw={**record, "event_only": True},
        messages=[message],
    )


def parse(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    records = data if isinstance(data, list) else [data]

    groups: dict[str, list[dict]] = {}
    standalone: list[dict] = []

    for r in records:
        if not isinstance(r, dict):
            continue
        conv_id = _conversation_id(r)
        if conv_id:
            groups.setdefault(conv_id, []).append(r)
        else:
            standalone.append(r)

    for conv_id, recs in groups.items():
        recs_sorted = sorted(recs, key=lambda r: r.get("time") or "")

        messages: list[dict] = []
        idx = 0
        timestamps: list[str] = []
        for r in recs_sorted:
            t = r.get("time")
            if t:
                timestamps.append(t)
            for m in _turn_messages(r, idx):
                messages.append(m)
                idx += 1

        if not messages:
            continue

        first_title = recs_sorted[0].get("title") or ""
        if first_title.startswith("Prompted "):
            first_title = first_title[len("Prompted "):]

        yield make_conversation(
            platform=platform,
            native_id=conv_id,
            title=first_title[:200] or None,
            project=None,  # confirmed: no workspace/project concept anywhere in this export
            started_at=min(timestamps) if timestamps else None,
            ended_at=max(timestamps) if timestamps else None,
            source_path=str(path),
            raw={"activity_record_count": len(recs_sorted), "conversation_url": f"https://gemini.google.com/app/{conv_id}"},
            messages=messages,
        )

    for r in standalone:
        conv = _parse_standalone_event(r)
        conv["source_path"] = str(path)
        yield conv

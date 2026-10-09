from __future__ import annotations

import hashlib
import html as html_mod
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
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

# Updated 2026-09-21: Google switched Takeout "My Activity" exports from JSON
# to HTML between the 2026-08-12 and 2026-09-16 exports. Same logical records,
# different container — one `<div class="outer-cell ...">` per activity record.
# Verified by full-file enumeration of both real HTML exports (104 cells), see
# docs/reports/gemini_myactivity_html_2026-09-21.md. The adapter now accepts
# both; `parse` dispatches on suffix.

_APP_URL_ID_RE = re.compile(r"/app/([a-f0-9]+)")


def sniff(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in (".json", ".html"):
        return False
    # "MyActivity.json" / "MyActivity.html" is what Takeout names every "My
    # Activity" product category, Gemini included — the content markers below
    # are what actually disambiguate it, so a renamed file still sniffs
    # correctly; the filename check is just documentation of the usual shape.
    # HTML exports front-load ~140KB of Material Design CSS before the first
    # record cell; the read window must cover it.
    window = 65536 if suffix == ".json" else 512 * 1024
    try:
        with path.open("rb") as fh:
            prefix = fh.read(window)
    except OSError:
        return False
    if suffix == ".json":
        return b'"Gemini Apps"' in prefix and b'"activityControls"' in prefix and b'"safeHtmlItem"' in prefix
    # HTML variant: "Gemini Apps" header + the per-cell boilerplate link.
    return b"Gemini Apps" in prefix and b"outer-cell" in prefix and b"myaccount.google.com/activitycontrols" in prefix


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
        # .strip() parity with the HTML-variant parser (_parse_html_cell,
        # which strips after unescaping): the JSON title carries a trailing
        # space the HTML export's <br>-handling never produces, so the same
        # prompt hashed differently across variants and every re-export of
        # an already-ingested conversation surfaced as a flagged duplicate
        # instead of a noop (387 of them on the 2026-10-02 ingest).
        user_text = title[len("Prompted "):].strip()
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


# ---- HTML-variant (MyActivity.html) -------------------------------------
#
# Cell anatomy, verified on all 104 cells across both real files:
#   <div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
#     <div class="mdl-grid">
#       <div class="header-cell ..."><p class="mdl-typography--title">Gemini Apps<br></p></div>
#       <div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">
#         Prompted <user text><br>[Attached N file(s).<br>- <a href="stored">display</a><br>…]
#         <timestamp><br><response HTML>
#       </div>
#       <div class="content-cell ... text-right"></div>          <- empty in all 104
#       <div class="content-cell mdl-cell--12-col mdl-typography--caption">
#         Products / Details (the /app/<id> link) / Why is this here?
#       </div>
#
# The "Attached N" count is unreliable (one cell says "Attached 2 files."
# followed by 6 links) — count the actual <a> list, not the number.
# No-link cells (20x "Cleared previous feedback", 1x "Created Gemini Canvas")
# are the same standalone-event class as the JSON variant's 91 no-id records.

_CELL_SPLIT_RE = re.compile(r'<div class="outer-cell')
_PAYLOAD_RE = re.compile(
    r'content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">(.*?)'
    r'<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right">',
    re.DOTALL,
)
# "Sep 16, 2026, 7:08:02 PM CEST" — U+202F narrow nbsp appears before AM/PM.
_TS_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}, 20\d\d,"
    r" \d{1,2}:\d{2}:\d{2}[\s\u202f]+(?:AM|PM) [A-Z]{2,5}"
)
_ATT_LINK_RE = re.compile(r'<a href="([^"]+)">([^<]*)</a>')
_TZ_OFFSETS = {"UTC": 0, "GMT": 0, "CET": 1, "CEST": 2}


def _parse_html_timestamp(raw_ts: str) -> str:
    """'Sep 16, 2026, 7:08:02\u202fPM CEST' -> ISO 8601 UTC.
    Google writes a fixed local abbreviation, not an offset; the map covers
    the zones observed in real exports. Unknown abbreviations fall back to
    naive-UTC rather than dropping the record (timezone shown in raw)."""
    cleaned = raw_ts.replace("\u202f", " ").replace("\xa0", " ")
    m = re.match(r"(.*?)\s+([A-Z]{2,5})$", cleaned)
    ts_part, tz_abbr = cleaned, "UTC"
    if m:
        ts_part, tz_abbr = m.group(1), m.group(2)
    dt = datetime.strptime(ts_part, "%b %d, %Y, %I:%M:%S %p")
    offset = _TZ_OFFSETS.get(tz_abbr, 0)
    dt = dt.replace(tzinfo=timezone(timedelta(hours=offset))).astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _parse_html_cell(cell: str) -> dict | None:
    """One outer-cell div -> {'conv_id', 'title', 'time', 'attachments', 'response_html'}.
    Returns None for cells with no payload body."""
    m = _PAYLOAD_RE.search(cell)
    if not m:
        return None
    payload = m.group(1)

    ts_m = _TS_RE.search(payload)
    time_ = _parse_html_timestamp(ts_m.group(0)) if ts_m else None

    prompt_zone = payload[: ts_m.start()] if ts_m else payload
    response_html = payload[ts_m.end() :] if ts_m else ""

    prompt_zone = prompt_zone.lstrip()

    # Prompt verbs observed across the real exports: "Prompted" (ordinary
    # turn) and "Branched" (a prompt that forked the conversation — same
    # payload layout). Cells can carry leading whitespace before the verb.
    verb_m = re.match(r"(Prompted|Branched)\s", prompt_zone)

    attachments: list[tuple[str, str]] = []
    title_text = ""
    if verb_m:
        # Cut the "Attached N file(s)." section: from that marker to the end
        # of prompt_zone is only attachment links and <br>s.
        att_m = re.search(r"<br>\s*Attached \d+ files?\.", prompt_zone)
        att_zone = prompt_zone[att_m.end() :] if att_m else ""
        prompt_text = prompt_zone[verb_m.end() : att_m.start()] if att_m else prompt_zone[verb_m.end() :]
        prompt_text = prompt_text.rstrip()
        if prompt_text.endswith("<br>"):
            prompt_text = prompt_text[: -len("<br>")]
        # Prompts are plain text; <br> = in-prompt line breaks, any other
        # residual markup is stripped (prompt zone carries no other tags in
        # the observed exports).
        title_text = html_mod.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", prompt_text))).strip()
        attachments = _ATT_LINK_RE.findall(att_zone)
    else:
        # Event cells: "Cleared previous feedback", "Created Gemini Canvas …"
        title_text = html_mod.unescape(re.sub(r"<[^>]+>", " ", prompt_zone)).strip()

    conv_m = _APP_URL_ID_RE.search(cell)
    return {
        "conv_id": conv_m.group(1) if conv_m else None,
        "is_prompt": bool(verb_m),
        "verb": verb_m.group(1) if verb_m else None,
        "title": title_text,
        "time": time_,
        "attachments": attachments,  # (stored_filename, display_name)
        "response_html": response_html,
        "raw_cell": cell,
    }


def _html_turn_messages(rec: dict, msg_idx: int) -> list[dict]:
    """Mirror of _turn_messages for the HTML record shape."""
    messages = []
    if rec["is_prompt"] and rec["title"]:
        blocks = [make_block(0, "text", rec["title"], raw={"title": f"{rec['verb']} {rec['title']}"})]
        for stored, display in rec["attachments"]:
            blocks.append(
                make_block(
                    len(blocks),
                    "attachment",
                    "",
                    raw={"filename": stored, "display": display, "source": "html_attached_files"},
                    attachment=make_attachment(
                        # href is URL-encoded (e.g. %20); the file on disk
                        # sits beside MyActivity.html under its real name.
                        filename=urllib.parse.unquote(stored),
                        availability="missing",
                        role="upload",
                        raw={"source_field": "html_attached_files", "display": display},
                    ),
                )
            )
        messages.append(make_message(msg_idx, "user", None, rec["time"], blocks, raw={"html_cell": True}))
        msg_idx += 1

    text = html_to_text(rec["response_html"])
    if text:
        blocks = [make_block(0, "text", text, raw={"html": rec["response_html"]})]
        messages.append(make_message(msg_idx, "assistant", None, rec["time"], blocks, raw={"html_source": "MyActivity.html"}))
        msg_idx += 1
    return messages


def _parse_html(path: Path) -> Iterator[dict]:
    html = path.read_text(encoding="utf-8")
    cells = _CELL_SPLIT_RE.split(html)[1:]

    groups: dict[str, list[dict]] = {}
    standalone: list[dict] = []
    for c in cells:
        rec = _parse_html_cell(c)
        if rec is None:
            continue
        if rec["conv_id"]:
            groups.setdefault(rec["conv_id"], []).append(rec)
        else:
            standalone.append(rec)

    for conv_id, recs in groups.items():
        recs_sorted = sorted(recs, key=lambda r: r["time"] or "")
        messages: list[dict] = []
        idx = 0
        timestamps: list[str] = []
        for r in recs_sorted:
            if r["time"]:
                timestamps.append(r["time"])
            for m in _html_turn_messages(r, idx):
                messages.append(m)
                idx += 1
        if not messages:
            continue
        first_title = recs_sorted[0]["title"]
        yield make_conversation(
            platform=platform,
            native_id=conv_id,
            title=first_title[:200] or None,
            project=None,
            started_at=min(timestamps) if timestamps else None,
            ended_at=max(timestamps) if timestamps else None,
            source_path=str(path),
            raw={"activity_record_count": len(recs_sorted), "conversation_url": f"https://gemini.google.com/app/{conv_id}"},
            messages=messages,
        )

    for r in standalone:
        title = r["title"] or "(untitled Gemini event)"
        native_id = "event:" + hashlib.sha1(f"{title}|{r['time']}".encode("utf-8")).hexdigest()[:16]
        blocks = [make_block(0, "system", title, raw=r["raw_cell"])]
        message = make_message(0, "system", None, r["time"], blocks, raw={"html_cell": True, "event_only": True})
        yield make_conversation(
            platform=platform,
            native_id=native_id,
            title=title,
            project=None,
            started_at=r["time"],
            ended_at=r["time"],
            source_path=str(path),
            raw={"html_cell": True, "event_only": True},
            messages=[message],
        )


def parse(path: Path) -> Iterator[dict]:
    if path.suffix.lower() == ".html":
        yield from _parse_html(path)
        return
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

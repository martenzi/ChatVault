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
from chatvault.adapters.base import make_attachment, make_block, make_conversation, make_message, normalize_title

platform = "gemini"
name = "gemini_takeout"

# Per-file parse diagnostics, reset at the start of each parse() call and
# read by ingest afterwards (see ingest.py's _adapter_parse_warnings). A
# timestamp is metadata, not content: an unparseable one degrades the
# message to created_at=None and flags the conversation
# review/timestamp-unparsed rather than failing the whole file — format
# drift in these exports is ongoing (the same account changed timestamp
# shape between the Sep-30 and Oct-16 2026 exports), so a loud counter plus
# a review tag beats a fatal ValueError that loses every conversation.
parse_stats: dict = {"timestamps_unparsed": 0, "unknown_timezones": {}}

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


# ---- Canvas events and standalone-event identity -------------------------
#
# A "Created Gemini Canvas titled <name>" activity record is the delivered
# artifact (a page, script, letter, report), not dialogue. Both Takeout
# variants hold it differently: JSON puts the name in `title` and the content
# in subtitles[0].name; HTML delivers one zone `titled\xa0<name><br><content>`
# (the name ends at the first <br>). Both become the same conversation: a
# `Gemini Canvas: <name>` title, one summary text block, and the content as an
# attachment with role "artifact" — never chat text, never a title. Verified
# against 3016 (JSON) and 4120 (HTML): same name, same 50,171-char content,
# same second.

CANVAS_PREFIX_RE = re.compile(r"^\s*Created Gemini Canvas titled\s*")
_FENCE_RE = re.compile(r"\A\s*```([A-Za-z0-9_+#.-]*)[ \t]*\n(.*?)\n?```\s*\Z", re.DOTALL)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_PROBABLE_SOURCE_WINDOW_MIN = 5.0
_FENCE_KINDS = {
    "html": ("html", "text/html"),
    "javascript": ("js", "text/javascript"),
    "js": ("js", "text/javascript"),
    "typescript": ("ts", "text/typescript"),
    "python": ("py", "text/x-python"),
    "py": ("py", "text/x-python"),
    "bash": ("sh", "text/x-shellscript"),
    "sh": ("sh", "text/x-shellscript"),
    "shell": ("sh", "text/x-shellscript"),
    "zsh": ("zsh", "text/x-shellscript"),
    "latex": ("tex", "application/x-tex"),
    "tex": ("tex", "application/x-tex"),
    "markdown": ("md", "text/markdown"),
    "md": ("md", "text/markdown"),
    "json": ("json", "application/json"),
    "css": ("css", "text/css"),
    "xml": ("xml", "application/xml"),
    "yaml": ("yaml", "text/yaml"),
    "sql": ("sql", "application/sql"),
    "svg": ("svg", "image/svg+xml"),
}


def is_canvas_title(title: str | None) -> bool:
    return bool(title and CANVAS_PREFIX_RE.match(title))


def _squash(text: str) -> str:
    return " ".join(text.split())


def _second_utc(time_: str | None) -> str:
    """ISO time truncated to the second, UTC — the JSON export carries
    milliseconds, the HTML export does not, and the same event must hash the
    same in both."""
    if not time_:
        return ""
    try:
        dt = datetime.fromisoformat(time_.replace("Z", "+00:00"))
    except ValueError:
        return time_
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_native_id(title: str, time_: str | None) -> str:
    """Identity of a standalone (no /app/<id>) event. Hashes the *normalised*
    title and the time to the second, so NBSP-vs-space and ms-vs-no-ms
    differences between Takeout variants no longer mint a second row."""
    key = f"{normalize_title(title) or ''}|{_second_utc(time_)}"
    return "event:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def canvas_native_id(name: str, time_: str | None) -> str:
    key = f"{_squash(name).casefold()}|{_second_utc(time_)}"
    return "canvas:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _split_fence(content: str) -> tuple[str, str]:
    """(language, inner) for content wrapped in one outer code fence, else
    ("", content)."""
    m = _FENCE_RE.match(content)
    if not m:
        return "", content.strip()
    return m.group(1).lower(), m.group(2).strip("\n")


def _canvas_name_fallback(content: str) -> str:
    m = _HTML_TITLE_RE.search(content)
    if m and _squash(html_mod.unescape(m.group(1))):
        return _squash(html_mod.unescape(m.group(1)))
    m = _MD_HEADING_RE.search(content)
    if m and _squash(m.group(1)):
        return _squash(m.group(1))
    for line in content.splitlines():
        if line.strip():
            return _squash(line)[:80]
    return "Untitled canvas"


def split_canvas_json(title: str, subtitles: list | None) -> tuple[str, str]:
    """(name, content) from a JSON-variant canvas record."""
    name = CANVAS_PREFIX_RE.sub("", title, count=1)
    content = ""
    if subtitles:
        st = subtitles[0]
        content = (st.get("name") if isinstance(st, dict) else str(st)) or ""
    return name, content


def split_canvas_html(zone_html: str) -> tuple[str, str]:
    """(name, content) from an HTML-variant zone: everything after the verb up
    to the first <br> is the name, everything after it the content."""
    zone = CANVAS_PREFIX_RE.sub("", zone_html.lstrip(), count=1)
    head, sep, rest = zone.partition("<br>")
    name = html_mod.unescape(re.sub(r"<[^>]+>", "", head))
    content = html_mod.unescape(re.sub(r"<br\s*/?>", "\n", rest)) if sep else ""
    return name, content


def probable_source_chat(time_: str | None, assistant_times: list[tuple[str, str | None]]) -> list[dict]:
    """Gemini conversations with an assistant message within five minutes of
    the canvas event, nearest first. Takeout carries no link between a canvas
    and its chat, so this is a recorded candidate list, never a link."""
    if not time_:
        return []
    try:
        t0 = datetime.fromisoformat(_second_utc(time_).replace("Z", "+00:00"))
    except ValueError:
        return []
    hits: dict[str, float] = {}
    for native_id, at in assistant_times:
        if not at:
            continue
        try:
            t1 = datetime.fromisoformat(_second_utc(at).replace("Z", "+00:00"))
        except ValueError:
            continue
        minutes = abs((t1 - t0).total_seconds()) / 60
        if minutes <= _PROBABLE_SOURCE_WINDOW_MIN and minutes < hits.get(native_id, 1e9):
            hits[native_id] = minutes
    return [{"native_id": n, "minutes_apart": round(m, 2)} for n, m in sorted(hits.items(), key=lambda kv: (kv[1], kv[0]))]


def canvas_conversation(
    *,
    name: str,
    content: str,
    time_: str | None,
    variant: str,
    raw: dict,
    source_path: str,
    msg_raw: dict | None = None,
    source_chats: list[dict] | None = None,
) -> dict:
    """The canonical conversation for one canvas, identical for both export
    variants (and for `chatvault repair gemini-canvas`, which calls this with
    the stored raw record)."""
    lang, inner = _split_fence(content)
    name = _squash(name) or _canvas_name_fallback(inner)
    ext, mime = _FENCE_KINDS.get(lang, ("md", "text/markdown") if not lang else (lang[:8] or "txt", "text/plain"))
    kind = lang or "markdown"
    filename = re.sub(r"[\\/:*?\"<>|]+", "-", name)[:100].strip(" .") or "canvas"
    summary = f"Created Gemini Canvas “{name}” — {kind}, {len(inner)} chars. Stored as artifact."
    block_raw = {"source": "gemini_canvas", "variant": variant, "probable_source_chat": source_chats or []}
    blocks = [
        make_block(0, "text", summary, raw=block_raw),
        make_block(
            1,
            "attachment",
            "",
            raw={"filename": f"{filename}.{ext}", "source": "gemini_canvas"},
            attachment=make_attachment(
                filename=f"{filename}.{ext}",
                mime=mime,
                ext=ext,
                bytes_=len(inner),
                content=inner,
                availability="text",
                role="artifact",
                raw={"source": "gemini_canvas", "variant": variant},
            ),
        ),
    ]
    message = make_message(0, "system", None, time_, blocks, raw={**(msg_raw or {}), "event_only": True, "canvas": True})
    return make_conversation(
        platform=platform,
        native_id=canvas_native_id(name, time_),
        title=f"Gemini Canvas: {name}",
        project=None,
        started_at=time_,
        ended_at=time_,
        source_path=source_path,
        raw={**raw, "event_only": True, "canvas": True, "probable_source_chat": source_chats or []},
        messages=[message],
    )


def canvas_from_html_record(rec: dict, source_path: str, assistant_times: list[tuple[str, str | None]]) -> dict:
    """Canvas conversation from a parsed HTML cell (`_parse_html_cell`)."""
    name, content = split_canvas_html(rec["zone_html"])
    return canvas_conversation(
        name=name,
        content=content,
        time_=rec["time"],
        variant="html",
        raw={"html_cell": True, "raw_cell": rec["raw_cell"]},
        source_path=source_path,
        msg_raw={**({"timestamp_unparsed": True} if rec.get("timestamp_unparsed") else {}), "html_cell": True},
        source_chats=probable_source_chat(rec["time"], assistant_times),
    )


def _assistant_times(conv: dict) -> list[tuple[str, str | None]]:
    return [(conv["native_id"], m["created_at"]) for m in conv["messages"] if m["role"] == "assistant"]


def _parse_standalone_event(record: dict, assistant_times: list | None = None) -> dict:
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
    if is_canvas_title(title):
        name, content = split_canvas_json(title, record.get("subtitles"))
        return canvas_conversation(
            name=name,
            content=content,
            time_=time_,
            variant="json",
            raw=record,
            source_path="",  # filled in by caller with the real path
            source_chats=probable_source_chat(time_, assistant_times or []),
        )
    native_id = event_native_id(title, time_)

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
# Two timestamp shapes observed across real exports, both always
# <br>-delimited on their own line (verified: every cell in every export —
# 32/32, 72/72, 121/121, 1901/1901 month-first cells and 1904/1904
# day-first cells in the Oct-16 h1 export). The <br> anchors also keep the
# regex from matching date strings quoted inside prompt/response prose
# (e.g. "the time (Friday, July 26, 2025, 1:56:37 AM CEST)" in h1-oct16).
#   'Sep 16, 2026, 7:08:02<nbsp>PM CEST'   month-first, 12h — pre-Oct-16 exports
#   'Sep 16, 2026, 7:08:02 CEST'           month-first, 24h variant
#   '16 Oct 2026, 16:34:52 CEST'           day-first, 24h — Oct-16 h1 export
_MONTHS = r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_TS_RE = re.compile(
    r"<br>\s*("
    rf"(?:{_MONTHS})[a-z]* \d{{1,2}}, 20\d\d, \d{{1,2}}:\d{{2}}:\d{{2}}[\s\u202f]+(?:AM|PM)[\s\u202f]+[A-Z]{{2,5}}"
    rf"|(?:{_MONTHS})[a-z]* \d{{1,2}}, 20\d\d, \d{{1,2}}:\d{{2}}:\d{{2}}[\s\u202f]+[A-Z]{{2,5}}"
    rf"|\d{{1,2}} (?:{_MONTHS})[a-z]* 20\d\d, \d{{1,2}}:\d{{2}}:\d{{2}}[\s\u202f]+[A-Z]{{2,5}}"
    r")\s*<br>"
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
    # 'Sept' (4-letter) observed 294x in the Oct-16 h1 export's real cells;
    # %b only accepts 'Sep'.
    ts_part = re.sub(r"\bSept\b", "Sep", ts_part)
    dt = None
    for fmt in (
        "%b %d, %Y, %I:%M:%S %p",  # 'Sep 16, 2026, 7:08:02 PM'
        "%d %b %Y, %H:%M:%S",      # '16 Oct 2026, 16:34:52' (Oct-16 h1 export, day-first/24h)
        "%B %d, %Y, %I:%M:%S %p",  # full-month-name variants ('July 26, 2025, 1:56:37 AM'
        "%d %B %Y, %H:%M:%S",      #  seen quoted in prose; real cells observed abbreviated so far)
        "%b %d, %Y, %H:%M:%S",     # 'Sep 16, 2026, 19:08:02' (24h month-first)
        "%B %d, %Y, %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(ts_part, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(f"unparseable Gemini HTML timestamp: {raw_ts!r}")
    if tz_abbr not in _TZ_OFFSETS:
        # Treated as UTC (was already the silent fallback); now also counted
        # so a zone we don't know surfaces in the ingest report instead of
        # silently writing a timestamp wrong by hours.
        unknown = parse_stats["unknown_timezones"]
        unknown[tz_abbr] = unknown.get(tz_abbr, 0) + 1
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
    time_ = None
    timestamp_unparsed = False
    if ts_m:
        try:
            time_ = _parse_html_timestamp(ts_m.group(1))
        except ValueError:
            # Timestamp is metadata, not content: degrade to created_at=None,
            # mark the record so the ingest flag pass can tag the
            # conversation review/timestamp-unparsed, and keep going — one
            # new format must not drop every conversation in the file.
            parse_stats["timestamps_unparsed"] += 1
            timestamp_unparsed = True

    # group(1) is the timestamp itself; ts_m.start()/<br>-aware bounds keep
    # the delimiter out of prompt_zone while response_html keeps its leading
    # <br> exactly as before the <br>-anchoring change.
    prompt_zone = payload[: ts_m.start()] if ts_m else payload
    response_html = payload[ts_m.end(1) :] if ts_m else ""

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
        "timestamp_unparsed": timestamp_unparsed,
        "zone_html": prompt_zone,
        "raw_cell": cell,
    }


def _html_turn_messages(rec: dict, msg_idx: int) -> list[dict]:
    """Mirror of _turn_messages for the HTML record shape."""
    messages = []
    # Marker the post-load flag pass (tags.flag_timestamp_unparsed) keys on:
    # lands in messages.raw_json so a failed timestamp parse stays
    # identifiable long after ingest.
    msg_raw = {"timestamp_unparsed": True} if rec.get("timestamp_unparsed") else {}
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
        messages.append(make_message(msg_idx, "user", None, rec["time"], blocks, raw={**msg_raw, "html_cell": True}))
        msg_idx += 1

    text = html_to_text(rec["response_html"])
    if text:
        blocks = [make_block(0, "text", text, raw={"html": rec["response_html"]})]
        messages.append(make_message(msg_idx, "assistant", None, rec["time"], blocks, raw={**msg_raw, "html_source": "MyActivity.html"}))
        msg_idx += 1
    return messages


def _parse_html(path: Path) -> Iterator[dict]:
    html = path.read_text(encoding="utf-8")
    cells = _CELL_SPLIT_RE.split(html)[1:]

    groups: dict[str, list[dict]] = {}
    standalone: list[dict] = []
    produced: list[dict] = []  # dialogue conversations, for the canvases' probable_source_chat
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
        conv = make_conversation(
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
        produced.append(conv)
        yield conv

    assistant_times = [t for c in produced for t in _assistant_times(c)]
    for r in standalone:
        if is_canvas_title(r["title"]):
            yield canvas_from_html_record(r, str(path), assistant_times)
            continue
        title = r["title"] or "(untitled Gemini event)"
        native_id = event_native_id(title, r["time"])
        blocks = [make_block(0, "system", title, raw=r["raw_cell"])]
        message = make_message(
            0,
            "system",
            None,
            r["time"],
            blocks,
            raw={
                **({"timestamp_unparsed": True} if r.get("timestamp_unparsed") else {}),
                "html_cell": True,
                "event_only": True,
            },
        )
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
    # Runs on first next(), before any cell is parsed — ingest reads these
    # after the generator is exhausted.
    parse_stats["timestamps_unparsed"] = 0
    parse_stats["unknown_timezones"] = {}
    if path.suffix.lower() == ".html":
        yield from _parse_html(path)
        return
    data = orjson.loads(path.read_bytes())
    records = data if isinstance(data, list) else [data]

    groups: dict[str, list[dict]] = {}
    standalone: list[dict] = []
    produced: list[dict] = []

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

        conv = make_conversation(
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
        produced.append(conv)
        yield conv

    assistant_times = [t for c in produced for t in _assistant_times(c)]
    for r in standalone:
        conv = _parse_standalone_event(r, assistant_times)
        conv["source_path"] = str(path)
        yield conv

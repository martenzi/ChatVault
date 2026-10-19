from __future__ import annotations

import hashlib
import html as html_mod
import re
import urllib.parse
from pathlib import Path
from typing import Iterator

import orjson

from chatvault.adapters import gemini_takeout as _gt
from chatvault.adapters._html_lite import html_to_text
from chatvault.adapters.base import make_block, make_conversation, make_message

platform = "google_ai_mode"
name = "google_ai_mode"

# Same contract as gemini_takeout.parse_stats (read by ingest after parse):
# the timestamp helper is shared, so its counters are mirrored out of
# gemini_takeout's dict at the end of every parse.
parse_stats: dict = {"timestamps_unparsed": 0, "unknown_timezones": {}}

# Google Search "AI Mode" activity, from Takeout "My Activity/AI Mode".
# NOT Gemini app chat — separate platform by user decision, see
# docs/68_PROMPT_idle_backlog_google_and_hygiene.md §1.2.
#
# Shape, enumerated over all 8 real files (6 HTML + 2 byte-identical JSON,
# 1,607 records), not sampled:
#   - record = one-shot Q&A: "Searched for <query>" + timestamp + a body of
#     `<p><strong>Your prompt:</strong>…</p><p><strong>Search's response:
#     </strong>…</p>`. 1,228 HTML + 372 JSON records carry a response.
#   - the visible link text equals the q= URL parameter in every cell
#     (modulo \r\n); "Your prompt:" is Google's *rewritten* query in 85 HTML
#     cells (typos corrected). The user's own words are the link text, so the
#     user message uses it; the rewrite is kept in message raw.
#   - 8 HTML / 5 JSON records have no response: 2 HTML "Your prompt" only,
#     4 HTML / 3 JSON query only, and 4 HTML / 2 JSON "Used AI Mode" product
#     events with no query at all.
#   - no `/app/<id>` anywhere, no model field.

_HEADER_RE = re.compile(r'mdl-typography--title">AI Mode<')
_LINK_RE = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)
_PROMPT_RE = re.compile(r"<strong>Your prompt:</strong><br>\s*(.*?)</p>", re.S)
_RESP_MARK = "<strong>Search&#39;s response:</strong>"
_VERB_RE = re.compile(r"\s*(Searched for|Used)[\s\xa0]+", re.S)
_ACCOUNT_DIR_RE = re.compile(r"account|desire|private", re.I)


def sniff(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in (".json", ".html"):
        return False
    window = 512 * 1024 if suffix == ".html" else 4096
    try:
        with path.open("rb") as fh:
            prefix = fh.read(window)
    except OSError:
        return False
    if suffix == ".json":
        return b'"header": "AI Mode"' in prefix and b'"activityControls"' in prefix
    return b'mdl-typography--title">AI Mode<' in prefix and b"outer-cell" in prefix


def _account_hint(path: Path) -> str | None:
    # Folder structure is the only provenance (no /app/<id> to match
    # accounts by); recorded as a hint, never an inferred account.
    for part in reversed(path.parts):
        if part.startswith("Takeout") and _ACCOUNT_DIR_RE.search(part):
            return part
    return None


def _html_to_prompt(frag: str) -> str:
    return html_mod.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", frag))).strip()


def _iso_seconds(ts: str | None) -> str | None:
    # JSON times carry milliseconds, HTML times only seconds; native_id must
    # be identical for the same activity record in both.
    return re.sub(r"\.\d+Z$", "Z", ts) if ts else ts


def _native_id(time_: str | None, query: str, verb: str) -> str:
    h = hashlib.sha1(" ".join(query.split()).encode("utf-8")).hexdigest()[:16]
    return f"{'q' if verb == 'Searched for' else 'ev'}:{_iso_seconds(time_) or 'notime'}:{h}"


def _html_records(path: Path) -> Iterator[dict]:
    text = path.read_text(encoding="utf-8")
    for cell in _gt._CELL_SPLIT_RE.split(text)[1:]:
        if not _HEADER_RE.search(cell):
            continue
        m = _gt._PAYLOAD_RE.search(cell)
        if not m:
            continue
        payload = m.group(1)
        ts_m = _gt._TS_RE.search(payload)
        time_, unparsed = None, False
        if ts_m:
            try:
                time_ = _gt._parse_html_timestamp(ts_m.group(1))
            except ValueError:
                unparsed = True
                parse_stats["timestamps_unparsed"] += 1
        pre = payload[: ts_m.start()] if ts_m else payload
        post = payload[ts_m.end(1):] if ts_m else ""
        verb_m = _VERB_RE.match(pre)
        verb = verb_m.group(1) if verb_m else "unknown"
        rest = pre[verb_m.end():] if verb_m else pre
        link = _LINK_RE.search(rest)
        if link:
            query = html_mod.unescape(link.group(2)).strip()
            if not query:
                qm = re.search(r"[?&]q=([^&]*)", html_mod.unescape(link.group(1)))
                query = urllib.parse.unquote_plus(qm.group(1)).strip() if qm else ""
        else:
            query = html_mod.unescape(re.sub(r"<[^>]+>", "", rest)).strip()
        yp = _PROMPT_RE.search(post)
        resp = post.split(_RESP_MARK, 1)[1] if _RESP_MARK in post else None
        yield {
            "verb": verb, "query": query, "time": time_, "timestamp_unparsed": unparsed,
            "your_prompt": _html_to_prompt(yp.group(1)) if yp else None,
            "response_html": resp, "src": "html",
        }


def _json_records(path: Path) -> Iterator[dict]:
    data = orjson.loads(path.read_bytes())
    for r in data if isinstance(data, list) else [data]:
        if not isinstance(r, dict) or r.get("header") != "AI Mode":
            continue
        title = r.get("title") or ""
        vm = _VERB_RE.match(title + " ")
        verb = vm.group(1) if vm else "unknown"
        query = title[vm.end():].strip() if vm else title
        body = (r.get("safeHtmlItem") or [{}])[0].get("html", "")
        yp = _PROMPT_RE.search(body)
        resp = body.split(_RESP_MARK, 1)[1] if _RESP_MARK in body else None
        yield {
            "verb": verb, "query": query, "time": r.get("time"), "timestamp_unparsed": False,
            "your_prompt": _html_to_prompt(yp.group(1)) if yp else None,
            "response_html": resp, "src": "json",
        }


def parse(path: Path) -> Iterator[dict]:
    parse_stats["timestamps_unparsed"] = 0
    _gt.parse_stats["unknown_timezones"] = {}
    parse_stats["unknown_timezones"] = {}
    records = _html_records(path) if path.suffix.lower() == ".html" else _json_records(path)
    hint = _account_hint(path)
    for rec in records:
        verb, query, time_ = rec["verb"], rec["query"], rec["time"]
        msg_raw = {"timestamp_unparsed": True} if rec["timestamp_unparsed"] else {}
        conv_raw = {"record_kind": "query" if verb == "Searched for" else "event", "takeout_account_hint": hint, "source_format": rec["src"]}
        if verb != "Searched for" or not query:
            title = f"{verb} AI Mode" if verb != "unknown" else "(unrecognised AI Mode record)"
            msg = make_message(0, "system", None, time_, [make_block(0, "system", title, raw={})], raw={**msg_raw, "event_only": True})
            yield make_conversation(
                platform=platform, native_id=_native_id(time_, title, "event"), title=title,
                project=None, started_at=time_, ended_at=time_, source_path=str(path),
                raw={**conv_raw, "event_only": True}, messages=[msg],
            )
            continue
        user_raw = {**msg_raw}
        if rec["your_prompt"] and rec["your_prompt"] != query:
            user_raw["rewritten_prompt"] = rec["your_prompt"]
        messages = [make_message(0, "user", None, time_, [make_block(0, "text", query, raw={})], raw=user_raw)]
        resp_text = html_to_text(rec["response_html"]) if rec["response_html"] else ""
        if resp_text:
            messages.append(make_message(
                1, "assistant", None, time_,
                [make_block(0, "text", resp_text, raw={"html": rec["response_html"]})],
                raw={**msg_raw, "html_source": rec["src"]},
            ))
        else:
            messages[0]["raw"]["shape_unclear"] = True  # no response body in the source
        yield make_conversation(
            platform=platform, native_id=_native_id(time_, query, verb), title=query[:200],
            project=None, started_at=time_, ended_at=time_, source_path=str(path),
            raw=conv_raw, messages=messages,
        )
    parse_stats["unknown_timezones"] = dict(_gt.parse_stats["unknown_timezones"])

from __future__ import annotations

from html.parser import HTMLParser

# Small, dependency-free HTML->markdown-ish text converter, scoped to
# exactly the tag vocabulary confirmed present across the real Gemini
# Takeout export (checked by regex-scanning every safeHtmlItem.html in the
# file before writing this, not assumed): h1-h5, p, ul/ol/li, strong, em,
# code, pre, a, blockquote, table/thead/tbody/tr/td/th, br, hr, img.
# No divs, spans, scripts, or styling attributes were present in the real
# data, so this deliberately does not try to handle arbitrary HTML.

_HEADERS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####"}


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.list_stack: list[str] = []  # 'ul' or 'ol'
        self.list_counters: list[int] = []
        self.in_pre = False
        self.link_href: str | None = None
        self.link_text: list[str] = []
        self.table_row: list[str] = []
        self.in_table_cell = False

    def _write(self, text: str) -> None:
        self.out.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HEADERS:
            self._write(f"\n\n{_HEADERS[tag]} ")
        elif tag == "p":
            self._write("\n\n")
        elif tag == "br":
            self._write("\n")
        elif tag == "hr":
            self._write("\n\n---\n\n")
        elif tag == "ul":
            self.list_stack.append("ul")
        elif tag == "ol":
            self.list_stack.append("ol")
            self.list_counters.append(0)
        elif tag == "li":
            depth = max(len(self.list_stack) - 1, 0)
            indent = "  " * depth
            kind = self.list_stack[-1] if self.list_stack else "ul"
            if kind == "ol":
                self.list_counters[-1] += 1
                self._write(f"\n{indent}{self.list_counters[-1]}. ")
            else:
                self._write(f"\n{indent}- ")
        elif tag == "strong":
            self._write("**")
        elif tag == "em":
            self._write("*")
        elif tag == "code":
            if not self.in_pre:
                self._write("`")
        elif tag == "pre":
            self.in_pre = True
            self._write("\n\n```\n")
        elif tag == "blockquote":
            self._write("\n> ")
        elif tag == "a":
            href = dict(attrs).get("href")
            self.link_href = href
            self.link_text = []
        elif tag == "img":
            src = dict(attrs).get("src", "")
            alt = dict(attrs).get("alt", "image")
            self._write(f"![{alt}]({src})")
        elif tag in ("td", "th"):
            self.in_table_cell = True
        elif tag == "tr":
            self.table_row = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul" or tag == "ol":
            if self.list_stack:
                self.list_stack.pop()
            if tag == "ol" and self.list_counters:
                self.list_counters.pop()
            if not self.list_stack:
                self._write("\n")
        elif tag == "strong":
            self._write("**")
        elif tag == "em":
            self._write("*")
        elif tag == "code":
            if not self.in_pre:
                self._write("`")
        elif tag == "pre":
            self.in_pre = False
            self._write("\n```\n\n")
        elif tag == "a":
            text = "".join(self.link_text).strip()
            if self.link_href:
                self._write(f"[{text}]({self.link_href})")
            else:
                self._write(text)
            self.link_href = None
            self.link_text = []
        elif tag in ("td", "th"):
            self.in_table_cell = False
        elif tag == "tr":
            if self.table_row:
                self._write("| " + " | ".join(self.table_row) + " |\n")

    def handle_data(self, data: str) -> None:
        if self.link_href is not None:
            self.link_text.append(data)
            return
        if self.in_table_cell:
            self.table_row.append(data.strip())
            return
        self._write(data)

    def result(self) -> str:
        text = "".join(self.out)
        # collapse runs of blank lines left by paragraph/header spacing
        lines = [ln.rstrip() for ln in text.split("\n")]
        collapsed: list[str] = []
        blank_run = 0
        for ln in lines:
            if ln == "":
                blank_run += 1
                if blank_run > 2:
                    continue
            else:
                blank_run = 0
            collapsed.append(ln)
        return "\n".join(collapsed).strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    conv = _Converter()
    try:
        conv.feed(html)
    except Exception:
        # Malformed fragment: fall back to a crude tag strip rather than
        # losing the content entirely.
        import re

        return re.sub(r"<[^>]+>", " ", html).strip()
    return conv.result()

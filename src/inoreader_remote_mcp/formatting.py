"""From Inoreader's payloads to what Claude reads.

Short and flat on purpose: every field sent costs context, and an article list of a hundred
items with full HTML summaries would fill a conversation on its own.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any

SUMMARY_LENGTH = 500

READING_LIST = "user/-/state/com.google/reading-list"
STARRED = "user/-/state/com.google/starred"
READ = "user/-/state/com.google/read"

# Inoreader's fixed name for the folder whose sub-folders are keyword-monitoring feeds.
DYNAMIC_FOLDERS = "Dynamic Folders"

_LABEL = re.compile(r"^user/[^/]+/label/(.+)$")
_STATE = re.compile(r"^user/[^/]+/state/")


class _TextExtractor(HTMLParser):
    # Their content is code or styling, never prose.
    SKIPPED = {"script", "style", "noscript", "template"}
    # Tags after which words must not run together.
    BREAKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIPPED:
            self.skipping += 1
        elif tag in self.BREAKS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIPPED and self.skipping:
            self.skipping -= 1
        elif tag in self.BREAKS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(unescape("".join(parser.parts)).split())


def truncate(text: str, length: int = SUMMARY_LENGTH) -> str:
    if len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "…"


def iso(seconds: float | int | None) -> str | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(float(seconds), UTC).isoformat().replace("+00:00", "Z")


def label_name(stream_id: str) -> str | None:
    """`user/1005921515/label/Tech` → `Tech`; `None` for anything that is not a label."""
    match = _LABEL.match(stream_id)
    return match.group(1) if match else None


def is_state(stream_id: str) -> bool:
    return bool(_STATE.match(stream_id))


def article(item: dict[str, Any]) -> dict[str, Any]:
    links = item.get("canonical") or item.get("alternate") or []
    summary = (item.get("summary") or {}).get("content") or ""
    crawl = item.get("crawlTimeMsec")
    return {
        "id": item.get("id"),
        "title": html_to_text(item.get("title") or ""),
        "url": links[0].get("href") if links else None,
        "feed": (item.get("origin") or {}).get("title"),
        "published": iso(item.get("published")),
        "crawlTimeMsec": int(crawl) if crawl else None,
        "summary": truncate(html_to_text(summary)),
    }


def articles_page(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "articles": [article(item) for item in payload.get("items", [])],
        # Absent on the last page; Claude passes it back as `continuation` for the next.
        "continuation": payload.get("continuation") or None,
    }


def feed_stream_id(url_or_id: str) -> str:
    """Accepts a feed URL as well as its stream id."""
    return url_or_id if url_or_id.startswith("feed/") else f"feed/{url_or_id}"

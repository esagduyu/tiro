"""Readwise JSON export adapter (Phase 4 M4.2, spec D7.5).

A Readwise export is a JSON document of "book"/"article" objects, each with a
`highlights[]` array. Articles and books are all reading material — both
import, no category filtering. Field access is lenient (Readwise's field names
have drifted across export eras): the top level may be a bare list, or a dict
wrapping the list under `results`/`books`/`articles`/`documents`; per item the
URL is `source_url` (falling back to `url`/`readable_url`), and per highlight
the quote is `text` (falling back to `quote`/`content`) with an optional
`note` and `highlighted_at`.

Items **without a resolvable URL are skipped** (a Tiro article needs a URL or
content — Readwise carries no article body, only highlights) with a logged
count; malformed objects are skipped, never raised.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from tiro.ingestion.importers.base import ImportHighlight, ImportItem

logger = logging.getLogger(__name__)

# Where the item list may live when the export is a dict rather than a bare
# list — tried in order.
_LIST_KEYS = ("results", "books", "articles", "documents", "items")


def _parse_iso(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _books(data) -> list:
    """Normalize the top-level shape to a list of item objects."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _highlights_from_obj(obj: dict) -> list[ImportHighlight]:
    out: list[ImportHighlight] = []
    raw = obj.get("highlights")
    if not isinstance(raw, list):
        return out
    for h in raw:
        if not isinstance(h, dict):
            continue
        quote = h.get("text") or h.get("quote") or h.get("content")
        if not isinstance(quote, str) or not quote.strip():
            continue
        note = h.get("note")
        out.append(
            ImportHighlight(
                quote=quote.strip(),
                note=note.strip() if isinstance(note, str) and note.strip() else None,
                created_at=_parse_iso(h.get("highlighted_at") or h.get("updated_at")),
            )
        )
    return out


def _item_from_obj(obj) -> ImportItem | None:
    if not isinstance(obj, dict):
        return None
    url = (obj.get("source_url") or obj.get("url") or obj.get("readable_url") or "").strip()
    title = (obj.get("title") or "").strip()
    if not url:
        logger.warning("Readwise item %r skipped: no source_url", title or "<untitled>")
        return None

    author = obj.get("author")
    return ImportItem(
        url=url,
        title=title or url,
        author=author.strip() if isinstance(author, str) and author.strip() else None,
        published_at=_parse_iso(obj.get("published_at") or obj.get("publishedAt")),
        saved_at=_parse_iso(obj.get("saved_at") or obj.get("last_highlight_at")),
        highlights=_highlights_from_obj(obj),
    )


def parse_export(path):
    """Yield one `ImportItem` per Readwise book/article that has a URL.
    Items without a URL are skipped with a logged count; a malformed document
    yields nothing (logged), never raises."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Readwise export %s unreadable/invalid JSON: %s", path, e)
        return

    for obj in _books(data):
        item = _item_from_obj(obj)
        if item is not None:
            yield item

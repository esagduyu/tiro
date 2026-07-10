"""Omnivore export-zip adapter (Phase 4 M4.2, spec D7.5).

An Omnivore export is a zip of `metadata_*.json` chunk arrays plus per-article
content files under `content/` (HTML in older exports, markdown in newer ones
— both handled) and, in the shutdown-era export format, per-article highlight
files under `highlights/{slug}.md`. Both content and highlight files are
resolved by slug (then id). Highlights come from two places, merged: any
`highlights` array carried inline in the metadata object, PLUS the article's
`highlights/{slug}.md` file if present — each highlight in that file is a `>`
blockquote (the quoted passage) optionally followed by a plain paragraph (the
note). Parsing is conservative: a blockquote run becomes one quote; anything
that isn't a recognizable blockquote yields no highlight, and any parsed quote
the importer can't re-anchor is counted in `highlights_skipped` (never
hand-placed). Everything is read IN-MEMORY — no member is ever extracted to
disk — and hostile member paths (absolute or `..`-traversal) are rejected up
front (zip-slip protection), so a malicious archive can neither escape a
directory nor be processed.
"""

import json
import logging
import posixpath
import re
import zipfile
from datetime import datetime

from tiro.ingestion.importers.base import ImportHighlight, ImportItem

logger = logging.getLogger(__name__)

# Skip any single member larger than this uncompressed (zip-bomb guard). A
# generous per-article ceiling; real article HTML/markdown is far smaller.
_MAX_MEMBER_BYTES = 25 * 1024 * 1024


def _is_safe_member(name: str) -> bool:
    """Reject absolute paths and `..` traversal (zip-slip). Even though we
    only ever read members in-memory, an unsafe name is never legitimate
    Omnivore content, so it is skipped outright."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    norm = posixpath.normpath(name)
    parts = norm.split("/")
    return not (norm.startswith("/") or ".." in parts)


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str | None:
    if info.file_size > _MAX_MEMBER_BYTES:
        logger.warning("Omnivore member %s skipped: too large (%d bytes)",
                       info.filename, info.file_size)
        return None
    try:
        return zf.read(info).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("Omnivore member %s unreadable: %s", info.filename, e)
        return None


def _parse_iso(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _labels_to_tags(labels) -> list[str]:
    tags = []
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, str) and label.strip():
                tags.append(label.strip())
            elif isinstance(label, dict):
                name = label.get("name")
                if isinstance(name, str) and name.strip():
                    tags.append(name.strip())
    return tags


def _highlights_from_obj(obj: dict) -> list[ImportHighlight]:
    """Inline highlights carried in the metadata object. Lenient about the
    field names Omnivore has used across eras (`quote`/`text`/`content` for
    the quote, `annotation`/`note` for the note)."""
    out = []
    raw = obj.get("highlights")
    if not isinstance(raw, list):
        return out
    for h in raw:
        if not isinstance(h, dict):
            continue
        quote = h.get("quote") or h.get("text") or h.get("content")
        if not isinstance(quote, str) or not quote.strip():
            continue
        note = h.get("annotation") or h.get("note")
        out.append(
            ImportHighlight(
                quote=quote.strip(),
                note=note.strip() if isinstance(note, str) and note.strip() else None,
                created_at=_parse_iso(h.get("highlightedAt") or h.get("updatedAt")),
            )
        )
    return out


def _parse_highlights_md(text: str) -> list[ImportHighlight]:
    """Parse an Omnivore `highlights/{slug}.md` export file into highlights.

    Conservative, blockquote-shaped: every contiguous run of `>` lines is one
    quote; the next non-blank paragraph that isn't itself a blockquote, heading,
    or horizontal rule is that quote's note. Structural lines (the title /
    `#### Highlights` headings, `---` rules) are ignored, and Omnivore's trailing
    `[⤴️](url)` link marker on a quote line is stripped. Anything that isn't a
    recognizable blockquote yields no highlight — an unparseable file simply
    contributes nothing, and any parsed quote that later can't be re-anchored is
    counted in `highlights_skipped` by the importer (never hand-placed)."""
    out: list[ImportHighlight] = []
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].lstrip().startswith(">"):
            i += 1
            continue
        quote_parts = []
        while i < n and lines[i].lstrip().startswith(">"):
            frag = lines[i].lstrip()[1:]
            if frag.startswith(" "):
                frag = frag[1:]
            quote_parts.append(frag)
            i += 1
        quote = "\n".join(quote_parts).strip()
        # Drop a trailing Omnivore highlight-link marker, e.g. " [⤴️](url)".
        quote = re.sub(r"\s*\[[^\]]*\]\([^)]*\)\s*$", "", quote).strip()
        # Optional note: the next non-blank paragraph that isn't a blockquote,
        # heading, or horizontal rule.
        while i < n and not lines[i].strip():
            i += 1
        note_parts = []
        while i < n:
            s = lines[i].strip()
            if not s or s.startswith((">", "#")) or s in ("---", "***", "___"):
                break
            note_parts.append(lines[i].rstrip())
            i += 1
        note = "\n".join(note_parts).strip() or None
        if quote:
            out.append(ImportHighlight(quote=quote, note=note))
    return out


def _index_members(zf: zipfile.ZipFile):
    """Return `(metadata_infos, content_by_stem, highlights_by_stem)` over the
    safe members. `content_by_stem[stem] = (ext, ZipInfo)`;
    `highlights_by_stem[stem] = ZipInfo` for `highlights/{slug}.md` files."""
    metadata_infos = []
    content_by_stem: dict[str, tuple[str, zipfile.ZipInfo]] = {}
    highlights_by_stem: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        name = info.filename
        if not _is_safe_member(name):
            logger.warning("Omnivore member %s skipped: unsafe path", name)
            continue
        low = name.lower()
        base = posixpath.basename(name)
        stem, ext = posixpath.splitext(base)
        ext = ext.lower()
        if base.lower().startswith("metadata") and low.endswith(".json"):
            metadata_infos.append(info)
        elif ("highlights/" in low or low.startswith("highlights")) and ext == ".md":
            highlights_by_stem[stem] = info
        elif ("content/" in low or low.startswith("content")) and ext in (".md", ".html", ".htm"):
            content_by_stem[stem] = (ext, info)
    return metadata_infos, content_by_stem, highlights_by_stem


def _item_from_obj(zf, obj, content_by_stem, highlights_by_stem) -> ImportItem | None:
    if not isinstance(obj, dict):
        return None
    url = (obj.get("url") or "").strip()
    title = (obj.get("title") or "").strip()
    slug = (obj.get("slug") or "").strip()
    if not url and not title:
        return None
    id_key = str(obj.get("id") or "")

    content_md = None
    content_html = None
    cf = content_by_stem.get(slug) or content_by_stem.get(id_key)
    if cf:
        ext, info = cf
        text = _read_member(zf, info)
        if text is not None:
            if ext == ".md":
                content_md = text
            else:
                content_html = text

    # Highlights: inline (metadata `highlights` array) + the per-article
    # highlights/{slug}.md export file, if present. Both keyed by slug (then id).
    highlights = _highlights_from_obj(obj)
    hf = highlights_by_stem.get(slug) or highlights_by_stem.get(id_key)
    if hf:
        htext = _read_member(zf, hf)
        if htext:
            highlights = highlights + _parse_highlights_md(htext)

    return ImportItem(
        url=url or None,
        title=title or url,
        author=(obj.get("author") or None),
        published_at=_parse_iso(obj.get("publishedAt")),
        saved_at=_parse_iso(obj.get("savedAt")),
        tags=_labels_to_tags(obj.get("labels")),
        content_md=content_md,
        content_html=content_html,
        highlights=highlights,
    )


def parse_export(path):
    """Yield one `ImportItem` per article across every `metadata_*.json` chunk.
    Malformed chunks/objects are skipped with a logged warning."""
    with zipfile.ZipFile(path) as zf:
        metadata_infos, content_by_stem, highlights_by_stem = _index_members(zf)
        for info in sorted(metadata_infos, key=lambda i: i.filename):
            raw = _read_member(zf, info)
            if raw is None:
                continue
            try:
                data = json.loads(raw)
            except Exception as e:
                logger.warning("Omnivore metadata %s skipped (bad JSON): %s", info.filename, e)
                continue
            if not isinstance(data, list):
                logger.warning("Omnivore metadata %s skipped: not a list", info.filename)
                continue
            for obj in data:
                item = _item_from_obj(zf, obj, content_by_stem, highlights_by_stem)
                if item is not None:
                    yield item

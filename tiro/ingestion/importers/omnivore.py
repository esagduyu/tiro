"""Omnivore export-zip adapter (Phase 4 M4.2, spec D7.5).

An Omnivore export is a zip of `metadata_*.json` chunk arrays plus per-article
content files under `content/` (HTML in older exports, markdown in newer ones
— both handled) and optional per-article highlight files under `highlights/`.
Content files are resolved by slug (then id). Everything is read IN-MEMORY —
no member is ever extracted to disk — and hostile member paths (absolute or
`..`-traversal) are rejected up front (zip-slip protection), so a malicious
archive can neither escape a directory nor be processed.
"""

import json
import logging
import posixpath
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


def _index_members(zf: zipfile.ZipFile):
    """Return `(metadata_infos, content_by_stem)` over the safe members.
    `content_by_stem[stem] = (ext, ZipInfo)`."""
    metadata_infos = []
    content_by_stem: dict[str, tuple[str, zipfile.ZipInfo]] = {}
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
        elif ("content/" in low or low.startswith("content")) and ext in (".md", ".html", ".htm"):
            content_by_stem[stem] = (ext, info)
    return metadata_infos, content_by_stem


def _item_from_obj(zf, obj, content_by_stem) -> ImportItem | None:
    if not isinstance(obj, dict):
        return None
    url = (obj.get("url") or "").strip()
    title = (obj.get("title") or "").strip()
    slug = (obj.get("slug") or "").strip()
    if not url and not title:
        return None

    content_md = None
    content_html = None
    cf = content_by_stem.get(slug) or content_by_stem.get(str(obj.get("id") or ""))
    if cf:
        ext, info = cf
        text = _read_member(zf, info)
        if text is not None:
            if ext == ".md":
                content_md = text
            else:
                content_html = text

    return ImportItem(
        url=url or None,
        title=title or url,
        author=(obj.get("author") or None),
        published_at=_parse_iso(obj.get("publishedAt")),
        saved_at=_parse_iso(obj.get("savedAt")),
        tags=_labels_to_tags(obj.get("labels")),
        content_md=content_md,
        content_html=content_html,
        highlights=_highlights_from_obj(obj),
    )


def parse_export(path):
    """Yield one `ImportItem` per article across every `metadata_*.json` chunk.
    Malformed chunks/objects are skipped with a logged warning."""
    with zipfile.ZipFile(path) as zf:
        metadata_infos, content_by_stem = _index_members(zf)
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
                item = _item_from_obj(zf, obj, content_by_stem)
                if item is not None:
                    yield item

"""Instapaper CSV export adapter (Phase 4 M4.2, spec D7.5).

Instapaper's CSV export has columns `URL, Title, Selection, Folder,
Timestamp` (Timestamp is Unix epoch seconds). Header-tolerant (names matched
case-insensitively, surrounding whitespace stripped). `Selection` becomes one
highlight when non-empty; `Folder` becomes a lowercase tag. Rows without a URL
are skipped (a Tiro article needs a URL or content); a malformed timestamp
degrades to `None`, never dropping the row.
"""

import csv
import logging
from datetime import datetime
from pathlib import Path

from tiro.ingestion.importers.base import ImportHighlight, ImportItem

logger = logging.getLogger(__name__)

# Guard against a pathological single-cell export blowing up memory.
_MAX_FIELD_BYTES = 5 * 1024 * 1024


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw))
    except (ValueError, OverflowError, OSError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_export(path):
    """Yield one `ImportItem` per valid CSV row. Malformed rows are skipped
    with a logged warning (lenient per spec D7.5)."""
    path = Path(path)
    # Some large Selection cells can exceed the default 128 KB csv field cap.
    try:
        csv.field_size_limit(_MAX_FIELD_BYTES)
    except (OverflowError, ValueError):
        pass

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return
        # Map raw header -> normalized key (lowercased, trimmed).
        field_map = {name: (name or "").strip().lower() for name in reader.fieldnames}
        for rownum, raw_row in enumerate(reader, start=2):
            try:
                row = {
                    field_map.get(k, k): (v or "")
                    for k, v in raw_row.items()
                    if k is not None
                }
            except Exception as e:
                logger.warning("Instapaper row %d skipped (unparseable): %s", rownum, e)
                continue

            url = row.get("url", "").strip()
            title = row.get("title", "").strip()
            if not url:
                logger.warning("Instapaper row %d skipped: no URL", rownum)
                continue

            highlights = []
            selection = row.get("selection", "").strip()
            if selection:
                highlights.append(ImportHighlight(quote=selection))

            tags = []
            folder = row.get("folder", "").strip()
            if folder:
                tags.append(folder.lower())

            yield ImportItem(
                url=url,
                title=title or url,
                saved_at=_parse_timestamp(row.get("timestamp")),
                tags=tags,
                highlights=highlights,
            )

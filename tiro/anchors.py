"""Pure text-anchor functions for Phase 2 highlights/notes (M2.1).

No DB, no config, no I/O — string functions only, so they stay trivially
unit-testable and reusable by the sidecar store (T2) and CRUD API (T3).

Three-tier anchor model: an anchor is `{quote, prefix, suffix,
position_start, position_end}` (plus a caller-attached `content_hash` of the
article's markdown at anchor-creation time, stored alongside but computed
separately via `content_hash()`). `reconcile_anchor()` re-locates an anchor
against the CURRENT markdown (which may have shifted or changed since the
anchor was made) and reports one of four statuses:

- "exact": quote still sits at the stored offsets.
- "shifted": quote found elsewhere (offsets moved, e.g. an edit upstream).
- "hash_mismatch": quote not findable and the document has changed
  (stored content_hash != content_hash(current markdown)).
- "missing": quote not findable but the document is byte-identical to when
  the anchor was made — degenerate/shouldn't happen, means the anchor data
  itself is corrupt (not a document-edit case).
"""

import hashlib


def content_hash(markdown: str) -> str:
    """sha256 hex digest of the article's markdown, used to distinguish an
    unchanged document (corrupt anchor -> "missing") from a changed one
    (quote genuinely gone -> "hash_mismatch")."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def make_anchor(markdown: str, start: int, end: int, context_chars: int = 32) -> dict:
    """Build an anchor dict for the `markdown[start:end]` span.

    Raises ValueError on an empty, inverted, or out-of-bounds range."""
    if start < 0 or end > len(markdown):
        raise ValueError(
            f"anchor range [{start}, {end}) is out of bounds for text of length {len(markdown)}"
        )
    if start >= end:
        raise ValueError(f"anchor range [{start}, {end}) is empty or inverted")

    prefix_start = max(0, start - context_chars)
    return {
        "quote": markdown[start:end],
        "prefix": markdown[prefix_start:start],
        "suffix": markdown[end : end + context_chars],
        "position_start": start,
        "position_end": end,
    }


def _find_all(text: str, needle: str) -> list[int]:
    """All (possibly overlapping) start indices of `needle` in `text`."""
    if not needle:
        return []
    positions = []
    idx = text.find(needle)
    while idx != -1:
        positions.append(idx)
        idx = text.find(needle, idx + 1)
    return positions


def _hash_status(markdown: str, stored_hash: str | None) -> dict:
    """Quote not findable anywhere: distinguish a genuinely changed document
    (hash_mismatch) from a byte-identical one (missing -> corrupt anchor)."""
    if stored_hash is not None and stored_hash != content_hash(markdown):
        return {"status": "hash_mismatch", "position_start": None, "position_end": None}
    return {"status": "missing", "position_start": None, "position_end": None}


def reconcile_anchor(markdown: str, anchor: dict) -> dict:
    """Re-locate `anchor` (quote/prefix/suffix/position_start/position_end/
    content_hash) against the current `markdown`. See module docstring for
    the status semantics."""
    quote = anchor.get("quote") or ""
    prefix = anchor.get("prefix") or ""
    suffix = anchor.get("suffix") or ""
    stored_start = anchor.get("position_start")
    stored_end = anchor.get("position_end")
    stored_hash = anchor.get("content_hash")

    if not quote:
        # Degenerate anchor (no quote to search for) — corrupt, not an edit.
        return _hash_status(markdown, stored_hash)

    # 1. Exact-position check first (cheap): still valid indices, quote
    # unchanged at that exact span.
    if (
        stored_start is not None
        and stored_end is not None
        and 0 <= stored_start <= stored_end <= len(markdown)
        and markdown[stored_start:stored_end] == quote
    ):
        return {"status": "exact", "position_start": stored_start, "position_end": stored_end}

    # 2. Quote moved (or duplicated) — find every occurrence in the document.
    occurrences = _find_all(markdown, quote)
    if not occurrences:
        return _hash_status(markdown, stored_hash)

    # 3. Disambiguate via prefix/suffix context (using the stored context's
    # own length, since it may already be edge-truncated from make_anchor).
    context_matches = []
    for occ_start in occurrences:
        occ_end = occ_start + len(quote)
        actual_prefix = markdown[max(0, occ_start - len(prefix)) : occ_start]
        actual_suffix = markdown[occ_end : occ_end + len(suffix)]
        if actual_prefix == prefix and actual_suffix == suffix:
            context_matches.append(occ_start)

    # Prefer context-disambiguated candidates; fall back to all occurrences
    # when none (or more than one, i.e. equal context) match.
    candidates = context_matches if context_matches else occurrences
    if len(candidates) == 1 or stored_start is None:
        best_start = candidates[0]
    else:
        best_start = min(candidates, key=lambda s: abs(s - stored_start))

    return {
        "status": "shifted",
        "position_start": best_start,
        "position_end": best_start + len(quote),
    }

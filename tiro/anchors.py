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
- "hash_mismatch": quote not findable anywhere in the document. This
  includes the case where there is no stored `content_hash` at all: a
  missing hash means we have no provenance to fall back on, so we CANNOT
  positively confirm the document is unchanged — treat "can't verify" the
  same as "verified changed" rather than assuming innocence. (Deliberate
  choice, see `reconcile_anchor` docstring.)
- "missing": quote not findable, but the document is byte-identical to the
  stored `content_hash` from when the anchor was made — degenerate/
  shouldn't happen, means the anchor data itself is corrupt (not a
  document-edit case). Only reachable when a `content_hash` IS stored and
  it matches.

Context-aware search has priority over the stored offsets (W3C
TextQuoteSelector approach): `reconcile_anchor` always searches the current
document for every occurrence of the quote and scores each by how much of
its stored prefix/suffix context still surrounds it, rather than trusting
the stored offsets first. This matters when content is edited such that an
unrelated identical string ends up sitting at the anchor's old offsets while
the TRUE occurrence (identifiable by its context) has moved elsewhere — the
stored-offsets-first approach would wrongly report "exact" at the stale
offsets; scoring by context first correctly reports "shifted" to the real
location instead. Offsets are only reported as "exact" when the winning,
context-scored occurrence happens to sit at the stored position.
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
    """Quote not findable anywhere in the current document.

    Deliberate decision: a stored `content_hash` of `None` (unknown
    provenance — e.g. legacy data written before hashing existed) can NOT be
    used to confirm the document is unchanged, so it is treated with
    "hash_mismatch" semantics rather than assumed innocent ("missing"). Only
    a hash that is present AND equal to the current document's hash proves
    the document is byte-identical to anchor-creation time, which is the
    one case where "missing" (corrupt anchor, not a document edit) applies.
    """
    if stored_hash is None or stored_hash != content_hash(markdown):
        return {"status": "hash_mismatch", "position_start": None, "position_end": None}
    return {"status": "missing", "position_start": None, "position_end": None}


def _context_score(markdown: str, occ_start: int, occ_end: int, prefix: str, suffix: str) -> int:
    """Score one occurrence by how much of its stored context still
    surrounds it: 2 = full prefix+suffix match, 1 = partial (prefix-only or
    suffix-only), 0 = bare quote match with no surrounding context match."""
    actual_prefix = markdown[max(0, occ_start - len(prefix)) : occ_start]
    actual_suffix = markdown[occ_end : occ_end + len(suffix)]
    prefix_match = actual_prefix == prefix
    suffix_match = actual_suffix == suffix
    if prefix_match and suffix_match:
        return 2
    if prefix_match or suffix_match:
        return 1
    return 0


def reconcile_anchor(markdown: str, anchor: dict) -> dict:
    """Re-locate `anchor` (quote/prefix/suffix/position_start/position_end/
    content_hash) against the current `markdown`. See module docstring for
    the status semantics.

    Context search always has priority over the stored offsets: every
    occurrence of the quote in the current document is found and scored by
    how much of the stored prefix/suffix context still surrounds it (full
    match beats partial beats bare quote); ties are broken by proximity to
    the stored `position_start`. Only once a winning occurrence is chosen do
    we compare its position to the stored offsets to decide "exact" vs
    "shifted" — so a stale-but-coincidentally-identical string sitting at
    the old offsets never wins over the context-matched true occurrence.
    """
    quote = anchor.get("quote") or ""
    prefix = anchor.get("prefix") or ""
    suffix = anchor.get("suffix") or ""
    stored_start = anchor.get("position_start")
    stored_end = anchor.get("position_end")
    stored_hash = anchor.get("content_hash")

    if not quote:
        # Degenerate anchor (no quote to search for) — corrupt, not an edit.
        return _hash_status(markdown, stored_hash)

    # 1. Find every occurrence of the quote in the current document.
    occurrences = _find_all(markdown, quote)
    if not occurrences:
        # 4. No candidates at all — fall back to the hash comparison.
        return _hash_status(markdown, stored_hash)

    # 2. Score each occurrence by context match quality, keep only the best.
    scores = {
        occ_start: _context_score(markdown, occ_start, occ_start + len(quote), prefix, suffix)
        for occ_start in occurrences
    }
    best_score = max(scores.values())
    top_candidates = [occ_start for occ_start in occurrences if scores[occ_start] == best_score]

    if len(top_candidates) == 1 or stored_start is None:
        best_start = top_candidates[0]
    else:
        best_start = min(top_candidates, key=lambda s: abs(s - stored_start))
    best_end = best_start + len(quote)

    # 3. Winner at the stored position -> exact (stored offsets); else shifted.
    if stored_start is not None and best_start == stored_start:
        return {
            "status": "exact",
            "position_start": stored_start,
            "position_end": stored_end if stored_end is not None else best_end,
        }

    return {"status": "shifted", "position_start": best_start, "position_end": best_end}

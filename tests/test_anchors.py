"""Pure anchor functions (Phase 2 M2.1): content_hash, make_anchor,
reconcile_anchor. No DB/config/I-O involved — these are plain string tests."""

import hashlib

import pytest

from tiro.anchors import content_hash, make_anchor, reconcile_anchor

# --- content_hash -----------------------------------------------------------


def test_content_hash_is_sha256_hexdigest():
    text = "hello world"
    assert content_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_content_hash_changes_with_content():
    assert content_hash("a") != content_hash("b")


def test_content_hash_stable_for_same_content():
    assert content_hash("same text") == content_hash("same text")


# --- make_anchor -------------------------------------------------------------


def test_make_anchor_extracts_quote_prefix_suffix():
    text = "The quick brown fox jumps over the lazy dog."
    anchor = make_anchor(text, 4, 9, context_chars=8)
    assert anchor["quote"] == "quick"
    assert anchor["prefix"] == "The "
    assert anchor["suffix"] == " brown f"
    assert anchor["position_start"] == 4
    assert anchor["position_end"] == 9


def test_make_anchor_truncates_prefix_at_start_of_text():
    text = "Hello world"
    anchor = make_anchor(text, 0, 5, context_chars=32)
    assert anchor["quote"] == "Hello"
    assert anchor["prefix"] == ""
    assert anchor["suffix"] == " world"


def test_make_anchor_truncates_suffix_at_end_of_text():
    text = "Hello world"
    anchor = make_anchor(text, 6, 11, context_chars=32)
    assert anchor["quote"] == "world"
    assert anchor["suffix"] == ""
    assert anchor["prefix"] == "Hello "


def test_make_anchor_context_chars_truncation_both_edges_short_text():
    text = "Hi"
    anchor = make_anchor(text, 0, 2, context_chars=32)
    assert anchor["quote"] == "Hi"
    assert anchor["prefix"] == ""
    assert anchor["suffix"] == ""


def test_make_anchor_rejects_empty_range():
    with pytest.raises(ValueError):
        make_anchor("hello", 3, 3)


def test_make_anchor_rejects_inverted_range():
    with pytest.raises(ValueError):
        make_anchor("hello", 4, 1)


def test_make_anchor_rejects_negative_start():
    with pytest.raises(ValueError):
        make_anchor("hello", -1, 3)


def test_make_anchor_rejects_negative_end():
    with pytest.raises(ValueError):
        make_anchor("hello", 0, -1)


def test_make_anchor_rejects_end_past_text_length():
    with pytest.raises(ValueError):
        make_anchor("hello", 0, 999)


def test_make_anchor_default_context_chars_is_32():
    text = "x" * 100 + "TARGET" + "y" * 100
    start = text.index("TARGET")
    end = start + len("TARGET")
    anchor = make_anchor(text, start, end)  # no context_chars passed
    assert anchor["prefix"] == "x" * 32
    assert anchor["suffix"] == "y" * 32


def test_make_anchor_unicode_emoji_and_nbsp():
    text = "prefix text \U0001f600 more words"
    start = text.index("\U0001f600")
    end = start + 1
    anchor = make_anchor(text, start, end, context_chars=6)
    assert anchor["quote"] == "\U0001f600"
    assert text[anchor["position_start"] : anchor["position_end"]] == "\U0001f600"


def test_make_anchor_unicode_nbsp_in_context():
    text = "a\u00a0quick brown fox"  # non-breaking space right before the quote
    start = text.index("quick")
    end = start + len("quick")
    anchor = make_anchor(text, start, end, context_chars=4)
    assert anchor["quote"] == "quick"
    assert anchor["prefix"] == "a\u00a0"
    assert anchor["suffix"] == " bro"


def test_reconcile_unicode_nbsp_survives_shift():
    original = "a\u00a0quick brown fox jumps"
    start = original.index("quick")
    end = start + len("quick")
    anchor = make_anchor(original, start, end, context_chars=4)
    anchor["content_hash"] = content_hash(original)

    edited = "intro. " + original
    result = reconcile_anchor(edited, anchor)
    assert result["status"] == "shifted"
    new_start, new_end = result["position_start"], result["position_end"]
    assert edited[new_start:new_end] == "quick"


# --- reconcile_anchor ---------------------------------------------------------


def test_reconcile_exact_when_quote_unchanged_at_stored_offsets():
    text = "The quick brown fox jumps over the lazy dog."
    anchor = make_anchor(text, 4, 9, context_chars=8)
    anchor["content_hash"] = content_hash(text)
    result = reconcile_anchor(text, anchor)
    assert result == {"status": "exact", "position_start": 4, "position_end": 9}


def test_reconcile_shifted_when_text_inserted_before_quote():
    original = "The quick brown fox jumps over the lazy dog."
    anchor = make_anchor(original, 4, 9, context_chars=8)
    anchor["content_hash"] = content_hash(original)

    edited = "Once upon a time. " + original
    result = reconcile_anchor(edited, anchor)
    assert result["status"] == "shifted"
    new_start, new_end = result["position_start"], result["position_end"]
    assert edited[new_start:new_end] == "quick"
    assert new_start == 4 + len("Once upon a time. ")


def test_reconcile_hash_mismatch_when_quote_removed_and_doc_changed():
    original = "The quick brown fox jumps over the lazy dog."
    anchor = make_anchor(original, 4, 9, context_chars=8)
    anchor["content_hash"] = content_hash(original)

    edited = "The slow brown fox jumps over the lazy dog."  # "quick" replaced
    result = reconcile_anchor(edited, anchor)
    assert result == {"status": "hash_mismatch", "position_start": None, "position_end": None}


def test_reconcile_missing_when_quote_unfindable_but_hash_matches():
    """Degenerate/corrupt anchor: the document is byte-identical to the hash
    stamped at anchor-creation time, yet the quote text itself doesn't
    appear in it (anchor data corrupted, not a document edit)."""
    text = "The quick brown fox jumps over the lazy dog."
    anchor = {
        "quote": "this text was never in the document",
        "prefix": "",
        "suffix": "",
        "position_start": 0,
        "position_end": 10,
        "content_hash": content_hash(text),
    }
    result = reconcile_anchor(text, anchor)
    assert result == {"status": "missing", "position_start": None, "position_end": None}


def test_reconcile_no_stored_hash_and_quote_unfindable_is_hash_mismatch():
    """No content_hash on the anchor at all (e.g. legacy data) — with no
    provenance to compare against we can't POSITIVELY confirm the document
    is unchanged, so this is treated as hash_mismatch (can't-verify ==
    treated-as-changed), NOT "missing". (Finding 1: this is a deliberate
    semantics change — a None hash used to default to "missing"; that
    silently assumed innocence when it should assume nothing. "missing" is
    now reserved for the case where a hash IS stored and it matches.)"""
    text = "abc def ghi"
    anchor = {
        "quote": "zzz not present zzz",
        "prefix": "",
        "suffix": "",
        "position_start": 0,
        "position_end": 3,
        "content_hash": None,
    }
    result = reconcile_anchor(text, anchor)
    assert result["status"] == "hash_mismatch"


# --- duplicate-quote disambiguation ---------------------------------------


def test_reconcile_disambiguates_duplicate_quotes_via_prefix_suffix_context():
    text = "apple banana apple cherry apple date"
    # Anchor the SECOND "apple" (surrounded by "banana " / " cherry").
    start = text.index("apple", text.index("banana"))
    anchor = make_anchor(text, start, start + len("apple"), context_chars=10)
    anchor["content_hash"] = content_hash(text)

    # Shift everything by inserting text at the very front so stored offsets
    # point at the wrong "apple" occurrence — context must disambiguate.
    edited = "PREFACE. " + text
    result = reconcile_anchor(edited, anchor)
    assert result["status"] == "shifted"
    new_start, new_end = result["position_start"], result["position_end"]
    assert edited[new_start:new_end] == "apple"
    # Confirm it picked the contextually-correct occurrence (the one after
    # "banana "), not just the first "apple" in the document.
    assert edited[max(0, new_start - 7) : new_start] == "banana "


def test_reconcile_duplicate_quotes_equal_context_resolved_by_proximity():
    """Two occurrences with equal (empty) disambiguating context — pick the
    one closest to the stored position."""
    text = "xx MATCH xx MATCH xx"
    first = text.index("MATCH")
    second = text.index("MATCH", first + 1)
    # Use zero context so both occurrences have identical (non-empty but
    # equal) surrounding context and can't be told apart by content alone.
    anchor = make_anchor(text, first, first + len("MATCH"), context_chars=0)
    anchor["content_hash"] = content_hash(text)
    # Simulate an edit that doesn't change the quote's own position — stored
    # position still points at `first`, so proximity should keep `first`.
    result = reconcile_anchor(text, anchor)
    assert result["status"] == "exact"  # unchanged doc: exact-position wins first

    # Now force the "shifted" path: corrupt the stored position off by one
    # so the exact-position check fails, but leave it much closer to
    # `first` than to `second` — proximity must pick `first`.
    anchor["position_start"] = first + 1
    anchor["position_end"] = first + 1 + len("MATCH")
    result = reconcile_anchor(text, anchor)
    assert result["status"] == "shifted"
    assert result["position_start"] == first

    # And closer to `second` instead.
    anchor["position_start"] = second - 1
    anchor["position_end"] = second - 1 + len("MATCH")
    result = reconcile_anchor(text, anchor)
    assert result["status"] == "shifted"
    assert result["position_start"] == second


# --- context-priority ordering (Finding 1) -----------------------------------


def test_reconcile_context_match_wins_over_stale_offset_with_unrelated_identical_string():
    """The reviewer's failure case: an unrelated occurrence of the quote text
    sits at the anchor's stale stored offset, while the TRUE occurrence
    (identifiable by its prefix/suffix context) has moved elsewhere. Context
    search must have priority over the stored offsets — the correct result
    is "shifted" to the context-matched location, NOT "exact" at the stale
    offset (which would be wrong: that occurrence has no relation to the
    original highlight)."""
    quote = "APPLE"
    stored_prefix = "context. "
    stored_suffix = " fruit"

    # An unrelated "APPLE" with mismatched surrounding text — this is what
    # now occupies the anchor's old stored offset.
    fake_segment = "xxxx" + quote + "yyyy"
    # The real occurrence: same prefix/suffix context the anchor recorded.
    true_segment = stored_prefix + quote + stored_suffix

    edited = fake_segment + "----padding-string-here----" + true_segment
    fake_start = edited.index(quote)
    true_start = edited.index(quote, fake_start + 1)
    assert fake_start != true_start  # sanity: two distinct occurrences

    anchor = {
        "quote": quote,
        "prefix": stored_prefix,
        "suffix": stored_suffix,
        # Stale: pretend the highlight used to sit at the fake occurrence.
        "position_start": fake_start,
        "position_end": fake_start + len(quote),
        "content_hash": content_hash(edited),
    }
    result = reconcile_anchor(edited, anchor)
    assert result["status"] == "shifted"
    assert result["position_start"] == true_start
    assert result["position_end"] == true_start + len(quote)


def test_reconcile_position_fallback_is_exact_when_no_context_matches_anywhere():
    """No occurrence's context matches at all (the anchor's stored
    prefix/suffix don't appear anywhere in the document) — every candidate
    ties at the lowest score, so the proximity tiebreak decides. When the
    quote is genuinely unchanged at the stored offset, proximity correctly
    recovers "exact" even though no context corroborated it (spec note:
    this ordering yields "exact" in the pure position-fallback case too)."""
    text = "AAA TOKEN BBB TOKEN CCC"
    first = text.index("TOKEN")
    anchor = {
        "quote": "TOKEN",
        "prefix": "NOPE-NOT-REAL",
        "suffix": "ALSO-NOT-REAL",
        "position_start": first,
        "position_end": first + len("TOKEN"),
        "content_hash": content_hash(text),
    }
    result = reconcile_anchor(text, anchor)
    assert result == {
        "status": "exact",
        "position_start": first,
        "position_end": first + len("TOKEN"),
    }


# --- edge positions ---------------------------------------------------------


def test_reconcile_quote_at_position_zero():
    text = "Beginning of the document goes here."
    anchor = make_anchor(text, 0, 9, context_chars=8)
    anchor["content_hash"] = content_hash(text)
    result = reconcile_anchor(text, anchor)
    assert result == {"status": "exact", "position_start": 0, "position_end": 9}


def test_reconcile_quote_at_eof():
    text = "This document ends with THEEND"
    start = text.index("THEEND")
    anchor = make_anchor(text, start, len(text), context_chars=8)
    anchor["content_hash"] = content_hash(text)
    result = reconcile_anchor(text, anchor)
    assert result == {"status": "exact", "position_start": start, "position_end": len(text)}


def test_reconcile_unicode_emoji_and_nbsp_survives_shift():
    original = "prefix text \U0001f600 more words"
    start = original.index("\U0001f600")
    anchor = make_anchor(original, start, start + 1, context_chars=6)
    anchor["content_hash"] = content_hash(original)

    edited = "  lead-in " + original
    result = reconcile_anchor(edited, anchor)
    assert result["status"] == "shifted"
    new_start, new_end = result["position_start"], result["position_end"]
    assert edited[new_start:new_end] == "\U0001f600"

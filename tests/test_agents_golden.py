"""Golden behavior locks for the four migrated agents (spec §4).

Each test pins the EXACT llm_call transcript (tier, purpose, max_tokens,
prompt BYTES) plus the writeback side effects. Written against the
pre-runtime code first (must pass on the old orchestration), kept green
across the refactor. Recorders monkeypatch BOTH seams: the module-bound
name in the legacy module (pre-refactor) and tiro.llm.llm_call (which
context.py reads via module attribute, post-refactor).
"""

import json

import pytest

from tiro.llm import LLMResult


class _Recorder:
    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, config, tier, prompt, *, purpose,
                 max_tokens=1024, system=None):
        self.calls.append({"tier": tier, "prompt": prompt,
                           "purpose": purpose, "max_tokens": max_tokens})
        return LLMResult(text=self.responses.pop(0), provider="fake",
                         model="golden", tokens_in=10, tokens_out=20)


@pytest.fixture
def record_llm(monkeypatch):
    """Patch every llm_call seam with a shared recorder factory."""

    def _install(*responses):
        rec = _Recorder(*responses)
        # pre-refactor bound-name seams (harmless no-ops once refactored,
        # guarded because the attribute disappears after each task). These
        # go FIRST, before the tiro.llm.llm_call patch below: a target
        # module here may not have been imported by anything yet in this
        # test session, and monkeypatch's string-target resolution imports
        # it on first use — if tiro.llm.llm_call were already patched to
        # `rec` at that moment, the module's own `from tiro.llm import
        # llm_call` would bind directly to `rec` as a normal import side
        # effect, and monkeypatch would then capture *that* as the
        # "original" value to restore on teardown, permanently leaking the
        # recorder into every later test that imports the same module.
        for target in (
            "tiro.ingestion.extractors.llm_call",
            "tiro.intelligence.preferences.llm_call",
            "tiro.intelligence.digest.llm_call",
            "tiro.intelligence.analysis.llm_call",
        ):
            try:
                monkeypatch.setattr(target, rec)
            except AttributeError:
                pass
        # post-refactor seam (context.py: module-attribute access) — last.
        monkeypatch.setattr("tiro.llm.llm_call", rec)
        return rec

    return _install


# --- MetadataExtractor (Task 6) -------------------------------------------

GOLDEN_TITLE = "The Golden Article"
GOLDEN_BODY = ("Sentence about testing pipelines. " * 500)  # > 12000 chars
GOLDEN_EXTRACT_RESPONSE = json.dumps({
    "tags": ["AI ", "Testing", "t3", "t4", "t5", "t6", "t7", "t8", "t9"],
    "entities": [
        {"name": " Anthropic ", "type": " Company "},
        {"bogus": "no name key"},
        "not-a-dict",
    ],
    "summary": "A golden summary.",
})


def test_metadata_extractor_transcript_golden(initialized_library, record_llm):
    from tiro.ingestion.extractors import EXTRACT_CONTENT_CHARS, extract_metadata
    from tiro.intelligence.prompts import extract_metadata_prompt

    rec = record_llm(GOLDEN_EXTRACT_RESPONSE)
    out = extract_metadata(GOLDEN_TITLE, GOLDEN_BODY, initialized_library)

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["tier"] == "light"
    assert call["purpose"] == "extract_metadata"
    assert call["max_tokens"] == 1024
    # THE byte-identity gate: exact prompt bytes, incl. the 12000-char cap.
    assert call["prompt"] == extract_metadata_prompt(
        GOLDEN_TITLE, GOLDEN_BODY[:EXTRACT_CONTENT_CHARS]
    )

    # Normalization pinned: lowercase/strip/max-8 tags; entity validation.
    assert out["tags"] == ["ai", "testing", "t3", "t4", "t5", "t6", "t7", "t8"]
    assert out["entities"] == [{"name": "Anthropic", "type": "company"}]
    assert out["summary"] == "A golden summary."


def test_metadata_extractor_empty_defaults_on_not_configured(initialized_library):
    # No API key in test env (conftest deletes it) and provider=anthropic:
    # must return empty defaults, never raise (processor.py relies on this).
    from tiro.ingestion.extractors import extract_metadata

    out = extract_metadata("T", "body", initialized_library)
    assert out == {"tags": [], "entities": [], "summary": ""}


def test_metadata_extractor_empty_defaults_on_garbage_json(
    initialized_library, record_llm,
):
    from tiro.ingestion.extractors import extract_metadata

    record_llm("this is not json at all")
    out = extract_metadata("T", "body", initialized_library)
    assert out == {"tags": [], "entities": [], "summary": ""}


# --- PreferenceClassifier (Task 8) -----------------------------------------


def _seed_rated_library(config):
    """5 rated + 2 unrated articles with fully-known fields, one VIP source.
    Direct SQL — no LLM, no chroma. Returns the unrated ids in insert order."""
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sources (name, domain, source_type, is_vip) "
            "VALUES ('VIP Source', 'vip.example.com', 'web', 1)")
        vip_sid = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO sources (name, domain, source_type) "
            "VALUES ('Plain Source', 'plain.example.com', 'web')")
        plain_sid = cur.lastrowid

        rows = [
            # (title, summary, rating, source_id, ingested_at)
            ("Loved One", "sum L1", 2, vip_sid, "2026-07-01T00:00:05"),
            ("Liked One", "sum K1", 1, plain_sid, "2026-07-01T00:00:04"),
            ("Liked Two", "sum K2", 1, plain_sid, "2026-07-01T00:00:03"),
            ("Disliked One", "sum D1", -1, plain_sid, "2026-07-01T00:00:02"),
            ("Loved Two", "sum L2", 2, vip_sid, "2026-07-01T00:00:01"),
            ("Unrated A", "sum UA", None, plain_sid, "2026-07-01T00:00:07"),
            ("Unrated B", "sum UB", None, plain_sid, "2026-07-01T00:00:06"),
        ]
        unrated_ids = []
        for title, summary, rating, sid, ts in rows:
            slug = title.lower().replace(" ", "-")
            cur = conn.execute(
                """INSERT INTO articles (uid, source_id, title, url, slug,
                   markdown_path, word_count, reading_time_min, ingested_at,
                   rating, summary)
                   VALUES (?, ?, ?, ?, ?, ?, 5, 1, ?, ?, ?)""",
                (new_ulid(), sid, title, f"https://x.com/{slug}", slug,
                 f"{slug}.md", ts, rating, summary),
            )
            if rating is None:
                unrated_ids.append(cur.lastrowid)
        conn.commit()
        return unrated_ids
    finally:
        conn.close()


def test_preference_classifier_transcript_golden(initialized_library, record_llm):
    from tiro.database import get_connection
    from tiro.intelligence.preferences import classify_articles
    from tiro.intelligence.prompts import learned_preferences_prompt

    unrated_ids = _seed_rated_library(initialized_library)
    rec = record_llm(json.dumps({"classifications": [
        {"article_id": unrated_ids[0], "tier": "must-read", "reason": "r1"},
        {"article_id": unrated_ids[1], "tier": "bogus-tier", "reason": "r2"},
    ]}))

    result = classify_articles(initialized_library)

    call = rec.calls[0]
    assert call["tier"] == "heavy"
    assert call["purpose"] == "classify"
    assert call["max_tokens"] == 4096
    # Exact prompt bytes: the template fed with EXACTLY the gathers the
    # seeded data produces (ingested_at DESC ordering, entry-dict shapes).
    expected_prompt = learned_preferences_prompt(
        loved_articles=[
            {"title": "Loved One", "source": "VIP Source", "summary": "sum L1"},
            {"title": "Loved Two", "source": "VIP Source", "summary": "sum L2"},
        ],
        liked_articles=[
            {"title": "Liked One", "source": "Plain Source", "summary": "sum K1"},
            {"title": "Liked Two", "source": "Plain Source", "summary": "sum K2"},
        ],
        disliked_articles=[
            {"title": "Disliked One", "source": "Plain Source", "summary": "sum D1"},
        ],
        vip_sources=["VIP Source"],
        # "Unrated" here means ai_tier IS NULL, independent of the rating
        # column — the seeded rated articles were never classified either,
        # so the real gather includes all 7 rows, DESC by ingested_at.
        unrated_articles=[
            {"id": unrated_ids[0], "title": "Unrated A",
             "source": "Plain Source", "summary": "sum UA"},
            {"id": unrated_ids[1], "title": "Unrated B",
             "source": "Plain Source", "summary": "sum UB"},
            {"id": 1, "title": "Loved One",
             "source": "VIP Source", "summary": "sum L1"},
            {"id": 2, "title": "Liked One",
             "source": "Plain Source", "summary": "sum K1"},
            {"id": 3, "title": "Liked Two",
             "source": "Plain Source", "summary": "sum K2"},
            {"id": 4, "title": "Disliked One",
             "source": "Plain Source", "summary": "sum D1"},
            {"id": 5, "title": "Loved Two",
             "source": "VIP Source", "summary": "sum L2"},
        ],
    )
    assert call["prompt"] == expected_prompt

    # Writeback pinned: valid tier applied, invalid tier skipped.
    assert len(result) == 2
    conn = get_connection(initialized_library.db_path)
    try:
        tiers = {r["id"]: r["ai_tier"] for r in conn.execute(
            "SELECT id, ai_tier FROM articles WHERE rating IS NULL")}
    finally:
        conn.close()
    assert tiers[unrated_ids[0]] == "must-read"
    assert tiers[unrated_ids[1]] is None


def test_preference_classifier_value_errors_preserved(initialized_library):
    from tiro.intelligence.preferences import classify_articles

    with pytest.raises(ValueError, match="Need at least 5 rated articles"):
        classify_articles(initialized_library)


# --- DigestWriter (Task 9) --------------------------------------------------

DIGEST_RESPONSE = (
    "### 1. Ranked by Importance\nranked body\n"
    "### 2. Grouped by Topic\ntopic body\n"
    "### 3. Grouped by Entity\nentity body\n"
)


def _seed_digest_library(config, with_highlight=False):
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    conn = get_connection(config.db_path)
    try:
        sid = conn.execute(
            "INSERT INTO sources (name, domain, source_type) "
            "VALUES ('Digest Source', 'd.example.com', 'web')").lastrowid
        aid = conn.execute(
            """INSERT INTO articles (uid, source_id, title, url, slug,
               markdown_path, word_count, reading_time_min, ingested_at,
               summary)
               VALUES (?, ?, 'Digest Article', 'https://d.example.com/a',
                       'digest-article', 'digest-article.md', 5, 1,
                       '2026-07-09T12:00:00', 'digest sum')""",
            (new_ulid(), sid)).lastrowid
        if with_highlight:
            conn.execute(
                """INSERT INTO highlights (uid, article_id, quote_text,
                   text_position_start, text_position_end, color,
                   content_hash, created_at, updated_at)
                   VALUES (?, ?, 'a quote', 0, 7, 'yellow', 'h',
                           datetime('now'), datetime('now'))""",
                (new_ulid(), aid))
        conn.commit()
        return aid
    finally:
        conn.close()


def test_digest_writer_transcript_golden_no_highlights(
    initialized_library, record_llm,
):
    from tiro.intelligence.digest import generate_digest
    from tiro.intelligence.prompts import daily_digest_prompt

    aid = _seed_digest_library(initialized_library)
    rec = record_llm(DIGEST_RESPONSE)
    result = generate_digest(initialized_library)

    assert len(rec.calls) == 1        # zero highlights => zero recap call
    call = rec.calls[0]
    assert call["tier"] == "heavy"
    assert call["purpose"] == "digest"
    assert call["max_tokens"] == 4096
    expected_articles = [{
        "id": aid, "title": "Digest Article", "source": "Digest Source",
        "is_vip": False, "tags": [], "entities": [], "summary": "digest sum",
        "published_date": "2026-07-09T12:00:00", "relevance_weight": 1.0,
    }]
    assert call["prompt"] == daily_digest_prompt([], [], expected_articles, [])

    assert result["ranked"]["content"] == "ranked body"
    assert result["by_topic"]["content"] == "topic body"
    assert result["by_entity"]["content"] == "entity body"
    assert result["ranked"]["article_ids"] == [aid]
    assert "created_at" in result["ranked"]
    # len("YYYY-MM-DD HH:MM:SS") — full datetime, not a bare date (the
    # historical staleness-banner contract)
    assert len(result["ranked"]["created_at"]) == 19


def test_digest_writer_recap_call_golden(initialized_library, record_llm):
    from tiro.intelligence.digest import generate_digest

    _seed_digest_library(initialized_library, with_highlight=True)
    rec = record_llm(DIGEST_RESPONSE, "recap synthesis")
    result = generate_digest(initialized_library)

    assert len(rec.calls) == 2
    recap = rec.calls[1]
    assert recap["purpose"] == "highlight_recap"
    assert recap["max_tokens"] == 1024
    assert "a quote" in recap["prompt"]
    assert result["ranked"]["content"].endswith("\n\n---\n\nrecap synthesis")
    # recap lands in ranked ONLY
    assert "recap synthesis" not in result["by_topic"]["content"]


def test_digest_writer_caches_and_errors_preserved(initialized_library, record_llm):
    from tiro.intelligence.digest import generate_digest, get_cached_digest

    with pytest.raises(ValueError, match="No articles in library"):
        generate_digest(initialized_library)          # empty library

    _seed_digest_library(initialized_library)
    record_llm("no headers at all")                    # split-fallback path
    result = generate_digest(initialized_library)
    assert result["ranked"]["content"] == "no headers at all"
    assert result["by_topic"]["content"].startswith("*This section was not generated")

    from datetime import date
    cached = get_cached_digest(initialized_library, date.today().isoformat())
    assert cached["ranked"]["content"] == "no headers at all"

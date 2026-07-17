"""ContradictionDetector (Phase 6 K4): prompt, agent flow, applier, backfill.

Hook/dispatch tests live in tests/test_ingest_hooks.py.
"""

import json

# ---------------------------------------------------------------------------
# Shared helpers (self-contained on purpose: mirrors the seeder pattern in
# tests/test_suggestions.py but adds rating/ai_tier — do not import across
# test modules here, K3 helper names may have drifted).
# ---------------------------------------------------------------------------


def _seed_article(config, title="Article", body="Body text.", *,
                  rating=None, ai_tier=None):
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    uid = new_ulid()
    fname = f"{title.lower().replace(' ', '-')}.md"
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.articles_dir / fname).write_text(
        f"---\ntitle: {title}\n---\n\n{body}")
    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sources (name, domain, source_type) "
            "VALUES (?, ?, 'web')",
            (f"src-{uid[:6]}", f"{uid[:6]}.example.com"))
        cur = conn.execute(
            """INSERT INTO articles (uid, source_id, title, url, slug,
               markdown_path, word_count, reading_time_min, ingested_at,
               rating, ai_tier)
               VALUES (?, ?, ?, ?, ?, ?, 3, 1, datetime('now'), ?, ?)""",
            (uid, cur.lastrowid, title, f"https://example.com/{uid[:6]}",
             fname[:-3], fname, rating, ai_tier))
        aid = cur.lastrowid
        conn.commit()
        return aid, uid
    finally:
        conn.close()


def _fake_similars(monkeypatch, id_score_pairs, *, expect_limit=8):
    """Route ctx.similar_articles' Chroma lookup to a canned relation list."""
    def fake(article_id, config, limit=5):
        assert limit == expect_limit
        return [{"related_article_id": i, "similarity_score": s}
                for i, s in id_score_pairs]

    monkeypatch.setattr(
        "tiro.search.semantic.find_related_articles", fake)


VERDICT_HIGH = json.dumps({"contradicts": True, "confidence": "high",
                           "claim": "Rates rose in 2025.",
                           "counter_claim": "Rates fell in 2025."})
VERDICT_MED = json.dumps({"contradicts": True, "confidence": "medium",
                          "claim": "GDP grew 3%.",
                          "counter_claim": "GDP grew 1%."})
VERDICT_LOW = json.dumps({"contradicts": True, "confidence": "low",
                          "claim": "a", "counter_claim": "b"})
VERDICT_NONE = json.dumps({"contradicts": False, "confidence": "high",
                           "claim": "", "counter_claim": ""})


# --- Task 1: config flag + prompt template ---------------------------------


def test_kill_switch_defaults_on(test_config):
    assert test_config.contradiction_detector_enabled is True


def test_contradiction_check_prompt_shape():
    from tiro.intelligence.prompts import contradiction_check_prompt

    p = contradiction_check_prompt(
        "New Piece", "Rates rose in 2025 according to the Fed.",
        "Trusted Piece", "The Fed cut rates through 2025.")
    assert "New Piece" in p and "Trusted Piece" in p
    assert "Rates rose in 2025 according to the Fed." in p
    assert "The Fed cut rates through 2025." in p
    # format() must have collapsed doubled braces into a literal JSON shape
    assert '{"contradicts"' in p
    assert '"confidence"' in p and '"counter_claim"' in p
    # topic overlap alone must be explicitly excluded (fixture pair 3 relies
    # on the instruction existing)
    assert "not a contradiction" in p.lower()

"""ContradictionDetector (Phase 6 K4): prompt, agent flow, applier, backfill.

Hook/dispatch tests live in tests/test_ingest_hooks.py.
"""

import json

import pytest

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


# --- Task 2: similar_articles carries trusted-set fields --------------------


def test_similar_articles_includes_rating_and_ai_tier(
        initialized_library, tmp_path, monkeypatch):
    from tiro.agents.context import RunContext
    from tiro.agents.runtime import TraceWriter

    _aid1, uid1 = _seed_article(initialized_library, title="Anchor K4")
    aid2, _uid2 = _seed_article(initialized_library, title="Loved K4",
                                rating=2)
    aid3, _uid3 = _seed_article(initialized_library, title="Tiered K4",
                                ai_tier="must-read")
    _fake_similars(monkeypatch, [(aid2, 0.9), (aid3, 0.8)], expect_limit=8)

    tw = TraceWriter(tmp_path / "k4-ctx.jsonl")
    tw.header(agent="t", version="1", inputs={}, provider="fake", model="m",
              replay_of=None)
    ctx = RunContext(initialized_library, trace=tw, run_uid="01K4CTX")
    out = ctx.similar_articles(uid1, k=8)
    tw.close()

    by_id = {o["id"]: o for o in out}
    assert by_id[aid2]["rating"] == 2 and by_id[aid2]["ai_tier"] is None
    assert by_id[aid3]["rating"] is None
    assert by_id[aid3]["ai_tier"] == "must-read"


# --- Task 3: the agent -------------------------------------------------------


def _run_detector(config, article_id):
    from tiro.agents.runtime import run_agent

    return run_agent(config, "contradiction-detector",
                     {"article_id": article_id})


def test_detector_files_gated_suggestion(initialized_library, fake_llm,
                                         monkeypatch):
    from tiro.suggestions import list_suggestions

    aid, uid = _seed_article(initialized_library, title="New Fed Piece",
                             body="Rates rose in 2025 according to the Fed.")
    cand_id, cand_uid = _seed_article(
        initialized_library, title="Trusted Fed Piece",
        body="The Fed cut rates through 2025.", rating=2)
    _fake_similars(monkeypatch, [(cand_id, 0.9)])
    fake_llm(VERDICT_HIGH)

    res = _run_detector(initialized_library, aid)
    out = res.outputs.model_dump()
    assert out["article_id"] == aid
    assert out["candidates_considered"] == 1
    assert out["trusted_candidates"] == 1
    assert out["contradictions_found"] == 1
    assert out["verdict_errors"] == 0
    assert len(out["suggestion_uids"]) == 1

    rows = list_suggestions(initialized_library, status="pending")
    assert len(rows) == 1
    s = rows[0]
    assert s["kind"] == "contradiction"
    assert s["persona"] == "contradiction-detector"
    assert s["payload"]["article_id"] == aid
    assert s["payload"]["article_uid"] == uid
    assert s["payload"]["candidate_uid"] == cand_uid
    assert s["payload"]["candidate_title"] == "Trusted Fed Piece"
    assert s["payload"]["claim"] == "Rates rose in 2025."
    assert s["payload"]["counter_claim"] == "Rates fell in 2025."
    assert s["payload"]["confidence"] == "high"
    assert "Challenges something you trusted" in s["payload"]["markdown"]
    # citations: both articles, both actually read
    assert set(s["citations"]) == {uid, cand_uid}
    # the inbox chip depends on json_extract($.article_id) finding it
    assert list_suggestions(initialized_library, article_id=aid)


def test_detector_gates_low_confidence_and_non_contradictions(
        initialized_library, fake_llm, monkeypatch):
    from tiro.suggestions import list_suggestions

    aid, _ = _seed_article(initialized_library, title="New Multi")
    c1, _ = _seed_article(initialized_library, title="Trusted A", rating=1)
    c2, _ = _seed_article(initialized_library, title="Trusted B", rating=2)
    _fake_similars(monkeypatch, [(c1, 0.9), (c2, 0.8)])
    fake_llm(VERDICT_LOW, VERDICT_NONE)

    out = _run_detector(initialized_library, aid).outputs.model_dump()
    assert out["trusted_candidates"] == 2
    assert out["contradictions_found"] == 0
    assert out["verdict_errors"] == 0
    assert list_suggestions(initialized_library) == []


def test_detector_normalizes_medium_confidence(initialized_library, fake_llm,
                                               monkeypatch):
    aid, _ = _seed_article(initialized_library, title="New Med")
    c1, _ = _seed_article(initialized_library, title="Trusted Med", rating=1)
    _fake_similars(monkeypatch, [(c1, 0.9)])
    fake_llm(VERDICT_MED)          # says "medium", not "med"

    out = _run_detector(initialized_library, aid).outputs.model_dump()
    assert out["contradictions_found"] == 1


def test_detector_counts_malformed_verdicts_and_continues(
        initialized_library, fake_llm, monkeypatch):
    aid, _ = _seed_article(initialized_library, title="New Malformed")
    c1, _ = _seed_article(initialized_library, title="Trusted C", rating=1)
    c2, _ = _seed_article(initialized_library, title="Trusted D", rating=1)
    _fake_similars(monkeypatch, [(c1, 0.9), (c2, 0.8)])
    fake_llm("utter nonsense, not json", VERDICT_HIGH)

    out = _run_detector(initialized_library, aid).outputs.model_dump()
    assert out["verdict_errors"] == 1
    assert out["contradictions_found"] == 1     # the second candidate landed


def test_detector_trusted_filter_semantics(initialized_library, fake_llm,
                                           monkeypatch):
    """Trusted = rating > 0 OR ai_tier == 'must-read'; disliked and plain
    articles are OUT (spec §6)."""
    aid, _ = _seed_article(initialized_library, title="New Filter")
    liked, _ = _seed_article(initialized_library, title="Liked", rating=1)
    disliked, _ = _seed_article(initialized_library, title="Disliked",
                                rating=-1)
    tiered, _ = _seed_article(initialized_library, title="MustRead",
                              ai_tier="must-read")
    plain, _ = _seed_article(initialized_library, title="Plain")
    _fake_similars(monkeypatch,
                   [(liked, 0.9), (disliked, 0.8), (tiered, 0.7), (plain, 0.6)])
    fake_llm(VERDICT_NONE, VERDICT_NONE)   # exactly TWO calls expected

    out = _run_detector(initialized_library, aid).outputs.model_dump()
    assert out["candidates_considered"] == 4
    assert out["trusted_candidates"] == 2


def test_detector_empty_trusted_set_makes_zero_llm_calls(
        initialized_library, fake_llm, monkeypatch):
    """FROZEN gate: empty trusted set = zero LLM calls. Proof: the fake
    backend RAISES on an empty queue — we queue nothing, so a green run
    proves ctx.llm was never called."""
    aid, _ = _seed_article(initialized_library, title="New Lonely")
    plain, _ = _seed_article(initialized_library, title="Unrated Neighbor")
    _fake_similars(monkeypatch, [(plain, 0.9)])
    # fake_llm fixture active (routes tiers to fake) but queue left EMPTY

    out = _run_detector(initialized_library, aid).outputs.model_dump()
    assert out["trusted_candidates"] == 0
    assert out["contradictions_found"] == 0
    assert out["suggestion_uids"] == []


def test_detector_no_similars_makes_zero_llm_calls(initialized_library,
                                                   fake_llm, monkeypatch):
    aid, _ = _seed_article(initialized_library, title="New Isolated")
    _fake_similars(monkeypatch, [])

    out = _run_detector(initialized_library, aid).outputs.model_dump()
    assert out["candidates_considered"] == 0
    assert out["suggestion_uids"] == []


# --- Task 4: accept applier --------------------------------------------------


def _contradiction_suggestion(config, article_id, markdown="**Challenges** x"):
    from tiro.suggestions import create_suggestion

    return create_suggestion(
        config, persona="contradiction-detector", kind="contradiction",
        payload={"article_id": article_id, "markdown": markdown,
                 "candidate_title": "Trusted Fed Piece"},
        citations=[])


def test_apply_contradiction_appends_note(initialized_library):
    from tiro.annotations import read_note
    from tiro.database import get_connection
    from tiro.suggestions import apply_suggestion

    aid, _ = _seed_article(initialized_library, title="Apply Target")
    s = _contradiction_suggestion(initialized_library, aid,
                                  markdown="**Challenges** claim vs counter")
    applied = apply_suggestion(initialized_library, s)
    assert applied["body_markdown"]

    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT id, markdown_path FROM articles WHERE id = ?",
            (aid,)).fetchone()
    finally:
        conn.close()
    note = read_note(initialized_library, row["markdown_path"][:-3])
    assert "Flagged by the contradiction detector" in note
    assert "**Challenges** claim vs counter" in note


def test_apply_contradiction_appends_after_existing_note(initialized_library):
    from tiro.annotations import read_note, upsert_article_note
    from tiro.database import get_connection
    from tiro.suggestions import apply_suggestion

    aid, _ = _seed_article(initialized_library, title="Apply Existing")
    upsert_article_note(initialized_library, aid, "My prior thoughts.")
    s = _contradiction_suggestion(initialized_library, aid)
    apply_suggestion(initialized_library, s)

    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT markdown_path FROM articles WHERE id = ?",
            (aid,)).fetchone()
    finally:
        conn.close()
    note = read_note(initialized_library, row["markdown_path"][:-3])
    assert note.startswith("My prior thoughts.")
    assert "\n\n---\n\n" in note


def test_apply_contradiction_validates(initialized_library):
    from tiro.suggestions import SuggestionApplyError, apply_suggestion

    s = _contradiction_suggestion(initialized_library, 999999)
    with pytest.raises(SuggestionApplyError, match="no longer exists"):
        apply_suggestion(initialized_library, s)

    aid, _ = _seed_article(initialized_library, title="Apply Empty")
    s2 = _contradiction_suggestion(initialized_library, aid, markdown="   ")
    with pytest.raises(SuggestionApplyError, match="no body"):
        apply_suggestion(initialized_library, s2)

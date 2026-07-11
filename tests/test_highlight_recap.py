"""Digest "Highlights this week" recap section (Phase 2 M2.3, Task 4).

`_gather_highlights` reads the derived `highlights`/`notes` SQLite index
directly (same posture as `tiro/intelligence/digest.py`'s `_gather_articles`),
so these tests seed rows directly rather than going through the annotations
API -- faster, and gives precise control over `created_at` for the
window/cap tests.
"""

from datetime import UTC, datetime, timedelta

from tiro.database import get_connection
from tiro.intelligence.digest import _gather_highlights, generate_digest, get_cached_digest
from tiro.migrations import new_ulid

DIGEST_TEXT = (
    "## 1. Ranked by Importance\n1. Great Article\n\n"
    "## 2. Grouped by Topic\n- Great Article\n\n"
    "## 3. Grouped by Entity\n- Great Article"
)


def _seed_article(config, stem="article-1", title="Great Article"):
    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        article_uid = new_ulid()
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
            " VALUES (?, ?, ?, ?, ?)",
            (article_uid, source_id, title, stem, f"{stem}.md"),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return article_id
    finally:
        conn.close()


def _seed_highlight(config, article_id, quote="A pithy insight", note=None, created_at=None):
    conn = get_connection(config.db_path)
    try:
        created_at = created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        h_uid = new_ulid()
        conn.execute(
            """INSERT INTO highlights
               (uid, article_id, quote_text, prefix_context, suffix_context,
                text_position_start, text_position_end, content_hash, color,
                created_at, updated_at)
               VALUES (?, ?, ?, 'pre', 'suf', 0, 11, 'hash', 'yellow', ?, ?)""",
            (h_uid, article_id, quote, created_at, created_at),
        )
        highlight_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        if note:
            conn.execute(
                "INSERT INTO notes (uid, article_id, highlight_id, body_markdown,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (new_ulid(), article_id, highlight_id, note, created_at, created_at),
            )
        conn.commit()
        return highlight_id
    finally:
        conn.close()


# --- _gather_highlights: windowing and cap -----------------------------------


def test_gather_highlights_empty_when_none(initialized_library):
    article_id = _seed_article(initialized_library)
    assert _gather_highlights(initialized_library) == []
    # article existing with zero highlights is exactly the zero-highlights case
    assert article_id  # sanity: article was actually created


def test_gather_highlights_excludes_older_than_window(initialized_library):
    config = initialized_library
    article_id = _seed_article(config)
    old = (datetime.now(UTC) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_highlight(config, article_id, quote="stale quote", created_at=old)
    _seed_highlight(config, article_id, quote="fresh quote", created_at=recent)

    gathered = _gather_highlights(config)
    quotes = [h["quote"] for h in gathered]
    assert quotes == ["fresh quote"]


def test_gather_highlights_includes_anchored_note(initialized_library):
    config = initialized_library
    article_id = _seed_article(config)
    _seed_highlight(config, article_id, quote="quoted line", note="my own take")

    gathered = _gather_highlights(config)
    assert len(gathered) == 1
    assert gathered[0]["quote"] == "quoted line"
    assert gathered[0]["note"] == "my own take"
    assert gathered[0]["article_id"] == article_id
    assert gathered[0]["article_title"] == "Great Article"


def test_gather_highlights_caps_at_50_by_recency(initialized_library):
    config = initialized_library
    article_id = _seed_article(config)
    base = datetime.now(UTC)
    for i in range(51):
        # Strictly increasing timestamps so ordering (and therefore which
        # ones get capped) is deterministic: quote-050 is newest, quote-000
        # is oldest, and the cap must drop the oldest one.
        created = (base - timedelta(minutes=51 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_highlight(config, article_id, quote=f"quote-{i:03d}", created_at=created)

    gathered = _gather_highlights(config)
    assert len(gathered) == 50
    quotes = {h["quote"] for h in gathered}
    assert "quote-000" not in quotes  # oldest, dropped by the cap
    assert "quote-050" in quotes  # newest, kept


# --- generate_digest integration ----------------------------------------------


def test_zero_highlights_no_recap_section_and_no_extra_llm_call(
    initialized_library, fake_llm, monkeypatch
):
    # NOTE (K2.2 adaptation): generate_digest now dispatches through the
    # digest_writer agent, whose ctx.llm() calls tiro.llm.llm_call via
    # module-attribute access (see tiro/agents/context.py) -- the same
    # post-refactor seam tests/test_agents_golden.py's record_llm fixture
    # patches. digest.py itself no longer imports/calls llm_call at all, so
    # the spy target moves from digest_mod to tiro.llm; every assertion
    # below is unchanged.
    import tiro.llm as llm_mod

    config = initialized_library
    _seed_article(config)  # article with zero highlights

    calls = []
    real_llm_call = llm_mod.llm_call

    def spy(cfg, tier, prompt, **kw):
        calls.append(kw.get("purpose"))
        return real_llm_call(cfg, tier, prompt, **kw)

    monkeypatch.setattr(llm_mod, "llm_call", spy)
    fake_llm(DIGEST_TEXT)
    result = generate_digest(config)

    assert calls == ["digest"]  # no "highlight_recap" call made
    assert "Highlights This Week" not in result["ranked"]["content"]


def test_highlights_present_recap_appended_to_ranked_only(initialized_library, fake_llm):
    config = initialized_library
    article_id = _seed_article(config)
    _seed_highlight(config, article_id, quote="A pithy insight", note="my own take")

    recap_text = (
        "## Highlights This Week\n\n"
        "### Theme: Insight\n"
        f"> A pithy insight\n"
        f"[Great Article](/articles/{article_id})\n"
    )
    fake_llm(DIGEST_TEXT, recap_text)

    result = generate_digest(config)
    ranked = result["ranked"]["content"]
    assert "Highlights This Week" in ranked
    assert f"/articles/{article_id})" in ranked
    assert "A pithy insight" in ranked

    # Not duplicated into the other two variants.
    assert "Highlights This Week" not in result["by_topic"]["content"]
    assert "Highlights This Week" not in result["by_entity"]["content"]

    # Cached alongside the digest -- no separate cache table.
    from datetime import date

    cached = get_cached_digest(config, date.today().isoformat())
    assert "Highlights This Week" in cached["ranked"]["content"]
    assert "Highlights This Week" not in cached["by_topic"]["content"]


def test_cap_at_50_highlights_reaches_prompt(initialized_library, fake_llm, monkeypatch):
    # NOTE (K2.2 adaptation): see the comment in
    # test_zero_highlights_no_recap_section_and_no_extra_llm_call above --
    # same spy-target move, same reason, assertions unchanged.
    import tiro.llm as llm_mod

    config = initialized_library
    article_id = _seed_article(config)
    base = datetime.now(UTC)
    for i in range(51):
        created = (base - timedelta(minutes=51 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_highlight(config, article_id, quote=f"quote-{i:03d}", created_at=created)

    captured = {}
    real_llm_call = llm_mod.llm_call

    def spy(cfg, tier, prompt, **kw):
        if kw.get("purpose") == "highlight_recap":
            captured["prompt"] = prompt
        return real_llm_call(cfg, tier, prompt, **kw)

    recap_text = "## Highlights This Week\n\nSome recap.\n"
    fake_llm(DIGEST_TEXT, recap_text)
    monkeypatch.setattr(llm_mod, "llm_call", spy)
    generate_digest(config)

    assert "prompt" in captured
    assert captured["prompt"].count("Quote:") == 50
    assert "quote-000" not in captured["prompt"]  # oldest, dropped by the cap
    assert "quote-050" in captured["prompt"]


# --- email path: renders fine with the extra section --------------------------


def test_send_digest_email_with_recap_section_does_not_error(initialized_library, monkeypatch):
    import tiro.intelligence.email_digest as ed

    config = initialized_library
    config.digest_email = "u@example.com"

    class FakeSMTP:
        def __init__(self, *a, **k):
            ...

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendmail(self, *a, **k):
            ...

    monkeypatch.setattr(ed.smtplib, "SMTP", FakeSMTP)

    recap_bearing_ranked = (
        "1. [Great Article](/articles/1) — a reason\n\n"
        "---\n\n"
        "## Highlights This Week\n\n"
        "> A pithy insight\n"
        "[Great Article](/articles/1)\n"
    )
    fresh = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    cached = {
        "ranked": {"content": recap_bearing_ranked, "article_ids": [1], "created_at": fresh},
        "by_topic": {"content": "by topic content", "article_ids": [1], "created_at": fresh},
        "by_entity": {"content": "by entity content", "article_ids": [1], "created_at": fresh},
    }
    monkeypatch.setattr(ed, "get_cached_digest", lambda *a, **k: cached)

    result = ed.send_digest_email(config)
    assert result["sent_to"] == "u@example.com"

    result_all = ed.send_digest_email(config, all_sections=True)
    assert result_all["sent_to"] == "u@example.com"

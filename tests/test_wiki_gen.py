"""On-demand wiki page generation (tiro/wiki_gen.py)."""

import pytest

import tiro.wiki_gen as wiki_gen
from tiro.database import get_connection
from tiro.migrations import canonical_key, new_ulid
from tiro.wiki import read_page, write_page
from tiro.wiki_gen import (
    WikiGenerationError,
    _strip_md_fences,
    gather_node_articles,
    generate_wiki_page,
    regenerate_wiki_page,
)

# --- seeding helpers (mirrors tests/test_wiki.py) ----------------------------


def _seed_source(config, is_vip=False):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "INSERT INTO sources (name, source_type, is_vip) VALUES ('s', 'web', ?)",
            (is_vip,),
        )
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return source_id
    finally:
        conn.close()


def _seed_article(config, source_id, slug, title="T", summary="", rating=None, uid=None):
    conn = get_connection(config.db_path)
    try:
        article_uid = uid or new_ulid()
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, summary, rating)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (article_uid, source_id, title, slug, f"{slug}.md", summary, rating),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return article_id, article_uid
    finally:
        conn.close()


def _link_entity(config, article_id, name, entity_type="company"):
    conn = get_connection(config.db_path)
    try:
        key = canonical_key(name)
        existing = conn.execute(
            "SELECT id FROM entities WHERE entity_type = ? AND canonical_key = ?",
            (entity_type, key),
        ).fetchone()
        if existing:
            entity_id = existing["id"]
        else:
            conn.execute(
                "INSERT INTO entities (uid, name, entity_type, canonical_key) VALUES (?, ?, ?, ?)",
                (new_ulid(), name, entity_type, key),
            )
            entity_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO article_entities (article_id, entity_id) VALUES (?, ?)",
            (article_id, entity_id),
        )
        conn.commit()
        return entity_id
    finally:
        conn.close()


def _link_tag(config, article_id, name):
    conn = get_connection(config.db_path)
    try:
        existing = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if existing:
            tag_id = existing["id"]
        else:
            conn.execute("INSERT INTO tags (uid, name) VALUES (?, ?)", (new_ulid(), name))
            tag_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
            (article_id, tag_id),
        )
        conn.commit()
        return tag_id
    finally:
        conn.close()


def _wiki_page_count(config):
    conn = get_connection(config.db_path)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM wiki_pages").fetchone()["n"]
    finally:
        conn.close()


def _log_text(config):
    log_path = config.wiki_dir / "log.md"
    return log_path.read_text() if log_path.exists() else ""


# --- gather_node_articles ------------------------------------------------------


def test_gather_node_articles_entity(initialized_library):
    config = initialized_library
    source_id = _seed_source(config, is_vip=True)
    article_id, uid = _seed_article(
        config, source_id, "a1", title="Article One", summary="A summary.", rating=2
    )
    _link_entity(config, article_id, "Anthropic", entity_type="company")
    entity_id = _link_entity(config, article_id, "Anthropic", entity_type="company")

    articles = gather_node_articles(config, "entity", entity_id)
    assert len(articles) == 1
    a = articles[0]
    assert a == {
        "stem": "a1",
        "title": "Article One",
        "summary": "A summary.",
        "rating_label": "Love",
        "is_vip": True,
        "relevance_weight": 1.0,
        "uid": uid,
    }


def test_gather_node_articles_tag(initialized_library):
    config = initialized_library
    source_id = _seed_source(config)
    article_id, uid = _seed_article(config, source_id, "a1", title="T1", summary="")
    tag_id = _link_tag(config, article_id, "context-engineering")

    articles = gather_node_articles(config, "tag", tag_id)
    assert len(articles) == 1
    assert articles[0]["stem"] == "a1"
    assert articles[0]["summary"] == ""
    assert articles[0]["rating_label"] is None
    assert articles[0]["is_vip"] is False
    assert articles[0]["uid"] == uid


def test_gather_node_articles_no_links_returns_empty(initialized_library):
    config = initialized_library
    assert gather_node_articles(config, "tag", 999) == []


def test_gather_node_articles_invalid_node_type_raises(initialized_library):
    with pytest.raises(ValueError):
        gather_node_articles(initialized_library, "bogus", 1)


# --- generate_wiki_page: happy path ---------------------------------------------


def test_generate_wiki_page_entity_happy_path(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    article_id, uid = _seed_article(
        config, source_id, "a1", title="Article One", summary="A summary."
    )
    entity_id = _link_entity(config, article_id, "Anthropic", entity_type="company")

    fake_llm("Anthropic makes Claude. [[a1|source]]")

    result = generate_wiki_page(config, "entity", entity_id)

    assert result["slug"] == "entities/anthropic"
    assert result["title"] == "Anthropic"
    assert result["created"] is True
    assert result["cited_articles"] == 1
    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert "cost_estimate" in result

    page = read_page(config, "entities/anthropic")
    assert page is not None
    assert page["kind"] == "entity"
    assert page["entity_type"] == "company"
    assert page["article_uids"] == [uid]
    assert page["body"] == "Anthropic makes Claude. [[a1|source]]"

    assert _wiki_page_count(config) == 1
    assert "create | entities/anthropic" in _log_text(config)
    assert (config.wiki_dir / "index.md").exists()


def test_generate_wiki_page_tag_maps_to_concept_kind(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    article_id, uid = _seed_article(config, source_id, "a1", title="T1", summary="s")
    tag_id = _link_tag(config, article_id, "context-engineering")

    fake_llm("Context engineering is a discipline. [[a1|source]]")

    result = generate_wiki_page(config, "tag", tag_id)

    assert result["slug"] == "concepts/context-engineering"
    page = read_page(config, "concepts/context-engineering")
    assert page["kind"] == "concept"
    assert page["entity_type"] is None
    assert page["article_uids"] == [uid]


# --- generate_wiki_page: fence-stripped model output -----------------------------


def test_generate_strips_markdown_fences_from_model_output(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    article_id, uid = _seed_article(
        config, source_id, "a1", title="Article One", summary="A summary."
    )
    entity_id = _link_entity(config, article_id, "Anthropic", entity_type="company")

    stem = "a1"
    fake_llm(f"```markdown\nBody with a citation [[{stem}|per source]].\n```")

    result = generate_wiki_page(config, "entity", entity_id)
    assert result["cited_articles"] == 1

    page = read_page(config, "entities/anthropic")
    assert page["body"].startswith("Body with")
    assert "`" not in page["body"]


def test_generate_strips_bare_fences(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    article_id, uid = _seed_article(
        config, source_id, "a1", title="Article One", summary="A summary."
    )
    entity_id = _link_entity(config, article_id, "Anthropic", entity_type="company")

    stem = "a1"
    fake_llm(f"```\nBody with a citation [[{stem}|per source]].\n```")

    result = generate_wiki_page(config, "entity", entity_id)
    assert result["cited_articles"] == 1

    page = read_page(config, "entities/anthropic")
    assert page["body"].startswith("Body with")
    assert "`" not in page["body"]


# --- _strip_md_fences (direct) ---------------------------------------------------


def test_strip_md_fences_with_lang_hint():
    assert _strip_md_fences("```markdown\nHello world\n```") == "Hello world"


def test_strip_md_fences_bare_fence():
    assert _strip_md_fences("```\nHello world\n```") == "Hello world"


def test_strip_md_fences_unfenced_passthrough():
    assert _strip_md_fences("Hello world") == "Hello world"


def test_strip_md_fences_inline_code_span_untouched():
    text = "A body mentioning an inline `x` code span, not a wrapping fence."
    assert _strip_md_fences(text) == text


# --- generate_wiki_page: citation validation -------------------------------------


def test_generate_wiki_page_zero_citations_raises_and_writes_nothing(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    article_id, _ = _seed_article(config, source_id, "a1", title="T1", summary="s")
    entity_id = _link_entity(config, article_id, "Anthropic")

    fake_llm("Anthropic is a company with no citations at all.")

    with pytest.raises(WikiGenerationError):
        generate_wiki_page(config, "entity", entity_id)

    assert read_page(config, "entities/anthropic") is None
    assert not (config.wiki_dir / "entities" / "anthropic.md").exists()
    assert _wiki_page_count(config) == 0
    assert _log_text(config) == ""


def test_generate_wiki_page_unresolvable_citations_only_raises(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    article_id, _ = _seed_article(config, source_id, "a1", title="T1", summary="s")
    entity_id = _link_entity(config, article_id, "Anthropic")

    fake_llm("Anthropic is a company. [[not-a-real-stem|source]]")

    with pytest.raises(WikiGenerationError):
        generate_wiki_page(config, "entity", entity_id)

    assert read_page(config, "entities/anthropic") is None
    assert _wiki_page_count(config) == 0
    assert _log_text(config) == ""


def test_generate_wiki_page_mixed_citations_counts_only_resolved(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    a1_id, a1_uid = _seed_article(config, source_id, "a1", title="T1", summary="s")
    entity_id = _link_entity(config, a1_id, "Anthropic")

    fake_llm("Anthropic. [[a1|real]] Also see [[ghost-stem|fake]].")

    result = generate_wiki_page(config, "entity", entity_id)
    assert result["cited_articles"] == 1
    page = read_page(config, "entities/anthropic")
    assert page["article_uids"] == [a1_uid]


def test_generate_wiki_page_no_linked_articles_raises(initialized_library):
    config = initialized_library
    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO tags (uid, name) VALUES (?, 'orphan-tag')", (new_ulid(),))
        tag_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(WikiGenerationError):
        generate_wiki_page(config, "tag", tag_id)


# --- generate_wiki_page: 404-equivalents -----------------------------------------


def test_generate_wiki_page_unknown_entity_raises_value_error(initialized_library):
    with pytest.raises(ValueError):
        generate_wiki_page(initialized_library, "entity", 999)


def test_generate_wiki_page_unknown_tag_raises_value_error(initialized_library):
    with pytest.raises(ValueError):
        generate_wiki_page(initialized_library, "tag", 999)


def test_generate_wiki_page_invalid_node_type_raises_value_error(initialized_library):
    with pytest.raises(ValueError):
        generate_wiki_page(initialized_library, "bogus", 1)


# --- generate_wiki_page: update path ----------------------------------------------


def test_generate_wiki_page_update_passes_prior_body_into_prompt(
    initialized_library, fake_llm, monkeypatch
):
    config = initialized_library
    source_id = _seed_source(config)
    a1_id, a1_uid = _seed_article(config, source_id, "a1", title="T1", summary="s")
    entity_id = _link_entity(config, a1_id, "Anthropic")

    fake_llm("Anthropic makes Claude. [[a1|source]]")
    first = generate_wiki_page(config, "entity", entity_id)
    assert first["created"] is True
    first_page = read_page(config, "entities/anthropic")

    a2_id, a2_uid = _seed_article(config, source_id, "a2", title="T2", summary="s2")
    _link_entity(config, a2_id, "Anthropic")

    captured = {}
    real_prompt = wiki_gen.wiki_page_prompt

    def capturing_prompt(**kwargs):
        captured.update(kwargs)
        return real_prompt(**kwargs)

    monkeypatch.setattr(wiki_gen, "wiki_page_prompt", capturing_prompt)

    fake_llm("Anthropic makes Claude and more. [[a1|source]] [[a2|source]]")
    second = generate_wiki_page(config, "entity", entity_id)

    assert captured["prior_body"] == first_page["body"]
    assert second["created"] is False
    assert second["cited_articles"] == 2

    page = read_page(config, "entities/anthropic")
    assert set(page["article_uids"]) == {a1_uid, a2_uid}
    assert page["uid"] == first_page["uid"]


def test_generate_wiki_page_tag_uid_stable_across_regeneration(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    a1_id, _ = _seed_article(config, source_id, "a1", title="T1", summary="s")
    tag_id = _link_tag(config, a1_id, "context-engineering")

    fake_llm("Context engineering. [[a1|source]]")
    first = generate_wiki_page(config, "tag", tag_id)
    first_uid = read_page(config, "concepts/context-engineering")["uid"]

    fake_llm("Context engineering, updated. [[a1|source]]")
    generate_wiki_page(config, "tag", tag_id)
    second_uid = read_page(config, "concepts/context-engineering")["uid"]

    assert first["slug"] == "concepts/context-engineering"
    assert first_uid == second_uid


# --- regenerate_wiki_page ----------------------------------------------------------


def test_regenerate_wiki_page_preserves_uid_and_pinned_note(initialized_library, fake_llm):
    config = initialized_library
    source_id = _seed_source(config)
    a1_id, a1_uid = _seed_article(config, source_id, "a1", title="T1", summary="s")
    entity_id = _link_entity(config, a1_id, "Anthropic")

    fake_llm("Anthropic makes Claude. [[a1|source]]")
    generate_wiki_page(config, "entity", entity_id)
    original = read_page(config, "entities/anthropic")

    # Pin a note the way a human editor would (write_page preserves it going
    # forward -- this is the store's existing contract, not wiki_gen's).
    write_page(
        config,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=original["article_uids"],
        body=original["body"],
        generated_by=original["generated_by"],
        user_pinned_note="Don't mention the lawsuit.",
        uid=original["uid"],
    )

    fake_llm("Anthropic, from scratch. [[a1|source]]")
    result = regenerate_wiki_page(config, "entities/anthropic")

    assert result["slug"] == "entities/anthropic"
    page = read_page(config, "entities/anthropic")
    assert page["uid"] == original["uid"]
    assert page["user_pinned_note"] == "Don't mention the lawsuit."
    assert page["body"] == "Anthropic, from scratch. [[a1|source]]"
    assert page["article_uids"] == [a1_uid]


def test_regenerate_wiki_page_prior_body_is_none(initialized_library, fake_llm, monkeypatch):
    config = initialized_library
    source_id = _seed_source(config)
    a1_id, _ = _seed_article(config, source_id, "a1", title="T1", summary="s")
    tag_id = _link_tag(config, a1_id, "context-engineering")

    fake_llm("Context engineering. [[a1|source]]")
    generate_wiki_page(config, "tag", tag_id)

    captured = {}
    real_prompt = wiki_gen.wiki_page_prompt

    def capturing_prompt(**kwargs):
        captured.update(kwargs)
        return real_prompt(**kwargs)

    monkeypatch.setattr(wiki_gen, "wiki_page_prompt", capturing_prompt)

    fake_llm("Context engineering, from scratch. [[a1|source]]")
    regenerate_wiki_page(config, "concepts/context-engineering")

    assert captured["prior_body"] is None


def test_regenerate_wiki_page_unknown_slug_raises_value_error(initialized_library):
    with pytest.raises(ValueError):
        regenerate_wiki_page(initialized_library, "entities/does-not-exist")

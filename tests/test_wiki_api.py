"""Wiki API routes (tiro/api/routes_wiki.py): list, read, generate, regenerate."""

from tiro.database import get_connection
from tiro.migrations import canonical_key, new_ulid
from tiro.wiki import write_page

# --- seeding helpers (mirrors tests/test_wiki_gen.py) ------------------------


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


# --- GET /api/wiki (list) -----------------------------------------------------


def test_list_wiki_pages_empty(authenticated_client):
    r = authenticated_client.get("/api/wiki")
    assert r.status_code == 200
    assert r.json() == {"success": True, "data": {"pages": []}}


def test_list_wiki_pages_ordered_kind_then_title(authenticated_client, configured_library):
    config = configured_library
    write_page(
        config, slug="concepts/zeta", kind="concept", title="Zeta", entity_type=None,
        article_uids=[], body="body", generated_by="test",
    )
    write_page(
        config, slug="entities/beta", kind="entity", title="Beta", entity_type="company",
        article_uids=[], body="body", generated_by="test",
    )
    write_page(
        config, slug="entities/alpha", kind="entity", title="Alpha", entity_type="company",
        article_uids=[], body="body", generated_by="test",
    )

    r = authenticated_client.get("/api/wiki")
    assert r.status_code == 200
    pages = r.json()["data"]["pages"]
    # ORDER BY kind, title: alphabetical by kind ("concept" < "entity"), then title.
    assert [(p["kind"], p["title"]) for p in pages] == [
        ("concept", "Zeta"),
        ("entity", "Alpha"),
        ("entity", "Beta"),
    ]


# --- GET /api/wiki/{slug} (read) -----------------------------------------------


def test_get_wiki_page_unknown_slug_404(authenticated_client):
    r = authenticated_client.get("/api/wiki/entities/does-not-exist")
    assert r.status_code == 404


def test_get_wiki_page_traversal_slug_returns_404_not_500(authenticated_client):
    # %2E%2E decodes to ".." while the literal "/" stays a path separator, so
    # the slug reaching the route is "entities/../../etc" -- a traversal shape
    # that page_path() rejects with ValueError. The route must map that to a
    # 404, not let it bubble up as an unhandled 500.
    r = authenticated_client.get("/api/wiki/entities/%2E%2E/%2E%2E/etc")
    assert r.status_code == 404


def test_get_wiki_page_returns_body_and_resolved_citations(
    authenticated_client, configured_library
):
    config = configured_library
    source_id = _seed_source(config)
    _article_id, uid = _seed_article(config, source_id, "a1", title="Article One")

    write_page(
        config,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[uid],
        body="Anthropic makes Claude. [[a1|source]] Also see [[ghost|nope]].",
        generated_by="test",
    )

    r = authenticated_client.get("/api/wiki/entities/anthropic")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["slug"] == "entities/anthropic"
    assert data["title"] == "Anthropic"
    assert "Anthropic makes Claude" in data["body"]
    # Resolvable stem maps to the article id; unresolvable stem is absent.
    assert data["citations"] == {"a1": _article_id}


def test_get_wiki_page_no_citations_empty_map(authenticated_client, configured_library):
    config = configured_library
    write_page(
        config, slug="concepts/no-links", kind="concept", title="No Links", entity_type=None,
        article_uids=[], body="A body with no wikilinks at all.", generated_by="test",
    )
    r = authenticated_client.get("/api/wiki/concepts/no-links")
    assert r.status_code == 200
    assert r.json()["data"]["citations"] == {}


# --- POST /api/wiki/generate ---------------------------------------------------


def test_generate_happy_path_via_fake_llm(authenticated_client, configured_library, fake_llm):
    config = configured_library
    config.ai_light_provider = "fake"
    source_id = _seed_source(config)
    article_id, _uid = _seed_article(config, source_id, "a1", title="Article One", summary="s")
    entity_id = _link_entity(config, article_id, "Anthropic", entity_type="company")

    fake_llm("Anthropic makes Claude. [[a1|source]]")

    r = authenticated_client.post(
        "/api/wiki/generate", json={"node_type": "entity", "node_id": entity_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["slug"] == "entities/anthropic"
    assert body["data"]["cited_articles"] == 1

    r2 = authenticated_client.get("/api/wiki/entities/anthropic")
    assert r2.status_code == 200
    assert r2.json()["data"]["citations"] == {"a1": article_id}


def test_generate_zero_citations_returns_422(authenticated_client, configured_library, fake_llm):
    config = configured_library
    config.ai_light_provider = "fake"
    source_id = _seed_source(config)
    article_id, _uid = _seed_article(config, source_id, "a1", title="Article One", summary="s")
    entity_id = _link_entity(config, article_id, "Anthropic")

    fake_llm("Anthropic is a company with no citations at all.")

    r = authenticated_client.post(
        "/api/wiki/generate", json={"node_type": "entity", "node_id": entity_id}
    )
    assert r.status_code == 422
    assert "cited zero resolvable articles" in r.json()["detail"]


def test_generate_unknown_node_returns_404(authenticated_client, configured_library, fake_llm):
    config = configured_library
    config.ai_light_provider = "fake"
    r = authenticated_client.post(
        "/api/wiki/generate", json={"node_type": "entity", "node_id": 999}
    )
    assert r.status_code == 404


def test_generate_concurrent_duplicate_returns_409(
    authenticated_client, configured_library, monkeypatch
):
    import tiro.api.routes_wiki as rw

    def _boom(*a, **k):
        raise AssertionError("generate_wiki_page must not run while already in-flight")

    monkeypatch.setattr(rw, "generate_wiki_page", _boom)
    monkeypatch.setattr(rw, "_generating_nodes", {("entity", 1)})

    r = authenticated_client.post(
        "/api/wiki/generate", json={"node_type": "entity", "node_id": 1}
    )
    assert r.status_code == 409


# --- POST /api/wiki/{slug}/regenerate ------------------------------------------


def test_regenerate_happy_path_via_fake_llm(authenticated_client, configured_library, fake_llm):
    config = configured_library
    config.ai_light_provider = "fake"
    source_id = _seed_source(config)
    article_id, _uid = _seed_article(config, source_id, "a1", title="Article One", summary="s")
    entity_id = _link_entity(config, article_id, "Anthropic", entity_type="company")

    fake_llm("Anthropic makes Claude. [[a1|source]]")
    r = authenticated_client.post(
        "/api/wiki/generate", json={"node_type": "entity", "node_id": entity_id}
    )
    assert r.status_code == 200

    fake_llm("Anthropic, from scratch. [[a1|source]]")
    r2 = authenticated_client.post("/api/wiki/entities/anthropic/regenerate")
    assert r2.status_code == 200
    assert r2.json()["data"]["slug"] == "entities/anthropic"

    r3 = authenticated_client.get("/api/wiki/entities/anthropic")
    assert r3.json()["data"]["body"] == "Anthropic, from scratch. [[a1|source]]"


def test_regenerate_unknown_slug_returns_404(authenticated_client, configured_library, fake_llm):
    config = configured_library
    config.ai_light_provider = "fake"
    r = authenticated_client.post("/api/wiki/entities/does-not-exist/regenerate")
    assert r.status_code == 404


def test_regenerate_concurrent_duplicate_returns_409(
    authenticated_client, configured_library, monkeypatch
):
    import tiro.api.routes_wiki as rw

    def _boom(*a, **k):
        raise AssertionError("regenerate_wiki_page must not run while already in-flight")

    monkeypatch.setattr(rw, "regenerate_wiki_page", _boom)
    monkeypatch.setattr(rw, "_regenerating_slugs", {"entities/anthropic"})

    r = authenticated_client.post("/api/wiki/entities/anthropic/regenerate")
    assert r.status_code == 409


# --- MCP tools: list_wiki_pages, get_wiki_page --------------------------------
#
# These are plain module-level functions decorated with @mcp.tool() (FastMCP
# doesn't wrap them), so they're callable directly -- same precedent as
# test_queries.py::test_mcp_filter_sql_finds_null_tier_and_null_method_article.
# Unlike that test's _build_filter_sql (no config access), list_wiki_pages/
# get_wiki_page call _get_config() internally, so we point the module's
# global _config at an already-initialized library (no password -> the token
# gate no-ops) instead of going through load_config()/init_db().


def _mcp_config(monkeypatch, config):
    import tiro.mcp.server as mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    return mcp_server


def test_mcp_list_wiki_pages_empty(initialized_library, monkeypatch):
    mcp_server = _mcp_config(monkeypatch, initialized_library)
    result = mcp_server.list_wiki_pages()
    assert "No wiki pages yet" in result


def test_mcp_list_wiki_pages_populated(initialized_library, monkeypatch):
    config = initialized_library
    write_page(
        config, slug="entities/anthropic", kind="entity", title="Anthropic",
        entity_type="company", article_uids=[], body="body", generated_by="test",
    )
    write_page(
        config, slug="concepts/context-engineering", kind="concept",
        title="Context Engineering", entity_type=None, article_uids=[],
        body="body", generated_by="test",
    )
    mcp_server = _mcp_config(monkeypatch, config)
    result = mcp_server.list_wiki_pages()
    assert "entities/anthropic" in result and "Anthropic" in result
    assert "concepts/context-engineering" in result and "Context Engineering" in result
    assert "entity" in result and "concept" in result
    assert "fresh" in result


def test_mcp_get_wiki_page_existing(initialized_library, monkeypatch):
    config = initialized_library
    source_id = _seed_source(config)
    _article_id, uid = _seed_article(config, source_id, "a1", title="Article One")
    write_page(
        config, slug="entities/anthropic", kind="entity", title="Anthropic",
        entity_type="company", article_uids=[uid],
        body="Anthropic makes Claude.", generated_by="test",
    )
    mcp_server = _mcp_config(monkeypatch, config)
    result = mcp_server.get_wiki_page("entities/anthropic")
    assert "# Anthropic" in result
    assert "**Kind:** entity" in result
    assert "**Status:** fresh" in result
    assert "**Sources:** 1" in result
    assert "Anthropic makes Claude." in result


def test_mcp_get_wiki_page_unknown_slug_lists_available(initialized_library, monkeypatch):
    config = initialized_library
    write_page(
        config, slug="entities/anthropic", kind="entity", title="Anthropic",
        entity_type="company", article_uids=[], body="body", generated_by="test",
    )
    mcp_server = _mcp_config(monkeypatch, config)
    result = mcp_server.get_wiki_page("entities/does-not-exist")
    assert "No wiki page found" in result
    assert "entities/anthropic" in result


def test_mcp_get_wiki_page_no_pages_at_all_message(initialized_library, monkeypatch):
    mcp_server = _mcp_config(monkeypatch, initialized_library)
    result = mcp_server.get_wiki_page("entities/does-not-exist")
    assert "No wiki pages exist yet" in result


def test_mcp_get_wiki_page_traversal_slug_returns_message_not_exception(
    initialized_library, monkeypatch
):
    # page_path() raises ValueError on a traversal-shaped slug; the MCP tool
    # must turn that into a friendly not-found message, not let it propagate
    # (MCP tools return messages, never exceptions).
    mcp_server = _mcp_config(monkeypatch, initialized_library)
    result = mcp_server.get_wiki_page("entities/../../etc")
    assert "No wiki page found" in result
    assert "No wiki pages exist yet" in result


def test_mcp_get_wiki_page_available_slugs_capped_at_20(initialized_library, monkeypatch):
    config = initialized_library
    conn = get_connection(config.db_path)
    try:
        for i in range(25):
            conn.execute(
                "INSERT INTO wiki_pages (slug, kind, title, source_count) "
                "VALUES (?, 'concept', ?, 0)",
                (f"concepts/topic-{i:02d}", f"Topic {i:02d}"),
            )
        conn.commit()
    finally:
        conn.close()

    mcp_server = _mcp_config(monkeypatch, config)
    result = mcp_server.get_wiki_page("concepts/does-not-exist")
    assert "No wiki page found" in result
    listed = result.split("Available slugs: ", 1)[1]
    assert len(listed.split(", ")) == 20

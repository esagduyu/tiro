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

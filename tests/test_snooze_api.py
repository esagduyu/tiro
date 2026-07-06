"""PATCH /api/articles/{id}/snooze, GET /api/articles snooze filtering,
GET /api/filters snoozed facet, and the per-consumer visibility audit
(digest gather, decay, classify, MCP search all still see snoozed
articles — only the inbox listing hides them). Phase 3 M3.0 Task 1."""

from datetime import UTC, datetime, timedelta

from tiro.database import get_connection


def _seed_article(config, slug="art-1", title="T1", snoozed_until=None, **extra):
    conn = get_connection(config.db_path)
    conn.execute("INSERT OR IGNORE INTO sources (name, source_type) VALUES ('Src', 'web')")
    cols = ["uid", "source_id", "title", "slug", "markdown_path", "snoozed_until"]
    vals = [slug.upper().ljust(26, "0"), 1, title, slug, f"{slug}.md", snoozed_until]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    placeholders = ", ".join("?" * len(vals))
    conn.execute(
        f"INSERT INTO articles ({', '.join(cols)}) VALUES ({placeholders})", vals
    )
    conn.commit()
    row = conn.execute("SELECT id FROM articles WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.articles_dir / f"{slug}.md").write_text(f"---\ntitle: {title}\n---\nbody {slug}")
    return row["id"]


def _future_iso(days=1) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


# --- PATCH /api/articles/{id}/snooze -----------------------------------


def test_snooze_with_explicit_until(authenticated_client, configured_library):
    aid = _seed_article(configured_library)
    until = _future_iso(3)
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"until": until})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["id"] == aid
    assert body["data"]["snoozed_until"]  # stored, normalized value

    conn = get_connection(configured_library.db_path)
    stored = conn.execute("SELECT snoozed_until FROM articles WHERE id=?", (aid,)).fetchone()[0]
    conn.close()
    assert stored == body["data"]["snoozed_until"]


def test_snooze_rejects_past_until(authenticated_client, configured_library):
    aid = _seed_article(configured_library)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"until": past})
    assert r.status_code == 400


def test_snooze_rejects_malformed_until(authenticated_client, configured_library):
    aid = _seed_article(configured_library)
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"until": "not-a-date"})
    assert r.status_code == 400


def test_snooze_rejects_both_until_and_preset(authenticated_client, configured_library):
    aid = _seed_article(configured_library)
    r = authenticated_client.patch(
        f"/api/articles/{aid}/snooze",
        json={"until": _future_iso(), "preset": "tonight"},
    )
    assert r.status_code == 400


def test_snooze_rejects_unknown_preset(authenticated_client, configured_library):
    aid = _seed_article(configured_library)
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"preset": "eventually"})
    assert r.status_code == 400


def test_snooze_404_for_unknown_article(authenticated_client, configured_library):
    r = authenticated_client.patch("/api/articles/999999/snooze", json={"until": _future_iso()})
    assert r.status_code == 404


def test_unsnooze_via_null_until(authenticated_client, configured_library):
    aid = _seed_article(configured_library, snoozed_until="2099-01-01 00:00:00")
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"until": None})
    assert r.status_code == 200
    assert r.json()["data"]["snoozed_until"] is None

    conn = get_connection(configured_library.db_path)
    stored = conn.execute("SELECT snoozed_until FROM articles WHERE id=?", (aid,)).fetchone()[0]
    conn.close()
    assert stored is None


def test_unsnooze_via_empty_body(authenticated_client, configured_library):
    aid = _seed_article(configured_library, snoozed_until="2099-01-01 00:00:00")
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={})
    assert r.status_code == 200
    assert r.json()["data"]["snoozed_until"] is None


def test_snooze_preset_tonight_uses_frozen_clock(
    authenticated_client, configured_library, monkeypatch
):
    import tiro.snooze as snooze_mod

    frozen = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)
    monkeypatch.setattr(snooze_mod, "_local_now", lambda: frozen)

    aid = _seed_article(configured_library)
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"preset": "tonight"})
    assert r.status_code == 200
    assert r.json()["data"]["snoozed_until"] == "2026-07-06 19:00:00"


def test_snooze_preset_tomorrow(authenticated_client, configured_library, monkeypatch):
    import tiro.snooze as snooze_mod

    frozen = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    monkeypatch.setattr(snooze_mod, "_local_now", lambda: frozen)

    aid = _seed_article(configured_library)
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"preset": "tomorrow"})
    assert r.status_code == 200
    assert r.json()["data"]["snoozed_until"] == "2026-07-07 09:00:00"


def test_snooze_preset_weekend(authenticated_client, configured_library, monkeypatch):
    import tiro.snooze as snooze_mod

    frozen = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)  # Monday
    monkeypatch.setattr(snooze_mod, "_local_now", lambda: frozen)

    aid = _seed_article(configured_library)
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"preset": "weekend"})
    assert r.status_code == 200
    assert r.json()["data"]["snoozed_until"] == "2026-07-11 09:00:00"


def test_snooze_preset_next_week(authenticated_client, configured_library, monkeypatch):
    import tiro.snooze as snooze_mod

    frozen = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)  # Monday
    monkeypatch.setattr(snooze_mod, "_local_now", lambda: frozen)

    aid = _seed_article(configured_library)
    r = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"preset": "next_week"})
    assert r.status_code == 200
    assert r.json()["data"]["snoozed_until"] == "2026-07-13 09:00:00"


# --- GET /api/articles inbox filtering ----------------------------------


def test_inbox_hides_future_snoozed_by_default(authenticated_client, configured_library):
    _seed_article(configured_library, slug="future", title="Future", snoozed_until="2099-01-01 00:00:00")
    _seed_article(configured_library, slug="normal", title="Normal")

    r = authenticated_client.get("/api/articles")
    titles = {a["title"] for a in r.json()["data"]}
    assert titles == {"Normal"}


def test_inbox_include_snoozed_reveals_it(authenticated_client, configured_library):
    _seed_article(configured_library, slug="future", title="Future", snoozed_until="2099-01-01 00:00:00")
    _seed_article(configured_library, slug="normal", title="Normal")

    r = authenticated_client.get("/api/articles", params={"include_snoozed": "true"})
    titles = {a["title"] for a in r.json()["data"]}
    assert titles == {"Normal", "Future"}


def test_inbox_expired_snooze_auto_reappears(authenticated_client, configured_library):
    past = "2000-01-01 00:00:00"
    _seed_article(configured_library, slug="expired", title="Expired", snoozed_until=past)

    r = authenticated_client.get("/api/articles")
    titles = {a["title"] for a in r.json()["data"]}
    assert titles == {"Expired"}  # past snoozed_until no longer excluded


def test_inbox_count_only_respects_snooze_default(authenticated_client, configured_library):
    _seed_article(configured_library, slug="future", title="Future", snoozed_until="2099-01-01 00:00:00")
    _seed_article(configured_library, slug="normal", title="Normal")

    r = authenticated_client.get("/api/articles", params={"count_only": "true"})
    assert r.json()["data"]["count"] == 1


# --- GET /api/filters snoozed facet -------------------------------------


def test_filters_snoozed_facet_counts_future_only(authenticated_client, configured_library):
    _seed_article(configured_library, slug="future", title="Future", snoozed_until="2099-01-01 00:00:00")
    _seed_article(configured_library, slug="expired", title="Expired", snoozed_until="2000-01-01 00:00:00")
    _seed_article(configured_library, slug="normal", title="Normal")

    r = authenticated_client.get("/api/filters")
    assert r.status_code == 200
    assert r.json()["data"]["snoozed"] == 1


# --- Per-consumer visibility audit: snoozed articles are NOT deleted, so ---
# --- everything except the inbox default keeps seeing them. --------------


def test_digest_gather_still_sees_snoozed_article(test_config):
    from tiro.database import init_db, migrate_db
    from tiro.intelligence.digest import _gather_articles

    init_db(test_config.db_path)
    migrate_db(test_config.db_path)
    _seed_article(test_config, slug="snoozed-digest", title="Snoozed For Digest",
                  snoozed_until="2099-01-01 00:00:00")

    articles, _vip_sources, _vip_authors, _ratings = _gather_articles(test_config)
    assert any(a["title"] == "Snoozed For Digest" for a in articles)


def test_decay_recalculation_still_processes_snoozed_article(test_config):
    from tiro.database import init_db, migrate_db
    from tiro.decay import recalculate_decay

    init_db(test_config.db_path)
    migrate_db(test_config.db_path)
    _seed_article(test_config, slug="snoozed-decay", title="Snoozed For Decay",
                  snoozed_until="2099-01-01 00:00:00")

    result = recalculate_decay(test_config)
    assert result["total"] == 1  # the snoozed row was included, not skipped


def test_classifier_gather_still_sees_snoozed_unrated_article(test_config):
    from tiro.database import init_db, migrate_db
    from tiro.intelligence.preferences import _gather_unrated_articles

    init_db(test_config.db_path)
    migrate_db(test_config.db_path)
    _seed_article(test_config, slug="snoozed-classify", title="Snoozed For Classify",
                  snoozed_until="2099-01-01 00:00:00")

    unrated = _gather_unrated_articles(test_config)
    assert any(a["title"] == "Snoozed For Classify" for a in unrated)


def test_mcp_search_still_finds_snoozed_article(initialized_library, monkeypatch):
    import tiro.mcp.server as mcp_server

    monkeypatch.setattr(mcp_server, "_config", initialized_library)
    _seed_article(initialized_library, slug="snoozed-mcp", title="Snoozed For MCP",
                  snoozed_until="2099-01-01 00:00:00")

    result = mcp_server.search_articles(query="")
    assert "Snoozed For MCP" in result


def test_semantic_search_payload_carries_snoozed_until(authenticated_client, configured_library):
    # Finding 5 (M3.2 final review): tiro/search/semantic.py's SELECT
    # (backing GET /api/search) omitted snoozed_until, breaking the repo
    # convention that the column appears in every article-returning query
    # (list, detail, AND search) -- a snoozed article surfaced by search
    # rendered without its "Snoozed until..." chip client-side.
    #
    # Ingested via the real API (not the raw-SQL _seed_article helper above)
    # so the article is actually embedded in ChromaDB and reachable through
    # search_articles()'s real query path.
    from pathlib import Path

    from tiro.search.semantic import search_articles

    eml = (Path(__file__).parent / "fixtures" / "newsletter.eml").read_bytes()
    r = authenticated_client.post(
        "/api/ingest/email", files={"file": ("newsletter.eml", eml, "message/rfc822")}
    )
    assert r.status_code == 200, r.text
    aid = r.json()["data"]["id"]

    until = _future_iso(2)
    sr = authenticated_client.patch(f"/api/articles/{aid}/snooze", json={"until": until})
    assert sr.status_code == 200

    results = search_articles("newsletter", configured_library)
    match = next((row for row in results if row["id"] == aid), None)
    assert match is not None, f"article {aid} not found in search results: {results}"
    assert match["snoozed_until"] is not None


# --- Payload surfacing: snoozed_until must reach the frontend (M3.2 Task 1) --


def test_article_list_payload_carries_snoozed_until(authenticated_client, configured_library):
    _seed_article(
        configured_library, slug="chip", title="Chip", snoozed_until="2099-01-01 00:00:00"
    )
    r = authenticated_client.get("/api/articles", params={"include_snoozed": "true"})
    assert r.status_code == 200
    articles = {a["title"]: a for a in r.json()["data"]}
    assert "snoozed_until" in articles["Chip"]
    assert articles["Chip"]["snoozed_until"] == "2099-01-01 00:00:00"


def test_article_list_payload_carries_null_snoozed_until_when_unset(
    authenticated_client, configured_library
):
    _seed_article(configured_library, slug="plain", title="Plain")
    r = authenticated_client.get("/api/articles")
    articles = {a["title"]: a for a in r.json()["data"]}
    assert articles["Plain"]["snoozed_until"] is None


def test_article_detail_payload_carries_snoozed_until(authenticated_client, configured_library):
    aid = _seed_article(
        configured_library, slug="chip-detail", title="Chip Detail",
        snoozed_until="2099-01-01 00:00:00",
    )
    r = authenticated_client.get(f"/api/articles/{aid}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "snoozed_until" in data
    assert data["snoozed_until"] == "2099-01-01 00:00:00"

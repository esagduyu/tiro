"""M4b: digest GETs are pure cache reads; generation is POST-only."""

from datetime import date

import pytest


def _seed_digests(config, date_str):
    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    try:
        for dt in ("ranked", "by_topic", "by_entity"):
            conn.execute(
                "INSERT INTO digests (date, digest_type, content, article_ids) VALUES (?, ?, ?, ?)",
                (date_str, dt, f"# {dt} digest", "[1, 2]"),
            )
        conn.commit()
    finally:
        conn.close()


def _boom(*a, **k):
    raise AssertionError("generate_digest must not be called by a GET")


CANNED = {
    t: {"content": f"# fresh {t}", "article_ids": [], "created_at": "2026-07-02 10:00:00"}
    for t in ("ranked", "by_topic", "by_entity")
}


def test_get_digest_returns_cached(authenticated_client, configured_library, monkeypatch):
    import tiro.api.routes_digest as rd

    monkeypatch.setattr(rd, "generate_digest", _boom)
    _seed_digests(configured_library, date.today().isoformat())
    r = authenticated_client.get("/api/digest/today")
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is True
    assert body["data"]["ranked"]["content"] == "# ranked digest"


def test_get_digest_404_when_empty_and_never_generates(authenticated_client, monkeypatch):
    import tiro.api.routes_digest as rd

    monkeypatch.setattr(rd, "generate_digest", _boom)
    r = authenticated_client.get("/api/digest/today")
    assert r.status_code == 404
    # Legacy ?refresh=true must be inert, not a generation trigger
    r = authenticated_client.get("/api/digest/today?refresh=true")
    assert r.status_code == 404


def test_post_digest_generates_fresh(authenticated_client, monkeypatch):
    import tiro.api.routes_digest as rd

    calls = {}

    def fake_generate(config, unread_only=False):
        calls["unread_only"] = unread_only
        return CANNED

    monkeypatch.setattr(rd, "generate_digest", fake_generate)
    r = authenticated_client.post("/api/digest/today", json={"unread_only": True})
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is False
    assert body["data"]["ranked"]["content"] == "# fresh ranked"
    assert calls["unread_only"] is True


def test_post_digest_body_optional(authenticated_client, monkeypatch):
    import tiro.api.routes_digest as rd

    calls = {}

    def fake_generate(config, unread_only=False):
        calls["unread_only"] = unread_only
        return CANNED

    monkeypatch.setattr(rd, "generate_digest", fake_generate)
    r = authenticated_client.post("/api/digest/today")  # no body at all
    assert r.status_code == 200
    assert calls["unread_only"] is False


def test_post_digest_maps_errors(authenticated_client, monkeypatch):
    import tiro.api.routes_digest as rd

    monkeypatch.setattr(rd, "generate_digest", _raise(RuntimeError("no api key")))
    assert authenticated_client.post("/api/digest/today").status_code == 503

    monkeypatch.setattr(rd, "generate_digest", _raise(ValueError("no articles")))
    assert authenticated_client.post("/api/digest/today").status_code == 400


def _raise(exc):
    def f(*a, **k):
        raise exc

    return f


def test_get_digest_by_type_cached_and_404(authenticated_client, configured_library, monkeypatch):
    import tiro.api.routes_digest as rd

    monkeypatch.setattr(rd, "generate_digest", _boom)
    r = authenticated_client.get("/api/digest/today/ranked")
    assert r.status_code == 404
    r = authenticated_client.get("/api/digest/today/ranked?refresh=true")
    assert r.status_code == 404

    _seed_digests(configured_library, date.today().isoformat())
    r = authenticated_client.get("/api/digest/today/ranked")
    assert r.status_code == 200
    assert r.json()["data"]["ranked"]["content"] == "# ranked digest"

    # Invalid type still 400
    assert authenticated_client.get("/api/digest/today/bogus").status_code == 400

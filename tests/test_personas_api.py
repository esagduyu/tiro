"""Persona + suggestion routes (Phase 6 K3)."""

import pytest

from tests.test_personas import write_persona
from tests.test_suggestions import _mk_suggestion, _seed_article


@pytest.fixture
def api(authenticated_client):
    return authenticated_client


def _cfg(client):
    return client.app.state.config


def test_list_personas_valid_broken_disabled(api):
    write_persona(_cfg(api), "mine")
    write_persona(_cfg(api), "busted", scope="galaxy")
    r = api.get("/api/personas")
    assert r.status_code == 200
    data = {p["slug"]: p for p in r.json()["data"]}
    assert data["mine"]["enabled"] is True and data["mine"]["error"] is None
    assert data["busted"]["error"] and "scope" in data["busted"]["error"]
    # packaged defaults were ensured
    assert "devils-advocate" in data
    assert data["mine"]["path"].endswith("personas/mine.md")


def test_disable_enable_roundtrip_persists(api):
    write_persona(_cfg(api), "mine")
    assert api.post("/api/personas/mine/disable").status_code == 200
    data = {p["slug"]: p for p in api.get("/api/personas").json()["data"]}
    assert data["mine"]["enabled"] is False
    assert "mine" in _cfg(api).personas_disabled
    assert api.post("/api/personas/mine/enable").status_code == 200
    assert "mine" not in _cfg(api).personas_disabled
    assert api.post("/api/personas/ghost/disable").status_code == 404


def test_suggestions_list_filters(api):
    aid, _ = _seed_article(_cfg(api), title="Route Article")
    _mk_suggestion(_cfg(api), "note", {"article_id": aid, "markdown": "m"})
    _mk_suggestion(_cfg(api), "digest_section",
                   {"title": "t", "markdown": "d"})
    body = api.get("/api/suggestions?status=pending").json()["data"]
    assert len(body["suggestions"]) == 2
    only = api.get(f"/api/suggestions?article_id={aid}").json()["data"]
    assert len(only["suggestions"]) == 1
    assert api.get("/api/suggestions?status=bogus").status_code == 400


def test_accept_applies_then_resolves(api):
    aid, _ = _seed_article(_cfg(api), title="Accept Target")
    s = _mk_suggestion(_cfg(api), "note",
                       {"article_id": aid, "markdown": "Apply me."})
    r = api.post(f"/api/suggestions/{s['uid']}/accept")
    assert r.status_code == 200 and r.json()["success"] is True
    # already resolved -> 409; unknown -> 404
    assert api.post(f"/api/suggestions/{s['uid']}/accept").status_code == 409
    assert api.post("/api/suggestions/01NOPE/accept").status_code == 404
    # the validated write actually happened
    notes = api.get(f"/api/articles/{aid}/annotations").json()["data"]
    assert notes["note"] and "Apply me." in notes["note"]["body_markdown"]


def test_accept_apply_failure_leaves_pending(api):
    s = _mk_suggestion(_cfg(api), "digest_section",
                       {"title": "t", "markdown": "d"})   # no digest today
    r = api.post(f"/api/suggestions/{s['uid']}/accept")
    assert r.status_code == 409
    from tiro.suggestions import get_suggestion

    assert get_suggestion(_cfg(api), s["uid"])["status"] == "pending"


def test_dismiss(api):
    aid, _ = _seed_article(_cfg(api), title="Dismiss Target")
    s = _mk_suggestion(_cfg(api), "note",
                       {"article_id": aid, "markdown": "no thanks"})
    assert api.post(f"/api/suggestions/{s['uid']}/dismiss").status_code == 200
    assert api.post(f"/api/suggestions/{s['uid']}/dismiss").status_code == 409

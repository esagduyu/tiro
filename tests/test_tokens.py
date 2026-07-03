"""API token lifecycle: helpers, routes, auth gating."""

from tiro import auth


def test_list_and_revoke_helpers(configured_library):
    db = configured_library.db_path
    raw = auth.create_api_token(db, "laptop")
    tokens = auth.list_api_tokens(db)
    assert len(tokens) == 1
    assert tokens[0]["name"] == "laptop"
    assert "token_hash" not in tokens[0]  # hashes never leave the helper

    assert auth.revoke_api_token(db, tokens[0]["id"]) is True
    assert auth.list_api_tokens(db) == []
    assert not auth.validate_api_token(db, raw)  # revoked token stops working
    assert auth.revoke_api_token(db, 999) is False


def test_token_routes_lifecycle(authenticated_client):
    r = authenticated_client.post("/api/tokens", json={"name": "chrome"})
    assert r.status_code == 200
    created = r.json()["data"]
    assert created["name"] == "chrome"
    assert len(created["token"]) > 30  # raw token shown once

    r = authenticated_client.get("/api/tokens")
    listed = r.json()["data"]
    assert len(listed) == 1
    assert "token" not in listed[0] and "token_hash" not in listed[0]

    r = authenticated_client.delete(f"/api/tokens/{listed[0]['id']}")
    assert r.status_code == 200
    assert authenticated_client.get("/api/tokens").json()["data"] == []


def test_token_routes_404_on_unknown_delete(authenticated_client):
    assert authenticated_client.delete("/api/tokens/999").status_code == 404


def test_token_routes_require_auth(auth_client):
    assert auth_client.get("/api/tokens").status_code == 401
    assert auth_client.post("/api/tokens", json={"name": "x"}).status_code == 401


def test_created_token_works_as_bearer(authenticated_client, configured_library):
    raw = authenticated_client.post("/api/tokens", json={"name": "cli"}).json()["data"]["token"]
    from fastapi.testclient import TestClient
    from tiro.app import create_app

    with TestClient(create_app(configured_library)) as c:
        r = c.get("/api/articles", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200

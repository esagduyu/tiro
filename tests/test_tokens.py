"""API token lifecycle: helpers, routes, auth gating."""

import argparse

from tiro import auth
from tiro.config import load_config


def _token_args(cfg_path, command, **kw):
    return argparse.Namespace(config=str(cfg_path), token_command=command, **kw)


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


def test_token_cli_lifecycle(tmp_path, capsys):
    from tiro.cli import cmd_token
    from tiro.database import init_db

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"library_path: {tmp_path / 'lib'}\n")
    init_db(load_config(cfg_file).db_path)

    cmd_token(_token_args(cfg_file, "create", name="phone"))
    out = capsys.readouterr().out
    assert "phone" in out
    raw = [line for line in out.splitlines() if line.strip().startswith("Token:")][0].split()[-1]
    assert len(raw) > 30

    cmd_token(_token_args(cfg_file, "list"))
    out = capsys.readouterr().out
    assert "phone" in out
    assert raw not in out  # raw token never shown again

    db = load_config(cfg_file).db_path
    tid = auth.list_api_tokens(db)[0]["id"]
    cmd_token(_token_args(cfg_file, "revoke", id=tid))
    assert auth.list_api_tokens(db) == []

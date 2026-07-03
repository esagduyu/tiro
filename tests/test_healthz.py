"""M6: /healthz detail gating + tiro status + version single-sourcing."""

import pytest


def test_healthz_unauthenticated_is_minimal(auth_client):
    r = auth_client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "stores" not in body  # no store-size leak on the open endpoint
    assert "tasks" not in body


def test_healthz_authenticated_has_detail(authenticated_client):
    r = authenticated_client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["stores"]["articles"] == 0
    assert body["stores"]["db_bytes"] > 0
    assert isinstance(body["uptime_seconds"], int)
    assert set(body["tasks"]) == {"imap", "digest", "vector_retry"}


def test_healthz_no_password_has_detail(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert "stores" in r.json()


def test_version_single_source():
    import tiro
    from tiro.app import create_app  # noqa: F401 — import proves no circularity

    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert tiro.__version__ == pyproject["project"]["version"]


def test_cli_status(initialized_library, capsys):
    from types import SimpleNamespace

    from tiro import cli

    cli.cmd_status(SimpleNamespace(config="unused", _config_override=initialized_library))
    out = capsys.readouterr().out
    assert "Articles:" in out
    assert "0" in out


def test_cli_status_no_library_exits_with_friendly_message(test_config, capsys):
    """`tiro status` against a config whose db_path doesn't exist (no `tiro init`
    run yet) must exit 1 with a friendly message, not crash."""
    from types import SimpleNamespace

    from tiro import cli

    assert not test_config.db_path.exists()

    with pytest.raises(SystemExit) as exc:
        cli.cmd_status(SimpleNamespace(config="unused", _config_override=test_config))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "No library found" in out

"""Sync S1: reconcile scheduler registration + tiro reconcile CLI."""
import json

import pytest
from fastapi.testclient import TestClient

from tiro.app import create_app
from tiro.database import get_connection
from tiro.ingestion.processor import process_article


def _ingest(config, title="Hello World", body="# Hello\n\nSome body text.",
            url="https://example.com/hello"):
    """Local copy of tests/test_reconcile.py's helper — tests/ is not an
    importable package, so don't import across test modules."""
    return process_article(
        title=title, author="A. Writer", content_md=body, url=url, config=config,
    )


def test_reconcile_loop_registered_by_default(configured_library):
    app = create_app(configured_library)
    with TestClient(app, base_url="http://localhost"):
        assert getattr(app.state, "reconcile_task", None) is not None
        assert "reconcile" in app.state.scheduler.periodic_status()


def test_reconcile_loop_off_at_zero(configured_library):
    configured_library.reconcile_interval_s = 0
    app = create_app(configured_library)
    with TestClient(app, base_url="http://localhost"):
        assert getattr(app.state, "reconcile_task", None) is None


def test_config_default_is_30(test_config):
    assert test_config.reconcile_interval_s == 30


class _Args:
    def __init__(self, config=None, dry_run=False, json=False):
        self.config = config
        self.dry_run = dry_run
        self.json = json


def test_cli_reconcile_runs_one_pass(initialized_library, capsys, monkeypatch):
    import tiro.sync.reconcile as rec
    from tiro.cli import cmd_reconcile

    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)
    _ingest(initialized_library)
    (initialized_library.articles_dir / "cli-new.md").write_text("# CLI\n\nNew file.")

    args = _Args(json=True)
    args._config_override = initialized_library
    with pytest.raises(SystemExit) as exc:
        cmd_reconcile(args)
    assert exc.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ingested"] == 1
    conn = get_connection(initialized_library.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        assert n == 2
    finally:
        conn.close()


def test_cli_reconcile_dry_run_acts_on_nothing(initialized_library, capsys, monkeypatch):
    import tiro.sync.reconcile as rec
    from tiro.cli import cmd_reconcile

    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)
    (initialized_library.articles_dir / "dry-cli.md").write_text("# Dry\n\nBody.")
    args = _Args(dry_run=True, json=True)
    args._config_override = initialized_library
    with pytest.raises(SystemExit) as exc:
        cmd_reconcile(args)
    assert exc.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ingested"] == 1
    conn = get_connection(initialized_library.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"] == 0
    finally:
        conn.close()

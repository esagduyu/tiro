"""Tests for the TIRO_* environment variable config overlay.

Precedence: env > yaml > defaults. Overlay lives inside load_config(),
applied after the YAML load and before the ANTHROPIC_API_KEY/OPENAI_API_KEY
env-sync step (tiro/config.py).
"""

from pathlib import Path


def test_env_overrides_yaml(tmp_path, monkeypatch):
    from tiro.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("port: 8000\nimap_enabled: false\ndecay_threshold: 0.1\n")
    monkeypatch.setenv("TIRO_PORT", "9001")
    monkeypatch.setenv("TIRO_IMAP_ENABLED", "true")
    monkeypatch.setenv("TIRO_DECAY_THRESHOLD", "0.25")
    monkeypatch.setenv("TIRO_LIBRARY_PATH", str(tmp_path / "lib"))
    config = load_config(cfg_file)
    assert config.port == 9001
    assert config.imap_enabled is True
    assert config.decay_threshold == 0.25
    assert config.library_path == str(tmp_path / "lib")


def test_env_bool_falsy_values(tmp_path, monkeypatch):
    from tiro.config import load_config

    monkeypatch.setenv("TIRO_IMAP_ENABLED", "0")
    config = load_config(tmp_path / "none.yaml")
    assert config.imap_enabled is False


def test_env_unset_leaves_yaml_value(tmp_path, monkeypatch):
    """An env var that isn't set must not clobber the YAML-provided value."""
    from tiro.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("port: 9500\n")
    monkeypatch.delenv("TIRO_PORT", raising=False)
    config = load_config(cfg_file)
    assert config.port == 9500


def test_env_unset_leaves_defaults(tmp_path, monkeypatch):
    """No YAML and no env for a field: dataclass default wins."""
    from tiro.config import load_config

    monkeypatch.delenv("TIRO_PORT", raising=False)
    config = load_config(tmp_path / "none.yaml")
    assert config.port == 8000


def test_env_bool_case_insensitive_truthy(tmp_path, monkeypatch):
    from tiro.config import load_config

    for truthy in ("1", "true", "TRUE", "yes", "YES", "on", "On"):
        monkeypatch.setenv("TIRO_IMAP_ENABLED", truthy)
        config = load_config(tmp_path / "none.yaml")
        assert config.imap_enabled is True, f"{truthy!r} should be truthy"


def test_env_bool_other_values_are_falsy(tmp_path, monkeypatch):
    from tiro.config import load_config

    for falsy in ("false", "no", "off", "nope", ""):
        monkeypatch.setenv("TIRO_IMAP_ENABLED", falsy)
        config = load_config(tmp_path / "none.yaml")
        assert config.imap_enabled is False, f"{falsy!r} should be falsy"


def test_env_int_coercion(tmp_path, monkeypatch):
    from tiro.config import load_config

    monkeypatch.setenv("TIRO_VECTOR_RETRY_INTERVAL", "42")
    config = load_config(tmp_path / "none.yaml")
    assert config.vector_retry_interval == 42
    assert isinstance(config.vector_retry_interval, int)


def test_env_str_field_verbatim(tmp_path, monkeypatch):
    from tiro.config import load_config

    monkeypatch.setenv("TIRO_ANTHROPIC_API_KEY", "sk-ant-from-env")
    config = load_config(tmp_path / "none.yaml")
    assert config.anthropic_api_key == "sk-ant-from-env"


def test_config_path_field_is_not_overlayable(tmp_path, monkeypatch):
    """config_path is set by load_config itself and excluded from the overlay
    — TIRO_CONFIG_PATH must never override it."""
    from tiro.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("port: 8000\n")
    monkeypatch.setenv("TIRO_CONFIG_PATH", "/should/not/apply")
    config = load_config(cfg_file)
    assert config.config_path == str(cfg_file)


def test_run_py_config_path_honors_tiro_config(monkeypatch, tmp_path):
    """run.py's _config_path() must mirror tiro/mcp/server.py's: honor
    TIRO_CONFIG (absolute path) instead of always defaulting to
    ./config.yaml. Regression test for a real trap that bit a prior
    session (run.py silently ignored TIRO_CONFIG while the MCP server
    honored it)."""
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run = importlib.import_module("run")

    monkeypatch.delenv("TIRO_CONFIG", raising=False)
    assert run._config_path() == "config.yaml"

    cfg = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("TIRO_CONFIG", str(cfg))
    assert run._config_path() == str(cfg)


def test_cli_config_default_honors_tiro_config(monkeypatch, tmp_path):
    """cli.py's `--config` default must honor TIRO_CONFIG (absolute path),
    mirroring run.py's and tiro/mcp/server.py's _config_path() (Finding 2,
    M2.3 final review -- cli.py was the one remaining place that silently
    ignored TIRO_CONFIG, the same footgun that already bit run.py once).
    An explicit --config must still win over the env var."""
    import sys

    from tiro import cli

    captured = {}
    monkeypatch.setattr(cli, "cmd_status", lambda args: captured.update(config=args.config))

    cfg = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("TIRO_CONFIG", str(cfg))
    monkeypatch.setattr(sys, "argv", ["tiro", "status"])
    cli.main()
    assert captured["config"] == str(cfg)

    # explicit --config wins over TIRO_CONFIG even when the env var is set
    captured.clear()
    monkeypatch.setattr(sys, "argv", ["tiro", "--config", "explicit.yaml", "status"])
    cli.main()
    assert captured["config"] == "explicit.yaml"

    # no TIRO_CONFIG set: falls back to the historical "config.yaml" default
    captured.clear()
    monkeypatch.delenv("TIRO_CONFIG", raising=False)
    monkeypatch.setattr(sys, "argv", ["tiro", "status"])
    cli.main()
    assert captured["config"] == "config.yaml"


def test_env_overlay_applied_before_api_key_sync(tmp_path, monkeypatch):
    """The overlay must run before the ANTHROPIC_API_KEY env-sync so that an
    env-provided anthropic_api_key still gets synced to os.environ."""
    from tiro.config import load_config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("TIRO_ANTHROPIC_API_KEY", "sk-ant-overlay-value")
    load_config(tmp_path / "none.yaml")
    import os

    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-overlay-value"

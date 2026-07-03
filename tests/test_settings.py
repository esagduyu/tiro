"""M5: comment-preserving atomic config persistence + settings routes."""

import os
from pathlib import Path

import pytest

from tiro.config import TiroConfig, load_config, persist_config


def _write_cfg(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "# Tiro configuration\n"
        'library_path: "./lib"  # keep me\n'
        "port: 8000\n"
    )
    return cfg_file


def test_persist_config_preserves_comments_and_merges(tmp_path):
    cfg_file = _write_cfg(tmp_path)
    cfg = load_config(cfg_file)
    persist_config(cfg, {"tts_voice": "fable", "port": 9000})
    text = cfg_file.read_text()
    assert "# Tiro configuration" in text
    assert "# keep me" in text
    assert "tts_voice: fable" in text
    assert "port: 9000" in text


def test_persist_config_atomic_and_0600(tmp_path):
    cfg_file = _write_cfg(tmp_path)
    cfg = load_config(cfg_file)
    persist_config(cfg, {"digest_email": "a@b.c"})
    assert (cfg_file.stat().st_mode & 0o777) == 0o600
    assert not cfg_file.with_suffix(".yaml.tmp").exists()


def test_persist_config_requires_config_path(tmp_path):
    cfg = TiroConfig(library_path=str(tmp_path / "lib"))  # config_path is None
    with pytest.raises(ValueError):
        persist_config(cfg, {"port": 9000})


def test_persist_config_creates_missing_file(tmp_path):
    cfg = TiroConfig(library_path=str(tmp_path / "lib"))
    cfg.config_path = str(tmp_path / "new-config.yaml")
    persist_config(cfg, {"theme_light": "papyrus"})
    reloaded = load_config(tmp_path / "new-config.yaml")
    assert reloaded.theme_light == "papyrus"

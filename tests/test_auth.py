"""Auth spine tests: config fields, hashing, sessions, tokens, routes, enforcement."""

from pathlib import Path

from tiro.config import TiroConfig, load_config


def test_config_has_auth_fields_default_none(test_config):
    assert test_config.auth_password_hash is None


def test_load_config_records_its_path(tmp_path):
    cfg_file = tmp_path / "custom.yaml"
    cfg_file.write_text("library_path: ./lib\nauth_password_hash: dummy-hash\n")
    cfg = load_config(cfg_file)
    assert cfg.auth_password_hash == "dummy-hash"
    assert Path(cfg.config_path) == cfg_file


def test_load_config_records_path_even_when_missing(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert Path(cfg.config_path) == tmp_path / "nonexistent.yaml"

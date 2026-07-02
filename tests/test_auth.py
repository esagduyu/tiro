"""Auth spine tests: config fields, hashing, sessions, tokens, routes, enforcement."""

from pathlib import Path

import pytest

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


from tiro import auth
from tiro.database import get_connection, init_db


def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse")
    assert h != "correct horse"
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)


def test_session_lifecycle(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    token = auth.create_session(db)
    assert auth.validate_session(db, token)
    assert not auth.validate_session(db, "forged-token")
    auth.destroy_session(db, token)
    assert not auth.validate_session(db, token)


def test_session_expiry_and_sliding_renewal(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    token = auth.create_session(db)
    token_hash = auth._sha256(token)

    conn = get_connection(db)
    try:
        # Simulate an expired session
        conn.execute(
            "UPDATE sessions SET expires_at = datetime('now', '-1 day') WHERE token_hash = ?",
            (token_hash,),
        )
        conn.commit()
    finally:
        conn.close()
    assert not auth.validate_session(db, token)

    # Fresh session, aged 10 days: validation must slide expiry forward
    token2 = auth.create_session(db)
    t2_hash = auth._sha256(token2)
    conn = get_connection(db)
    try:
        conn.execute(
            "UPDATE sessions SET expires_at = datetime('now', '+20 days') WHERE token_hash = ?",
            (t2_hash,),
        )
        conn.commit()
    finally:
        conn.close()
    assert auth.validate_session(db, token2)
    conn = get_connection(db)
    try:
        row = conn.execute(
            "SELECT expires_at > datetime('now', '+29 days') AS slid FROM sessions WHERE token_hash = ?",
            (t2_hash,),
        ).fetchone()
    finally:
        conn.close()
    assert row["slid"] == 1


def test_api_token_lifecycle(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    raw = auth.create_api_token(db, "chrome-extension")
    assert auth.validate_api_token(db, raw)
    assert not auth.validate_api_token(db, "forged")
    conn = get_connection(db)
    try:
        row = conn.execute("SELECT name, token_hash FROM api_tokens").fetchone()
    finally:
        conn.close()
    assert row["name"] == "chrome-extension"
    assert row["token_hash"] != raw  # stored hashed, never plaintext


def test_save_password_hash_preserves_comments(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "# Tiro Configuration\n"
        "library_path: \"./tiro-library\"  # where articles live\n"
        "port: 8000\n"
    )
    from tiro.config import load_config

    cfg = load_config(cfg_file)
    auth.save_password_hash(cfg, "bcrypt-hash-here")
    text = cfg_file.read_text()
    assert "# Tiro Configuration" in text          # comments preserved
    assert "# where articles live" in text
    assert "auth_password_hash: bcrypt-hash-here" in text
    assert cfg.auth_password_hash == "bcrypt-hash-here"  # in-memory updated


def test_save_password_hash_failure_leaves_config_intact(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    original = "# precious comments\nlibrary_path: \"./tiro-library\"\n"
    cfg_file.write_text(original)
    cfg = load_config(cfg_file)

    from ruamel.yaml import YAML

    def boom(self, data, stream):
        raise OSError("disk full")

    monkeypatch.setattr(YAML, "dump", boom)
    with pytest.raises(OSError):
        auth.save_password_hash(cfg, "hash")
    assert cfg_file.read_text() == original  # untouched
    assert not cfg_file.with_suffix(".yaml.tmp").exists()  # no litter

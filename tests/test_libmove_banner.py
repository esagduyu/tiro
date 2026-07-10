"""Legacy-library-path suggestion banner (spec D3). A dismissible banner
appears when the effective library_path resolves to the legacy CWD-relative
`./tiro-library` default AND the platform default location is not in use. It
names `tiro migrate-library` and NEVER offers a web migrate action.
"""

from fastapi.testclient import TestClient

from tiro import auth as tiro_auth
from tiro.app import create_app
from tiro.config import TiroConfig
from tiro.database import init_db, migrate_db
from tiro.vectorstore import init_vectorstore

TEST_PASSWORD = "test-password-123"


def _build(config):
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)
    config.auth_password_hash = tiro_auth.hash_password(TEST_PASSWORD)


def _login(client):
    r = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200


def test_banner_present_at_legacy_default(tmp_path, monkeypatch, _shared_embeddings):
    # _isolate_cwd (autouse) has chdir'd into tmp_path, so "./tiro-library"
    # resolves to tmp_path/tiro-library.
    monkeypatch.setattr(
        "tiro.app.platform_default_library", lambda: tmp_path / "PlatformElsewhere"
    )
    config = TiroConfig(library_path="./tiro-library")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('library_path: "./tiro-library"\n')
    config.config_path = str(cfg_file)
    _build(config)

    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as c:
        _login(c)
        html = c.get("/inbox").text
    assert "migrate-library" in html
    assert "libmove" in html  # the banner element id/class


def test_banner_absent_at_platform_path(tmp_path, monkeypatch, _shared_embeddings):
    lib = tmp_path / "PlatformTiro"
    monkeypatch.setattr("tiro.app.platform_default_library", lambda: lib)
    config = TiroConfig(library_path=str(lib))
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f'library_path: "{lib}"\n')
    config.config_path = str(cfg_file)
    _build(config)

    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as c:
        _login(c)
        html = c.get("/inbox").text
    # zero DOM for the banner when not at the legacy default
    assert "libmove-banner" not in html


def test_banner_absent_at_custom_path(tmp_path, monkeypatch, _shared_embeddings):
    monkeypatch.setattr(
        "tiro.app.platform_default_library", lambda: tmp_path / "PlatformElsewhere"
    )
    lib = tmp_path / "my-custom-library"
    config = TiroConfig(library_path=str(lib))
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f'library_path: "{lib}"\n')
    config.config_path = str(cfg_file)
    _build(config)

    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as c:
        _login(c)
        html = c.get("/inbox").text
    assert "libmove-banner" not in html

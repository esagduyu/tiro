"""Shared pytest fixtures for Tiro tests.

Every fixture is isolated: temp library, temp SQLite, temp ChromaDB.
Nothing reads the developer's ./tiro-library or ./config.yaml.
"""

import pytest
from fastapi.testclient import TestClient

from tiro.config import TiroConfig


@pytest.fixture(autouse=True)
def _no_external_apis(monkeypatch):
    # Tests are offline and deterministic. extract_metadata()
    # (tiro/ingestion/extractors.py) skips AI when the key is unset.
    # direnv sets these in dev shells, so delete explicitly.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(scope="session")
def shared_embedding_fn():
    # Loading all-MiniLM-L6-v2 takes seconds; share one instance per session.
    from chromadb.utils.embedding_functions import (
        SentenceTransformerEmbeddingFunction,
    )

    return SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")


@pytest.fixture
def _shared_embeddings(shared_embedding_fn, monkeypatch):
    # init_vectorstore() constructs a fresh embedding function each call;
    # patch the constructor so every call reuses the session instance.
    import chromadb.utils.embedding_functions as ef_mod

    monkeypatch.setattr(
        ef_mod,
        "SentenceTransformerEmbeddingFunction",
        lambda model_name: shared_embedding_fn,
    )


@pytest.fixture
def test_config(tmp_path) -> TiroConfig:
    return TiroConfig(library_path=str(tmp_path / "library"))


@pytest.fixture
def initialized_library(test_config, _shared_embeddings) -> TiroConfig:
    # Pre-create every store BEFORE the app starts. This mirrors `tiro init`,
    # the documented workaround for ChromaDB's "readonly database" error when
    # a collection is first created inside a running server (CLAUDE.md).
    from tiro.database import init_db, migrate_db
    from tiro.vectorstore import init_vectorstore

    test_config.articles_dir.mkdir(parents=True, exist_ok=True)
    (test_config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(test_config.db_path)
    migrate_db(test_config.db_path)
    init_vectorstore(test_config.chroma_dir, test_config.default_embedding_model)
    return test_config


@pytest.fixture
def client(initialized_library):
    from tiro.app import create_app

    app = create_app(initialized_library)
    # Context manager runs the lifespan (store init, background tasks, shutdown).
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    # Several routes write CWD-relative Path("config.yaml") (routes_settings.py).
    # Chdir into the per-test tmp dir so no test can ever touch the developer's
    # real ./config.yaml. Templates/static resolve via package __file__ paths,
    # so chdir is safe. Proper fix (config path on app.state) lands in M5.
    monkeypatch.chdir(tmp_path)


TEST_PASSWORD = "test-password-123"


@pytest.fixture
def configured_library(tmp_path, _shared_embeddings):
    """Library with a password configured (hash precomputed for speed).

    Deliberately does NOT depend on the `initialized_library` fixture: pytest
    caches fixtures by name per test invocation, so any test requesting both
    this fixture (indirectly, via auth_client) and the plain `client` fixture
    would have them collapse onto the same TiroConfig instance — mutating
    auth_password_hash here would leak into `client` too. Building an
    independent library in its own tmp_path subdir keeps the two isolated.
    """
    from tiro import auth as tiro_auth
    from tiro.config import TiroConfig
    from tiro.database import init_db, migrate_db
    from tiro.vectorstore import init_vectorstore

    config = TiroConfig(library_path=str(tmp_path / "auth-library"))
    cfg_file = tmp_path / "auth-config.yaml"
    cfg_file.write_text(f'library_path: "{tmp_path / "auth-library"}"\n')
    config.config_path = str(cfg_file)
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)
    config.auth_password_hash = tiro_auth.hash_password(TEST_PASSWORD)
    return config


@pytest.fixture
def auth_client(configured_library):
    """Client against a password-configured app; NOT logged in.

    follow_redirects=False: page routes gate via a 302-to-/login redirect
    (not a 401), so probing with redirects followed would land on the
    (intentionally open) login page and mask an unprotected route as a
    false 200. Tests that want the followed response opt in explicitly.
    """
    from tiro.app import create_app

    app = create_app(configured_library)
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def authenticated_client(auth_client):
    """Client with a live session cookie (logged in)."""
    r = auth_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200
    return auth_client

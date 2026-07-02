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

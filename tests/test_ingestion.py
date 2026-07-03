"""Atomic ingestion: staged pipeline with rollback, no orphans on failure."""

from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "newsletter.eml"


def _extracted():
    from tiro.ingestion.email import parse_eml

    return parse_eml(FIXTURE.read_bytes())


def test_happy_path_sets_vector_status_indexed(initialized_library):
    from tiro.database import get_connection
    from tiro.ingestion.processor import process_article

    ex = _extracted()
    result = process_article(**ex, config=initialized_library, ingestion_method="email")
    conn = get_connection(initialized_library.db_path)
    try:
        vs = conn.execute("SELECT vector_status FROM articles WHERE id = ?",
                          (result["id"],)).fetchone()["vector_status"]
    finally:
        conn.close()
    assert vs == "indexed"


def test_chromadb_failure_is_nonfatal_marks_pending(initialized_library, monkeypatch):
    from tiro.database import get_connection
    from tiro.ingestion import processor

    # Make the ChromaDB add blow up; ingestion must still succeed
    class BoomCollection:
        def add(self, *a, **k):
            raise RuntimeError("chroma down")

        def upsert(self, *a, **k):
            raise RuntimeError("chroma down")

    monkeypatch.setattr(processor, "get_collection", lambda: BoomCollection())
    ex = _extracted()
    result = processor.process_article(**ex, config=initialized_library, ingestion_method="email")
    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute("SELECT vector_status, markdown_path FROM articles WHERE id = ?",
                           (result["id"],)).fetchone()
    finally:
        conn.close()
    assert row["vector_status"] == "pending"
    assert (initialized_library.articles_dir / row["markdown_path"]).exists()  # article intact


def test_failure_after_insert_rolls_back_no_orphan(initialized_library, monkeypatch):
    from tiro.database import get_connection
    from tiro.ingestion import processor

    # Fail at the metadata/frontmatter update stage (after row + file exist)
    def boom(*a, **k):
        raise RuntimeError("stage failure")

    monkeypatch.setattr(processor, "extract_metadata", boom)
    ex = _extracted()
    before = _count_articles(initialized_library)
    with pytest.raises(RuntimeError):
        processor.process_article(**ex, config=initialized_library, ingestion_method="email")
    # No orphan row, no orphan file
    assert _count_articles(initialized_library) == before
    stray = list(initialized_library.articles_dir.glob("*.md"))
    assert stray == [], f"orphan markdown left: {stray}"


def test_failure_after_tag_commit_rolls_back_junctions(initialized_library, monkeypatch):
    """Fail at the post-metadata frontmatter rewrite, which runs AFTER the
    tag/entity commit (conn.commit() in process_article). Proves
    delete_article() rolls back the already-committed article_tags rows,
    not just the article row + file.

    extract_metadata() is monkeypatched to return real tags/entities
    (tests run with ANTHROPIC_API_KEY unset, where it would otherwise
    return empty defaults and the article_tags commit would be a no-op —
    that would make this test pass trivially without exercising anything).
    """
    from tiro.database import get_connection
    from tiro.ingestion import processor

    monkeypatch.setattr(
        processor,
        "extract_metadata",
        lambda title, content, config: {
            "tags": ["testing", "rollback"],
            "entities": [{"name": "Tiro", "type": "product"}],
            "summary": "A test summary.",
        },
    )

    original_dumps = processor.frontmatter.dumps
    call_count = {"n": 0}

    def boom(post, *a, **k):
        call_count["n"] += 1
        if call_count["n"] == 2:
            # First call is the pre-enrichment frontmatter write (before
            # the tag commit); second call is the post-metadata rewrite
            # (after tags/entities are committed) — fail there.
            raise RuntimeError("frontmatter rewrite failure")
        return original_dumps(post, *a, **k)

    monkeypatch.setattr(processor.frontmatter, "dumps", boom)
    ex = _extracted()
    before = _count_articles(initialized_library)
    with pytest.raises(RuntimeError):
        processor.process_article(**ex, config=initialized_library, ingestion_method="email")

    # No orphan row, no orphan file
    assert _count_articles(initialized_library) == before
    stray = list(initialized_library.articles_dir.glob("*.md"))
    assert stray == [], f"orphan markdown left: {stray}"

    # Committed junction rows (article_tags/article_entities) must also
    # have been rolled back, proving delete_article() unwinds more than
    # just the article row + file.
    conn = get_connection(initialized_library.db_path)
    try:
        n_tags = conn.execute("SELECT COUNT(*) AS n FROM article_tags").fetchone()["n"]
        n_entities = conn.execute("SELECT COUNT(*) AS n FROM article_entities").fetchone()["n"]
    finally:
        conn.close()
    assert n_tags == 0, "orphan article_tags rows left after rollback"
    assert n_entities == 0, "orphan article_entities rows left after rollback"


def _count_articles(config):
    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    finally:
        conn.close()


def test_retry_pending_vectors_indexes_them(initialized_library, monkeypatch):
    from tiro.database import get_connection
    from tiro.ingestion import processor

    class BoomOnce:
        def add(self, *a, **k):
            raise RuntimeError("down")

        def upsert(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(processor, "get_collection", lambda: BoomOnce())
    ex = _extracted()
    result = processor.process_article(**ex, config=initialized_library, ingestion_method="email")
    # It's pending now
    from tiro.vectorstore import retry_pending_vectors, get_collection

    n = retry_pending_vectors(initialized_library)
    assert n == 1
    conn = get_connection(initialized_library.db_path)
    try:
        vs = conn.execute("SELECT vector_status FROM articles WHERE id = ?",
                          (result["id"],)).fetchone()["vector_status"]
    finally:
        conn.close()
    assert vs == "indexed"
    assert get_collection().get(ids=[f"article_{result['id']}"])["ids"]

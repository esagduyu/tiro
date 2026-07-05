"""import_bundle: export round-trip + conflict modes."""

import pytest

from tiro.database import get_connection
from tiro.export import export_library
from tiro.importer import import_bundle


def _seed(config, *, title="T1", slug="art-1", uid="01AAAAAAAAAAAAAAAAAAAAAAAA",
          url="https://x.com/a", rating=None):
    conn = get_connection(config.db_path)
    conn.execute("INSERT OR IGNORE INTO sources (name, source_type) VALUES ('Src', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path, url, rating)"
        " VALUES (?, 1, ?, ?, ?, ?, ?)",
        (uid, title, slug, f"{slug}.md", url, rating),
    )
    conn.execute("INSERT OR IGNORE INTO tags (uid, name) VALUES ('01TAG000000000000000000000', 'ai')")
    conn.execute("INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (1, 1)")
    conn.commit()
    conn.close()
    (config.articles_dir / f"{slug}.md").write_text(f"---\ntitle: {title}\n---\nbody of {slug}")


def _fresh_library(tmp_path):
    """Second, empty library to import into. Import only writes SQLite +
    markdown (no ChromaDB/embedding calls), so unlike `initialized_library`
    this doesn't need `_shared_embeddings`/init_vectorstore."""
    from tiro.config import TiroConfig
    from tiro.database import init_db

    lib = tmp_path / "lib2"
    config = TiroConfig(library_path=str(lib))
    config.articles_dir.mkdir(parents=True)
    init_db(config.db_path)
    return config


def test_round_trip_into_empty_library(initialized_library, tmp_path):
    config = initialized_library
    _seed(config, rating=2)
    bundle = export_library(config)
    try:
        target = _fresh_library(tmp_path)
        result = import_bundle(target, bundle)
        assert result["imported"] == 1 and result["skipped"] == 0
        conn = get_connection(target.db_path)
        row = conn.execute(
            "SELECT a.title, a.uid, a.rating, a.vector_status, s.name AS src"
            " FROM articles a JOIN sources s ON a.source_id = s.id"
        ).fetchone()
        tag = conn.execute(
            "SELECT t.name FROM tags t JOIN article_tags at ON t.id = at.tag_id"
        ).fetchone()
        conn.close()
        assert row["title"] == "T1" and row["uid"] == "01AAAAAAAAAAAAAAAAAAAAAAAA"
        assert row["rating"] == 2 and row["vector_status"] == "pending"
        assert row["src"] == "Src" and tag["name"] == "ai"
        assert (target.articles_dir / "art-1.md").read_text().endswith("body of art-1")
    finally:
        bundle.unlink()


def test_conflict_skip_and_overwrite(initialized_library, tmp_path):
    config = initialized_library
    _seed(config, rating=1)
    bundle = export_library(config)
    try:
        # same library = guaranteed uid match
        r_skip = import_bundle(config, bundle, conflicts="skip")
        assert r_skip["skipped"] == 1 and r_skip["imported"] == 0

        conn = get_connection(config.db_path)
        conn.execute("UPDATE articles SET rating = -1, title = 'CHANGED'")
        # Local-only tag, not present in the bundle, linked to the article.
        conn.execute(
            "INSERT OR IGNORE INTO tags (uid, name) VALUES ('01TAGLOCAL0000000000000000', 'local-only')"
        )
        conn.execute(
            "INSERT INTO article_tags (article_id, tag_id)"
            " SELECT 1, id FROM tags WHERE name = 'local-only'"
        )
        conn.commit()
        conn.close()

        r_over = import_bundle(config, bundle, conflicts="overwrite")
        assert r_over["overwritten"] == 1
        conn = get_connection(config.db_path)
        row = conn.execute("SELECT title, rating, vector_status FROM articles").fetchone()
        tag_names = {
            r["name"] for r in conn.execute(
                "SELECT t.name FROM tags t JOIN article_tags at ON t.id = at.tag_id"
                " WHERE at.article_id = 1"
            ).fetchall()
        }
        conn.close()
        assert row["title"] == "T1" and row["rating"] == 1
        assert row["vector_status"] == "pending"
        # Overwrite means the bundle's state wins: the local-only tag link
        # is gone (bundle only had 'ai'); the tag row itself may remain.
        assert tag_names == {"ai"}
    finally:
        bundle.unlink()


def test_conflict_keep_both(initialized_library, tmp_path):
    config = initialized_library
    _seed(config)
    bundle = export_library(config)
    try:
        r = import_bundle(config, bundle, conflicts="keep-both")
        assert r["kept_both"] == 1
        conn = get_connection(config.db_path)
        rows = conn.execute("SELECT slug, uid FROM articles ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[1]["slug"] == "art-1-imported"
        assert rows[1]["uid"] != rows[0]["uid"]
        assert (initialized_library.articles_dir / "art-1-imported.md").exists()
    finally:
        bundle.unlink()


def test_invalid_conflicts_mode(initialized_library, tmp_path):
    with pytest.raises(ValueError, match="conflicts"):
        import_bundle(initialized_library, tmp_path / "x.zip", conflicts="merge")

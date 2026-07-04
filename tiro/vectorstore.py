"""ChromaDB vector store initialization and helpers for Tiro."""

import logging
from pathlib import Path

import chromadb

logger = logging.getLogger(__name__)

_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None


def init_vectorstore(
    chroma_dir: Path, embedding_model: str = "all-MiniLM-L6-v2"
) -> chromadb.Collection:
    """Initialize the ChromaDB persistent client and return the tiro_articles collection."""
    global _client, _collection

    chroma_dir.mkdir(parents=True, exist_ok=True)

    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    ef = SentenceTransformerEmbeddingFunction(model_name=embedding_model)

    _client = chromadb.PersistentClient(path=str(chroma_dir))
    _collection = _client.get_or_create_collection(
        name="tiro_articles",
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    logger.info(
        "ChromaDB initialized at %s (%d documents)",
        chroma_dir,
        _collection.count(),
    )
    return _collection


def get_collection() -> chromadb.Collection:
    """Get the tiro_articles collection. Must call init_vectorstore first."""
    if _collection is None:
        raise RuntimeError("Vectorstore not initialized. Call init_vectorstore first.")
    return _collection


def retry_pending_vectors(config) -> int:
    """Re-index articles whose ChromaDB add previously failed. Returns count indexed."""
    import frontmatter

    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.markdown_path, a.published_at, a.ingested_at,
                   s.name AS source_name, s.is_vip
            FROM articles a LEFT JOIN sources s ON a.source_id = s.id
            WHERE a.vector_status = 'pending'
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0
    collection = get_collection()
    indexed = 0
    for row in rows:
        md = config.articles_dir / row["markdown_path"]
        if not md.exists():
            # Markdown file is gone (e.g. deleted mid-retry, or an earlier
            # orphan). Mark it failed so it stops being rescanned every
            # cycle and `tiro doctor` (M5) has a signal to reconcile.
            try:
                conn2 = get_connection(config.db_path)
                try:
                    conn2.execute(
                        "UPDATE articles SET vector_status = 'failed' WHERE id = ?",
                        (row["id"],),
                    )
                    conn2.commit()
                finally:
                    conn2.close()
            except Exception as e:
                logger.error("Failed to mark article %d as vector_status=failed: %s", row["id"], e)
            continue
        try:
            post = frontmatter.load(str(md))
            # Full metadata parity with the initial-ingest upsert in
            # processor.py — re-embeds must not regress to a thinner
            # {title, article_id}-only shape (Phase-0 final-review deferral).
            conn_tags = get_connection(config.db_path)
            try:
                tag_names = [
                    r["name"]
                    for r in conn_tags.execute(
                        "SELECT t.name FROM tags t"
                        " JOIN article_tags at ON t.id = at.tag_id"
                        " WHERE at.article_id = ? ORDER BY t.name",
                        (row["id"],),
                    ).fetchall()
                ]
            finally:
                conn_tags.close()
            pub = (row["published_at"] or row["ingested_at"] or "")[:10]
            # upsert (not add): idempotent if a prior attempt already wrote
            # the vector but crashed before the status UPDATE committed —
            # add() would error on a re-add of an existing id.
            collection.upsert(
                ids=[f"article_{row['id']}"],
                documents=[post.content],
                metadatas=[{
                    "title": row["title"],
                    "source": row["source_name"] or "",
                    "is_vip": row["is_vip"] or 0,
                    "tags": ",".join(tag_names),
                    "published_at": pub,
                    "article_id": row["id"],
                }],
            )
            conn2 = get_connection(config.db_path)
            try:
                cursor = conn2.execute(
                    "UPDATE articles SET vector_status = 'indexed' WHERE id = ?", (row["id"],)
                )
                conn2.commit()
                rowcount = cursor.rowcount
            finally:
                conn2.close()
            if rowcount == 0:
                # Article was deleted mid-retry: the row is gone, so the
                # vector we just (re)added is now an orphan. Best-effort
                # clean it up.
                try:
                    collection.delete(ids=[f"article_{row['id']}"])
                except Exception as e:
                    logger.warning(
                        "Failed to delete orphaned vector for deleted article %d: %s",
                        row["id"], e,
                    )
            else:
                indexed += 1
        except Exception as e:
            logger.error("Vector retry failed for %d: %s", row["id"], e)
    return indexed

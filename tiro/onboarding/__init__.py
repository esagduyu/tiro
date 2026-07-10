"""Packaged first-run sample documents (Phase 5 M5.1, spec D6).

Two public-domain Markdown docs — a "Welcome to Tiro" guide and a
Cicero-to-Tiro letter excerpt — that the onboarding wizard can seed into a
fresh library so the inbox isn't empty on first launch. Ingested through the
completely normal `process_article` pipeline with NO network access (they carry
synthetic `tiro.local` URLs used purely for duplicate detection), so seeding is
offline-safe (a desktop first-run may have no connection) and AI enrichment
degrades gracefully without a key exactly as any offline ingest does today.
"""

import logging
from pathlib import Path

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.ingestion.processor import process_article

logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent

# (filename, title, author, synthetic-url-for-dedup). The URL never hits the
# network — it only gives process_article a source domain and gives the samples
# route a stable key to skip re-ingesting on a second call.
SAMPLE_DOCS = [
    (
        "welcome-to-tiro.md",
        "Welcome to Tiro",
        "Tiro",
        "https://tiro.local/welcome-to-tiro",
    ),
    (
        "cicero-to-tiro.md",
        "Cicero to Tiro — A Letter (53 BC)",
        "Marcus Tullius Cicero",
        "https://tiro.local/cicero-to-tiro",
    ),
]


def seed_samples(config: TiroConfig) -> list[dict]:
    """Ingest any not-yet-present sample docs through the normal pipeline.

    Rides the same URL-based duplicate detection the ingest route uses: a
    sample whose synthetic URL already has an article row is skipped, so a
    second call is a no-op (returns []). Each doc is ingested independently and
    a per-doc failure is logged and skipped rather than aborting the batch.
    Returns the list of freshly-created article metadata dicts.
    """
    created: list[dict] = []
    for filename, title, author, url in SAMPLE_DOCS:
        conn = get_connection(config.db_path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM articles WHERE url = ?", (url,)
            ).fetchone()
        finally:
            conn.close()
        if existing:
            continue
        body = (_DIR / filename).read_text()
        try:
            meta = process_article(
                title=title,
                author=author,
                content_md=body,
                url=url,
                config=config,
                ingestion_method="sample",
            )
            created.append(meta)
        except Exception as e:  # pragma: no cover - defensive
            logger.error("Failed to seed sample %s: %s", filename, e)
    return created

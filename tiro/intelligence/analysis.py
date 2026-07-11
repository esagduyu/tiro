"""On-demand ingenuity/trust analysis using Claude Opus 4.6."""

import json
import logging

from tiro.config import TiroConfig
from tiro.database import get_connection

logger = logging.getLogger(__name__)


def _coerce_score(value, fallback: float = 5.0) -> float:
    """Coerce an Opus-provided score to a float in [0, 10].

    Opus output is untrusted (prompt injection risk) and has zero schema
    validation on the way in — a malicious/broken response could hand us a
    non-numeric or out-of-range "score" that later gets rendered client-side.
    Falls back to a neutral mid score when the value isn't a usable number.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    if n != n:  # NaN check without importing math
        return fallback
    return max(0.0, min(10.0, n))


def _coerce_analysis_scores(analysis: dict) -> dict:
    """Coerce the bias/factual_confidence/novelty dimension scores in-place.

    Shared by both the "freshly generated" and "loaded from cache" paths so
    an analysis blob can never surface a non-numeric or out-of-range score
    to the frontend, regardless of when it was produced (including blobs
    cached before this coercion existed).
    """
    for dimension in ("bias", "factual_confidence", "novelty"):
        if isinstance(analysis.get(dimension), dict) and "score" in analysis[dimension]:
            analysis[dimension]["score"] = _coerce_score(analysis[dimension]["score"])
    return analysis


def get_cached_analysis(config: TiroConfig, article_id: int) -> dict | None:
    """Return cached ingenuity analysis for an article, or None."""
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT ingenuity_analysis FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        if row and row["ingenuity_analysis"]:
            return _coerce_analysis_scores(json.loads(row["ingenuity_analysis"]))
        return None
    finally:
        conn.close()


def _cache_analysis(config: TiroConfig, article_id: int, analysis: dict) -> None:
    """Store analysis JSON in the article's ingenuity_analysis column."""
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "UPDATE articles SET ingenuity_analysis = ? WHERE id = ?",
            (json.dumps(analysis), article_id),
        )
        conn.commit()
    finally:
        conn.close()


def analyze_article(config: TiroConfig, article_id: int) -> dict:
    """Run ingenuity/trust analysis via the ingenuity_analyst agent run.

    Exception surface preserved (ValueError for missing article/file,
    RuntimeError for provider problems) via cause re-raise.
    """
    from tiro.agents.base import AgentRunError
    from tiro.agents.runtime import run_agent

    try:
        res = run_agent(config, "ingenuity_analyst", {"article_id": article_id})
    except AgentRunError as e:
        if e.__cause__ is not None:
            raise e.__cause__  # noqa: B904
        raise
    return res.outputs.analysis

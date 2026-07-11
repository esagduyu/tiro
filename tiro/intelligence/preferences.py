"""Learned preferences — compat wrapper over the PreferenceClassifier agent.

The gather SQL now lives on the agent context
(tiro/agents/context.py: list_rated_articles / list_unrated_articles /
get_vip_names); the orchestration lives in
tiro/agents/builtin/preference_classifier.py. This wrapper preserves the
historical signature + exception surface for routes_classify.py.
"""

from tiro.config import TiroConfig
from tiro.database import get_connection

MAX_UNRATED_FOR_CLASSIFICATION = 50  # cap to avoid enormous prompts
MIN_RATED_ARTICLES = 5  # minimum rated articles needed before classification


def _gather_unrated_articles(config: TiroConfig) -> list[dict]:
    """Gather unrated articles (ai_tier IS NULL) for classification.

    Returns list of dicts with id, title, source, summary.
    Capped at MAX_UNRATED_FOR_CLASSIFICATION.

    Kept here (a byte-identical duplicate of RunContext.list_unrated_
    articles' SQL) as a back-compat direct-import seam —
    tests/test_snooze_api.py imports this name directly and predates the
    agent runtime; classify_articles itself no longer calls it (the agent's
    gather goes through the context tool instead).
    """
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute("""
            SELECT a.id, a.title, a.summary,
                   s.name AS source_name
            FROM articles a
            LEFT JOIN sources s ON a.source_id = s.id
            WHERE a.ai_tier IS NULL
            ORDER BY a.ingested_at DESC
            LIMIT ?
        """, (MAX_UNRATED_FOR_CLASSIFICATION,)).fetchall()

        return [
            {
                "id": row["id"],
                "title": row["title"],
                "source": row["source_name"] or "Unknown",
                "summary": row["summary"] or "",
            }
            for row in rows
        ]
    finally:
        conn.close()


def classify_articles(config: TiroConfig) -> list[dict]:
    """Classify unrated articles via the preference_classifier agent run.

    Raises ValueError (not enough rated / nothing unrated) and RuntimeError
    (LLMNotConfigured et al.) exactly as before — the original cause is
    re-raised out of AgentRunError.
    """
    from tiro.agents.base import AgentRunError
    from tiro.agents.runtime import run_agent

    try:
        res = run_agent(config, "preference_classifier", {})
    except AgentRunError as e:
        if e.__cause__ is not None:
            # Deliberate bare re-raise of the ORIGINAL exception: `from ...`
            # would rewrite its own cause chain; historical callers expect
            # the plain ValueError/RuntimeError surface.
            raise e.__cause__  # noqa: B904
        raise
    return res.outputs.classifications

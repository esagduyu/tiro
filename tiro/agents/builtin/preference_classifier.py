"""PreferenceClassifier — the migrated learned-preferences classification.

Behavior lock (spec §4): same gathers (via context tools whose SQL relocated
verbatim), same learned_preferences_prompt bytes, same ai_tier writeback,
same ValueError preconditions. Golden-tested in tests/test_agents_golden.py.
"""

import json
import logging

from pydantic import BaseModel

from tiro.agents.base import AgentContext, AgentResult
from tiro.intelligence.preferences import (
    MAX_UNRATED_FOR_CLASSIFICATION,
    MIN_RATED_ARTICLES,
)
from tiro.intelligence.prompts import learned_preferences_prompt
from tiro.llm import strip_json_fences

logger = logging.getLogger(__name__)

VALID_TIERS = {"must-read", "summary-enough", "discard"}


class ClassifyOutput(BaseModel):
    classifications: list[dict]


class PreferenceClassifier:
    name = "preference_classifier"
    version = "1.0"
    inputs: dict[str, type] = {}
    tier = "heavy"
    output_model = ClassifyOutput

    def run(self, ctx: AgentContext) -> AgentResult:
        loved, liked, disliked = ctx.list_rated_articles()
        if len(loved) + len(liked) + len(disliked) < MIN_RATED_ARTICLES:
            raise ValueError("Need at least 5 rated articles")
        vip = ctx.get_vip_names()
        unrated = ctx.list_unrated_articles(limit=MAX_UNRATED_FOR_CLASSIFICATION)
        if not unrated:
            raise ValueError("No unrated articles to classify")

        prompt = learned_preferences_prompt(
            loved_articles=loved, liked_articles=liked,
            disliked_articles=disliked, vip_sources=vip["sources"],
            unrated_articles=unrated,
        )
        logger.info(
            "Classifying %d unrated articles (rated: %d loved, %d liked, "
            "%d disliked, %d VIP sources)",
            len(unrated), len(loved), len(liked), len(disliked),
            len(vip["sources"]),
        )
        raw = ctx.llm("heavy", prompt, purpose="classify", max_tokens=4096)
        classifications = json.loads(strip_json_fences(raw)).get(
            "classifications", [])

        applied = 0
        for c in classifications:
            tier, article_id = c.get("tier"), c.get("article_id")
            if tier not in VALID_TIERS:
                logger.warning("Skipping invalid tier %r for article %s",
                               tier, article_id)
                continue
            ctx.set_tier(article_id, tier)
            applied += 1
        logger.info("Updated ai_tier for %d articles", applied)
        return ctx.result(ClassifyOutput(classifications=classifications))

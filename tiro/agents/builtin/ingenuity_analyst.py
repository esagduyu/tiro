"""IngenuityAnalyst — the migrated on-demand ingenuity/trust analysis.

Behavior lock (spec §4): prompt bytes, heavy tier, purpose/max_tokens, score
coercion, analyzed_at stamp, and cache-in-articles behavior identical.
"""

import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from tiro.agents.base import AgentContext, AgentResult
from tiro.intelligence.analysis import _coerce_analysis_scores
from tiro.intelligence.prompts import ingenuity_analysis_prompt
from tiro.llm import strip_json_fences

logger = logging.getLogger(__name__)


class AnalysisOutput(BaseModel):
    analysis: dict


class IngenuityAnalyst:
    name = "ingenuity_analyst"
    version = "1.0"
    inputs = {"article_id": int}
    tier = "heavy"
    output_model = AnalysisOutput

    def run(self, ctx: AgentContext, *, article_id: int) -> AgentResult:
        art = ctx.get_article(article_id)   # ValueError messages preserved
        prompt = ingenuity_analysis_prompt(art["content"],
                                           art["source"] or "Unknown")
        logger.info("Running ingenuity analysis for article %d (%s)",
                    article_id, art["source"] or "Unknown")
        raw = ctx.llm("heavy", prompt, purpose="analysis", max_tokens=2048)
        analysis = json.loads(strip_json_fences(raw))
        _coerce_analysis_scores(analysis)
        analysis["analyzed_at"] = datetime.now(UTC).isoformat()
        ctx.cache_analysis(article_id, analysis)
        return ctx.result(AnalysisOutput(analysis=analysis))

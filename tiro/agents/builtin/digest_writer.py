"""DigestWriter — the migrated three-variant daily digest generation.

Behavior lock (spec §4): one heavy call split into ranked/by_topic/by_entity,
sanitize_markdown on every section, highlight recap appended to `ranked`
only (second call only when the 7-day window has highlights), caching via
create_digest. The digest SCHEDULER (app.py _make_digest_task) and routes
keep calling generate_digest — which now dispatches here through run_agent.
"""

import logging
from datetime import date

from pydantic import BaseModel

from tiro.agents.base import AgentContext, AgentResult
from tiro.intelligence.digest import (
    DIGEST_TYPES,
    HIGHLIGHT_RECAP_WINDOW_DAYS,
    MAX_HIGHLIGHTS_FOR_RECAP,
    _split_digest,
)
from tiro.intelligence.prompts import daily_digest_prompt, highlight_recap_prompt
from tiro.sanitize import sanitize_markdown

logger = logging.getLogger(__name__)


class DigestOutput(BaseModel):
    sections: dict[str, str]
    article_ids: list[int]
    date: str


class DigestWriter:
    name = "digest_writer"
    version = "1.0"
    inputs = {"unread_only": bool}
    tier = "heavy"
    output_model = DigestOutput

    def run(self, ctx: AgentContext, *, unread_only: bool) -> AgentResult:
        articles, vip_sources, vip_authors, recent_ratings = \
            ctx.gather_digest_articles(unread_only=unread_only)
        if not articles:
            raise ValueError("No articles in library — save some articles first")

        prompt = daily_digest_prompt(vip_sources, recent_ratings, articles,
                                     vip_authors)
        article_ids = [a["id"] for a in articles]
        logger.info(
            "Generating digest with %d articles (%d VIP sources, %d rated)",
            len(articles), len(vip_sources), len(recent_ratings),
        )
        raw = ctx.llm("heavy", prompt, purpose="digest", max_tokens=4096)
        sections = _split_digest(raw)
        for dtype in DIGEST_TYPES:
            if dtype not in sections:
                sections[dtype] = ("*This section was not generated. "
                                   "Try refreshing the digest.*")
        sections = {d: sanitize_markdown(c) for d, c in sections.items()}

        highlights = ctx.get_highlights(days=HIGHLIGHT_RECAP_WINDOW_DAYS,
                                        limit=MAX_HIGHLIGHTS_FOR_RECAP)
        if highlights:
            recap = ctx.llm("heavy", highlight_recap_prompt(highlights),
                            purpose="highlight_recap", max_tokens=1024)
            sections["ranked"] = (
                f"{sections['ranked']}\n\n---\n\n"
                f"{sanitize_markdown(recap.strip())}"
            )

        today = date.today().isoformat()
        ctx.create_digest(today, sections, article_ids)
        return ctx.result(DigestOutput(
            sections=sections, article_ids=article_ids, date=today,
        ))

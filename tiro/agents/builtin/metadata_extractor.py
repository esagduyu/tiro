"""MetadataExtractor — the migrated Haiku tags/entities/summary extraction.

Behavior lock (spec §4): prompt bytes, tier, purpose, max_tokens, and output
normalization are byte-identical to the pre-runtime
tiro/ingestion/extractors.py orchestration; golden-tested in
tests/test_agents_golden.py. No tools — inputs in, one light call, outputs out.
"""

import json
import logging

from pydantic import BaseModel

from tiro.agents.base import AgentContext, AgentResult
from tiro.llm import strip_json_fences

logger = logging.getLogger(__name__)

# Haiku 4.5 handles this easily; 12k chars ~ 3k tokens ~ tenths of a cent.
# (Moved verbatim from tiro/ingestion/extractors.py, which re-imports it.)
EXTRACT_CONTENT_CHARS = 12000


class MetadataOutput(BaseModel):
    tags: list[str]
    entities: list[dict]
    summary: str


class MetadataExtractor:
    name = "metadata_extractor"
    version = "1.0"
    inputs = {"title": str, "content_md": str}
    tier = "light"
    output_model = MetadataOutput

    def run(self, ctx: AgentContext, *, title: str, content_md: str) -> AgentResult:
        # Lazy, module-attribute lookup (not a top-of-file bound name): the
        # historical monkeypatch seam is tiro.ingestion.extractors's
        # re-exported extract_metadata_prompt (test_extraction_reads_
        # beyond_2000_chars in tests/test_llm.py predates this agent and
        # patches it there) — resolving it dynamically here means a patch on
        # that name is still observed in the live call path.
        from tiro.ingestion.extractors import extract_metadata_prompt

        prompt = extract_metadata_prompt(title, content_md[:EXTRACT_CONTENT_CHARS])
        text = ctx.llm("light", prompt, purpose="extract_metadata",
                       max_tokens=1024)
        data = json.loads(strip_json_fences(text))

        tags = data.get("tags", [])
        entities = data.get("entities", [])
        summary = data.get("summary", "")
        if not isinstance(tags, list):
            tags = []
        if not isinstance(entities, list):
            entities = []
        if not isinstance(summary, str):
            summary = ""
        tags = [str(t).lower().strip() for t in tags if t][:8]
        valid_entities = []
        for e in entities:
            if isinstance(e, dict) and "name" in e and "type" in e:
                valid_entities.append({
                    "name": str(e["name"]).strip(),
                    "type": str(e["type"]).strip().lower(),
                })
        logger.info("Extracted %d tags, %d entities for '%s'",
                    len(tags), len(valid_entities), title)
        return ctx.result(MetadataOutput(
            tags=tags, entities=valid_entities, summary=summary,
        ))

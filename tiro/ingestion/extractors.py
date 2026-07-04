"""AI metadata extraction using Claude Haiku."""

import json
import logging

from tiro.config import TiroConfig
from tiro.llm import LLMNotConfigured, llm_call, strip_json_fences

logger = logging.getLogger(__name__)


def extract_metadata(title: str, content_md: str, config: TiroConfig) -> dict:
    """Extract tags, entities, and summary using Claude Haiku.

    Returns dict with keys: tags (list[str]), entities (list[dict]), summary (str).
    Returns empty defaults if extraction fails or no API key is configured.
    """
    empty = {"tags": [], "entities": [], "summary": ""}

    content_truncated = content_md[:2000]

    prompt = (
        "You are analyzing a saved article for a personal reading library. "
        "Extract structured metadata.\n\n"
        f"Article title: {title}\n"
        f"Article content: {content_truncated}\n\n"
        "Respond with JSON only, no other text:\n"
        "{\n"
        '  "tags": ["tag1", "tag2", ...],\n'
        '  "entities": [\n'
        '    {"name": "Entity Name", "type": "person|company|organization|product"}\n'
        "  ],\n"
        '  "summary": "2-3 sentence summary of the article\'s key points."\n'
        "}"
    )

    try:
        result = llm_call(
            config, "light", prompt,
            purpose="extract_metadata", max_tokens=1024,
        )
        data = json.loads(strip_json_fences(result.text))

        tags = data.get("tags", [])
        entities = data.get("entities", [])
        summary = data.get("summary", "")

        if not isinstance(tags, list):
            tags = []
        if not isinstance(entities, list):
            entities = []
        if not isinstance(summary, str):
            summary = ""

        # Normalize tags: lowercase, stripped, max 8
        tags = [str(t).lower().strip() for t in tags if t][:8]

        # Validate entity structure
        valid_entities = []
        for e in entities:
            if isinstance(e, dict) and "name" in e and "type" in e:
                valid_entities.append({
                    "name": str(e["name"]).strip(),
                    "type": str(e["type"]).strip().lower(),
                })

        logger.info(
            "Extracted %d tags, %d entities for '%s'",
            len(tags), len(valid_entities), title,
        )
        return {"tags": tags, "entities": valid_entities, "summary": summary}

    except LLMNotConfigured as e:
        logger.warning("AI extraction skipped: %s", e)
        return empty
    except Exception as e:
        logger.error("AI extraction failed for '%s': %s", title, e)
        return empty

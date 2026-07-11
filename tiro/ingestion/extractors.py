"""AI metadata extraction — compat wrapper over the agent runtime (K1).

`extract_metadata` keeps its historical signature and its never-raises,
empty-defaults-on-failure contract (processor.py and every ingestion path —
web/email/imap/rss/import — call it unchanged). The orchestration now lives
in tiro/agents/builtin/metadata_extractor.py and runs through run_agent, so
every extraction is a recorded, traced agent run.
"""

import logging

from tiro.agents.builtin.metadata_extractor import (
    EXTRACT_CONTENT_CHARS,  # noqa: F401  (re-export, historical import site)
)
from tiro.config import TiroConfig
from tiro.intelligence.prompts import (
    extract_metadata_prompt,  # noqa: F401  (re-export — legacy monkeypatch
    # seam; tests/test_llm.py::test_extraction_reads_beyond_2000_chars patches
    # this module attribute and the agent's run() reads it back dynamically)
)
from tiro.llm import LLMNotConfigured

logger = logging.getLogger(__name__)


def extract_metadata(title: str, content_md: str, config: TiroConfig) -> dict:
    """Extract tags, entities, and summary via the MetadataExtractor agent.

    Returns dict with keys: tags (list[str]), entities (list[dict]),
    summary (str). Returns empty defaults if extraction fails or no AI
    provider is configured — never raises (behavior lock).
    """
    from tiro.agents.base import AgentRunError
    from tiro.agents.runtime import run_agent  # lazy: avoids import cycle

    empty = {"tags": [], "entities": [], "summary": ""}
    try:
        res = run_agent(config, "metadata_extractor",
                        {"title": title, "content_md": content_md})
        return res.outputs.model_dump()
    except AgentRunError as e:
        if isinstance(e.__cause__, LLMNotConfigured):
            logger.warning("AI extraction skipped: %s", e.__cause__)
        else:
            logger.error("AI extraction failed for '%s': %s", title, e)
        return empty

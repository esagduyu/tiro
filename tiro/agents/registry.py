"""Agent registry — name -> agent instance. Built-ins registered explicitly
by ensure_builtins(); no import-time magic (spec §3)."""

import logging

from tiro.agents.base import TiroAgent

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, TiroAgent] = {}
_builtins_loaded = False


def register(agent: TiroAgent) -> None:
    if agent.name in _REGISTRY:
        raise ValueError(f"agent {agent.name!r} already registered")
    _REGISTRY[agent.name] = agent
    logger.debug("Registered agent %r v%s", agent.name, agent.version)


def unregister(name: str) -> None:
    """Test helper — remove a registration (no-op if absent)."""
    _REGISTRY.pop(name, None)


def get(name: str) -> TiroAgent:
    return _REGISTRY[name]  # KeyError is the contract for unknown names


def all_agents() -> dict[str, TiroAgent]:
    return dict(_REGISTRY)


def ensure_builtins() -> None:
    """Explicitly import + register the builtin agents, once.

    Idempotent; safe to call from run_agent, routes, and the CLI. Built-ins
    are appended here task-by-task (K1 Task 6, K2 Tasks 8-10).
    """
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    from tiro.agents.builtin.metadata_extractor import MetadataExtractor
    from tiro.agents.builtin.preference_classifier import PreferenceClassifier

    register(MetadataExtractor())
    register(PreferenceClassifier())

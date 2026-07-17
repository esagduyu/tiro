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


def unregister_prefix(prefix: str) -> None:
    """Remove every registration whose name starts with prefix (persona
    re-sync: files on disk are the source of truth, the registry is a
    per-process cache)."""
    for name in [n for n in _REGISTRY if n.startswith(prefix)]:
        _REGISTRY.pop(name, None)


def replace_prefix(prefix: str, agents: dict[str, TiroAgent]) -> None:
    """Swap all prefix-named registrations for the given set. Existing
    names are overwritten IN PLACE (dict item assignment -- a concurrent
    reader's get() on a still-valid name never sees it vanish, unlike an
    unregister-then-re-register window); only genuinely stale names are
    popped. Writer-writer interleaving is serialized by the caller
    (personas._SYNC_LOCK)."""
    for name in [n for n in _REGISTRY
                 if n.startswith(prefix) and n not in agents]:
        _REGISTRY.pop(name, None)
    _REGISTRY.update(agents)


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
    from tiro.agents.builtin.contradiction_detector import ContradictionDetector
    from tiro.agents.builtin.digest_writer import DigestWriter
    from tiro.agents.builtin.ingenuity_analyst import IngenuityAnalyst
    from tiro.agents.builtin.metadata_extractor import MetadataExtractor
    from tiro.agents.builtin.preference_classifier import PreferenceClassifier

    register(MetadataExtractor())
    register(PreferenceClassifier())
    register(DigestWriter())
    register(IngenuityAnalyst())
    register(ContradictionDetector())

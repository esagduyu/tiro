"""Agent contract — FROZEN from spec §2 (2026-07-06 agent-runtime spec).

Amendable through K2, frozen from K3 (personas/plugins build on it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class AgentRunError(RuntimeError):
    """The one typed error run_agent raises to callers.

    Carries the run_uid when a run row was opened before the failure (so
    callers/routes can point at the recorded error run); None when the
    failure preceded the row (unknown agent, input validation).
    The original exception is always chained as __cause__ — compat wrappers
    re-raise it to preserve the pre-runtime exception surface.
    """

    def __init__(self, message: str, *, run_uid: str | None = None):
        super().__init__(message)
        self.run_uid = run_uid


@dataclass
class AgentResult:
    outputs: BaseModel               # agent-declared pydantic model
    citations: list[str]             # article uids — auto-accumulated; prune-only
    tokens_in: int
    tokens_out: int
    cost_usd: float                  # best-effort estimate (audit pricing table)
    run_uid: str                     # trace file key


class AgentContext(Protocol):
    """Structural contract for the runtime-provided context (context.RunContext).

    Every read tool auto-appends the returned articles' uids to the run's
    citations and writes a trace event; ctx.llm is auto-audited (llm_call)
    and auto-traced, and honors the run-level (provider, model) override.
    """

    config: Any  # TiroConfig

    def llm(self, tier: str, prompt: str, *, purpose: str,
            max_tokens: int = 4096) -> str: ...

    # -- read tools (mirror MCP deliberately) --
    def search(self, q: str, *, limit: int = 10) -> list[dict]: ...
    def get_article(self, uid_or_id) -> dict: ...
    def get_highlights(self, article_uid: str | None = None, *,
                       days: int | None = None, limit: int = 50) -> list[dict]: ...
    def get_wiki_page(self, slug: str) -> dict | None: ...
    def similar_articles(self, article_uid: str, k: int = 5) -> list[dict]: ...

    # -- the ONLY persona write path (spec §5; also used by probabilistic
    #    code agents like K4's ContradictionDetector) --
    def suggest(self, kind: str, payload: dict,
                citations: list[str]) -> str: ...

    # -- result assembly (OPEN decision 3) --
    def result(self, outputs: BaseModel,
               citations: list[str] | None = None) -> AgentResult: ...


@runtime_checkable
class TiroAgent(Protocol):
    name: str
    version: str
    inputs: dict[str, type]          # validated before run
    tier: str                        # default capability tier ("heavy"|"light")
    output_model: type[BaseModel]

    def run(self, ctx: AgentContext, **inputs) -> AgentResult: ...

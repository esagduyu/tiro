"""The single chokepoint for every LLM call Tiro makes (Decision #7).

Call sites request a capability tier ("heavy" for cross-document reasoning,
"light" for per-article extraction) — NEVER a model name. Config maps tiers
to (provider, model). Audit logging (success and failure) lives here; no
call site should touch the anthropic SDK or log_api_call for LLM work again.
"""

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from tiro.audit import log_api_call
from tiro.config import TiroConfig

logger = logging.getLogger(__name__)

Tier = Literal["heavy", "light"]


class LLMNotConfigured(RuntimeError):
    """The resolved provider has no credentials/binary available."""


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


def strip_json_fences(text: str) -> str:
    """Remove a wrapping ```json ...``` / ``` ...``` fence if present.
    (Models wrap JSON in fences despite instructions — historical pattern
    from analysis.py, now centralized.)"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return cleaned


def resolve_tier(config: TiroConfig, tier: Tier) -> tuple[str, str]:
    if tier == "heavy":
        return (config.ai_heavy_provider, config.ai_heavy_model or config.opus_model)
    return (config.ai_light_provider, config.ai_light_model or config.haiku_model)


def _call_anthropic(config: TiroConfig, model: str, prompt: str, *,
                    system: str | None, max_tokens: int) -> LLMResult:
    if not os.environ.get("ANTHROPIC_API_KEY") and not config.anthropic_api_key:
        raise LLMNotConfigured("ANTHROPIC_API_KEY not set and no anthropic_api_key in config")
    import anthropic

    # anthropic.Anthropic() only reads the env var, not config — load_config
    # normally syncs config.anthropic_api_key into the env, but this is
    # defense-in-depth for callers that only set config.
    if config.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", config.anthropic_api_key)
    client = anthropic.Anthropic()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    usage = getattr(response, "usage", None)
    return LLMResult(
        text=response.content[0].text,
        provider="anthropic",
        model=model,
        tokens_in=getattr(usage, "input_tokens", None),
        tokens_out=getattr(usage, "output_tokens", None),
    )


_BACKENDS: dict[str, Callable[..., LLMResult]] = {
    "anthropic": _call_anthropic,
}


def llm_call(config: TiroConfig, tier: Tier, prompt: str, *, purpose: str,
             max_tokens: int = 1024, system: str | None = None) -> LLMResult:
    provider, model = resolve_tier(config, tier)
    backend = _BACKENDS.get(provider)
    if backend is None:
        raise LLMNotConfigured(f"Unknown AI provider '{provider}'")
    start = time.monotonic()
    try:
        result = backend(config, model, prompt, system=system, max_tokens=max_tokens)
    except Exception as e:
        log_api_call(
            config, provider, endpoint=purpose, model=model,
            duration_ms=int((time.monotonic() - start) * 1000),
            success=False, error=str(e),
        )
        raise
    log_api_call(
        config, provider, endpoint=purpose, model=model,
        tokens_in=result.tokens_in, tokens_out=result.tokens_out,
        duration_ms=int((time.monotonic() - start) * 1000),
        cost_usd=result.cost_usd,
    )
    return result

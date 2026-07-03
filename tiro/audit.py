"""External-API audit log: one JSONL line per non-local call.

Files live at {library}/audit/{YYYY-MM-DD}.jsonl (local date, matching the
digest cache's date convention). Logging is strictly best-effort — an
unwritable audit dir must never break the API call being observed.
"""

import json
import logging
import time
from datetime import date, datetime, timezone

from tiro.config import TiroConfig

logger = logging.getLogger(__name__)

# Per 1M tokens (input, output). Prefix-matched against the model string so
# dated ids like claude-haiku-4-5-20251001 resolve. Update when models change.
ANTHROPIC_PRICING = {
    "claude-opus-4-6": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Per 1M characters.
OPENAI_TTS_PRICING = {
    "tts-1-hd": 30.00,  # longest prefix first — tts-1 is a prefix of tts-1-hd
    "tts-1": 15.00,
}


def estimate_cost(service, model, tokens_in, tokens_out, chars):
    """Best-effort cost estimate in USD; None when unknown (never guess)."""
    if service == "anthropic" and model:
        for prefix, (in_rate, out_rate) in ANTHROPIC_PRICING.items():
            if model.startswith(prefix):
                return ((tokens_in or 0) / 1_000_000) * in_rate + \
                       ((tokens_out or 0) / 1_000_000) * out_rate
    if service == "openai_tts" and model:
        for prefix, rate in OPENAI_TTS_PRICING.items():
            if model.startswith(prefix):
                return ((chars or 0) / 1_000_000) * rate
    return None


def log_api_call(
    config: TiroConfig,
    service: str,
    *,
    endpoint: str = "",
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    chars: int | None = None,
    bytes_out: int | None = None,
    count: int | None = None,
    duration_ms: int | None = None,
    success: bool = True,
    error: str | None = None,
) -> None:
    """Append one audit entry. Swallows its own failures by design."""
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "service": service,
            "endpoint": endpoint,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "chars": chars,
            "bytes_out": bytes_out,
            "count": count,
            "duration_ms": duration_ms,
            "cost_estimate": estimate_cost(service, model, tokens_in, tokens_out, chars),
            "success": success,
            "error": error,
        }
        audit_dir = config.library / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = audit_dir / f"{date.today().isoformat()}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error("Audit log write failed (%s/%s): %s", service, endpoint, e)


def read_audit_entries(
    config: TiroConfig,
    *,
    date: str | None = None,
    month: str | None = None,
    service: str | None = None,
) -> list[dict]:
    """Read audit entries. date='YYYY-MM-DD' reads one file; month='YYYY-MM'
    reads every file in that month; neither reads everything."""
    audit_dir = config.library / "audit"
    if not audit_dir.exists():
        return []
    entries: list[dict] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        stem = path.stem
        if date and stem != date:
            continue
        if month and not stem.startswith(month):
            continue
        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt audit line in %s", path.name)
                continue
            if service and entry.get("service") != service:
                continue
            entries.append(entry)
    return entries


def summarize(entries: list[dict]) -> dict:
    """Per-service rollup: calls, failures, tokens, chars, est. cost."""
    rollup: dict[str, dict] = {}
    for e in entries:
        s = rollup.setdefault(e.get("service", "?"), {
            "calls": 0, "failures": 0, "tokens_in": 0, "tokens_out": 0,
            "chars": 0, "cost_estimate": 0.0,
        })
        s["calls"] += 1
        if not e.get("success", True):
            s["failures"] += 1
        s["tokens_in"] += e.get("tokens_in") or 0
        s["tokens_out"] += e.get("tokens_out") or 0
        s["chars"] += e.get("chars") or 0
        s["cost_estimate"] += e.get("cost_estimate") or 0.0
    return rollup


def audited_anthropic_call(config: TiroConfig, client, *, endpoint: str, **kwargs):
    """client.messages.create with timing + usage + cost audit logging.
    Re-raises API errors after logging the failure."""
    start = time.monotonic()
    model = kwargs.get("model")
    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        log_api_call(
            config, "anthropic", endpoint=endpoint, model=model,
            duration_ms=int((time.monotonic() - start) * 1000),
            success=False, error=str(e),
        )
        raise
    usage = getattr(response, "usage", None)
    log_api_call(
        config, "anthropic", endpoint=endpoint, model=model,
        tokens_in=getattr(usage, "input_tokens", None),
        tokens_out=getattr(usage, "output_tokens", None),
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return response

"""On-ingest agent hooks (Phase 6 K4, spec §3/§6).

process_article() calls dispatch_on_ingest() at its TAIL — after the
rollback window has closed and the result dict is built. Contract:

* dispatch_on_ingest NEVER raises and never blocks the save: real work
  happens on a daemon thread (run_agent's own lock serializes runs, so a
  synchronous hook would block every save behind any in-flight agent).
* Failures are logged and — once a run row exists — recorded in agent_runs
  by the runtime. They can never fail ingestion (processor double-guards).
* ingestion_method == "import" is deliberately SKIPPED: bulk importers
  would fan out hundreds of LLM runs; `tiro agent run
  contradiction-detector --backfill` is the explicit path for imports.
  Files reconciled from disk by the sync engine (S1's
  ingest_external_file, ingestion_method == "external") bypass
  process_article entirely and so never reach this dispatch either —
  the backfill CLI covers them the same way.
* Kill-switch: contradiction_detector_enabled=False removes the detector
  from dispatch entirely ("off = no hook registered", spec §6). Manual
  runs and backfill are unaffected — the flag gates the hook, not the agent.
* Persona schedules: enabled `schedule: on-ingest` personas with
  scope == "article" dispatch here too (closing K3's documented gap).
  Non-article scopes have no ingest-derivable inputs and are skipped.
  `cron` persona dispatch is NOT built yet (deferred to R5).
"""

import logging
import threading

from tiro.config import TiroConfig

logger = logging.getLogger(__name__)

SKIPPED_METHODS = {"import"}


def _spawn(fn) -> None:
    """Test seam — monkeypatch to `lambda fn: fn()` for inline execution."""
    threading.Thread(target=fn, daemon=True,
                     name="tiro-on-ingest-hooks").start()


def _plan(config: TiroConfig) -> list[str]:
    """Agent names to run for one ingest event. Never raises."""
    names: list[str] = []
    if config.contradiction_detector_enabled:
        names.append("contradiction-detector")
    try:
        from tiro.agents.personas import load_personas

        personas, _errors = load_personas(config)
        disabled = set(config.personas_disabled or [])
        for p in personas:
            if p.schedule != "on-ingest" or p.slug in disabled:
                continue
            if p.scope != "article":
                logger.debug(
                    "Persona %s is schedule=on-ingest but scope=%s — no "
                    "ingest-derivable inputs, skipped", p.slug, p.scope)
                continue
            names.append(f"persona:{p.slug}")
    except Exception as e:
        logger.error("on-ingest persona planning failed (non-fatal): %s", e)
    return names


def _run_named_hooks(config: TiroConfig, article_id: int,
                     names: list[str]) -> None:
    from tiro.agents.base import AgentRunError
    from tiro.agents.runtime import run_agent

    for name in names:
        try:
            run_agent(config, name, {"article_id": article_id})
        except AgentRunError as e:
            recorded = f" (recorded as run {e.run_uid})" if e.run_uid else ""
            logger.warning("on-ingest %s failed for article %d%s: %s",
                           name, article_id, recorded, e)
        except Exception as e:                       # pragma: no cover
            logger.error("on-ingest %s crashed for article %d: %s",
                         name, article_id, e)


def run_on_ingest_hooks(config: TiroConfig, article_id: int) -> None:
    """Synchronous hook body (tests call this directly). Never raises."""
    try:
        _run_named_hooks(config, article_id, _plan(config))
    except Exception as e:                           # pragma: no cover
        logger.error("on-ingest hooks failed for article %d: %s",
                     article_id, e)


def dispatch_on_ingest(config: TiroConfig, article_id: int,
                       ingestion_method: str) -> None:
    """Fire-and-forget dispatch. NEVER raises (test-asserted)."""
    try:
        if ingestion_method in SKIPPED_METHODS:
            return
        names = _plan(config)
        if not names:
            return          # kill-switch off + no on-ingest personas
        _spawn(lambda: _run_named_hooks(config, article_id, names))
    except Exception as e:
        logger.error("on-ingest dispatch failed (non-fatal): %s", e)

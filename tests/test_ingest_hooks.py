"""On-ingest agent hooks (Phase 6 K4): processor dispatch, failure
isolation, kill-switch, persona on-ingest wiring.

IMPORTANT: conftest's autouse _no_ingest_hooks fixture replaces the
tiro.agents.hooks.dispatch_on_ingest MODULE ATTRIBUTE with a no-op for the
whole suite. This module binds the REAL function at import time (below),
so calling `real_dispatch` here exercises the genuine code path while the
attribute stays patched for everyone else.
"""

import json

import pytest

from tests.test_contradiction import VERDICT_HIGH, _fake_similars, _seed_article
from tiro.agents import hooks
from tiro.agents.hooks import dispatch_on_ingest as real_dispatch
from tiro.agents.hooks import run_on_ingest_hooks

EXTRACTION_JSON = json.dumps(
    {"tags": ["k4"], "entities": [], "summary": "hook test summary"})


def _ingest(config, *, title="Hook Piece", method="manual"):
    from tiro.ingestion.processor import process_article

    return process_article(
        title=title, author=None,
        content_md="Rates rose in 2025 according to the Fed.",
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        config=config, ingestion_method=method)


# --- processor integration ---------------------------------------------------


def test_process_article_dispatches_hook_after_success(
        initialized_library, fake_llm, monkeypatch):
    calls = []
    monkeypatch.setattr(
        hooks, "dispatch_on_ingest",
        lambda config, article_id, ingestion_method:
            calls.append((article_id, ingestion_method)))
    fake_llm(EXTRACTION_JSON)

    result = _ingest(initialized_library, method="rss")
    assert calls == [(result["id"], "rss")]


def test_hook_error_never_fails_the_save(initialized_library, fake_llm,
                                         monkeypatch):
    """Parent-mandated scope guard: even a raising dispatch (which violates
    its own contract) must not fail ingestion — processor's try/except is
    the second belt."""
    def boom(config, article_id, ingestion_method):
        raise RuntimeError("hook exploded")

    monkeypatch.setattr(hooks, "dispatch_on_ingest", boom)
    fake_llm(EXTRACTION_JSON)

    result = _ingest(initialized_library, title="Survives Hook Crash")
    assert result["id"]                      # the save succeeded
    assert result["title"] == "Survives Hook Crash"


def test_failed_ingest_never_dispatches(initialized_library, fake_llm,
                                        monkeypatch):
    """The hook must sit OUTSIDE the rollback window: a rolled-back ingest
    dispatches nothing."""
    calls = []
    monkeypatch.setattr(
        hooks, "dispatch_on_ingest",
        lambda *a, **k: calls.append(a))
    # Force the enrichment stage to blow up -> rollback via delete_article.
    monkeypatch.setattr(
        "tiro.ingestion.processor.extract_metadata",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stage failed")))

    with pytest.raises(RuntimeError, match="stage failed"):
        _ingest(initialized_library, title="Rolled Back")
    assert calls == []


# --- dispatch gating ----------------------------------------------------------


def test_dispatch_skips_import_method(initialized_library, monkeypatch):
    spawned = []
    monkeypatch.setattr(hooks, "_spawn", lambda fn: spawned.append(fn))

    real_dispatch(initialized_library, 1, "import")
    assert spawned == []


def test_dispatch_no_thread_when_disabled_and_no_personas(
        initialized_library, monkeypatch):
    spawned = []
    monkeypatch.setattr(hooks, "_spawn", lambda fn: spawned.append(fn))
    initialized_library.contradiction_detector_enabled = False

    real_dispatch(initialized_library, 1, "manual")
    assert spawned == []                     # kill-switch: no hook registered


def test_dispatch_spawns_when_enabled(initialized_library, monkeypatch):
    spawned = []
    monkeypatch.setattr(hooks, "_spawn", lambda fn: spawned.append(fn))

    real_dispatch(initialized_library, 1, "manual")
    assert len(spawned) == 1


def test_dispatch_never_raises(initialized_library, monkeypatch):
    def boom(fn):
        raise RuntimeError("thread pool on fire")

    monkeypatch.setattr(hooks, "_spawn", boom)
    real_dispatch(initialized_library, 1, "manual")   # must not raise


# --- synchronous hook body ----------------------------------------------------


def test_run_hooks_executes_detector(initialized_library, fake_llm,
                                     monkeypatch):
    from tiro.suggestions import list_suggestions

    aid, _ = _seed_article(initialized_library, title="Hook New")
    cand, _ = _seed_article(initialized_library, title="Hook Trusted",
                            rating=2)
    _fake_similars(monkeypatch, [(cand, 0.9)])
    fake_llm(VERDICT_HIGH)

    run_on_ingest_hooks(initialized_library, aid)
    rows = list_suggestions(initialized_library, status="pending")
    assert [r["kind"] for r in rows] == ["contradiction"]


def test_run_hooks_swallows_agent_errors(initialized_library, fake_llm,
                                         monkeypatch):
    """Empty fake queue on a run that WOULD call the LLM -> AgentRunError
    inside run_agent -> swallowed here; the error run is still recorded."""
    from tiro.database import get_connection

    aid, _ = _seed_article(initialized_library, title="Hook Err New")
    cand, _ = _seed_article(initialized_library, title="Hook Err Trusted",
                            rating=2)
    _fake_similars(monkeypatch, [(cand, 0.9)])
    # queue deliberately left empty -> fake backend raises

    run_on_ingest_hooks(initialized_library, aid)    # must not raise
    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM agent_runs WHERE agent_name = "
            "'contradiction-detector' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row["status"] == "error"          # provenance survived the swallow


def test_run_hooks_respects_kill_switch(initialized_library, fake_llm,
                                        monkeypatch):
    from tiro.database import get_connection

    initialized_library.contradiction_detector_enabled = False
    aid, _ = _seed_article(initialized_library, title="Hook Disabled")

    run_on_ingest_hooks(initialized_library, aid)
    conn = get_connection(initialized_library.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM agent_runs").fetchone()["n"]
    finally:
        conn.close()
    assert n == 0


# --- persona on-ingest wiring (K3's documented gap, closed here) -------------


def _write_persona(config, slug, *, scope="article", schedule="on-ingest",
                   output="note", body="Note this: {{article}}"):
    pdir = config.library / "personas"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{slug}.md").write_text(
        "---\n"
        f"name: {slug}\nscope: {scope}\noutput: {output}\n"
        f"schedule: {schedule}\n"
        "---\n\n"
        f"{body}\n")


def test_on_ingest_persona_dispatched(initialized_library, fake_llm,
                                      monkeypatch):
    from tiro.suggestions import list_suggestions

    initialized_library.contradiction_detector_enabled = False   # isolate
    _write_persona(initialized_library, "on-save-note")
    aid, _ = _seed_article(initialized_library, title="Persona Hook")
    fake_llm("A persona note.")

    run_on_ingest_hooks(initialized_library, aid)
    rows = list_suggestions(initialized_library, status="pending")
    assert [r["kind"] for r in rows] == ["note"]
    assert rows[0]["persona"] == "persona:on-save-note"
    assert rows[0]["payload"]["markdown"] == "A persona note."


def test_on_ingest_persona_skips_wrong_scope_disabled_and_manual(
        initialized_library, fake_llm, monkeypatch):
    from tiro.suggestions import list_suggestions

    initialized_library.contradiction_detector_enabled = False
    _write_persona(initialized_library, "day-hook", scope="day",
                   output="digest_section", body="x {{day_articles}}")
    _write_persona(initialized_library, "manual-only", schedule="manual")
    _write_persona(initialized_library, "muted")
    initialized_library.personas_disabled = ["muted"]
    aid, _ = _seed_article(initialized_library, title="Persona Skips")
    # queue left empty: if ANY persona ran, the fake backend would raise
    # inside run_agent and land an error row — assert nothing ran at all.

    run_on_ingest_hooks(initialized_library, aid)
    assert list_suggestions(initialized_library) == []
    from tiro.database import get_connection
    conn = get_connection(initialized_library.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM agent_runs").fetchone()["n"]
    finally:
        conn.close()
    assert n == 0

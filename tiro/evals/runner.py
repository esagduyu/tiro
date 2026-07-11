"""Evals harness (spec §7). Structural mode: temp library per fixture,
fake provider, zero cost — NEVER the user's library or config. --real mode
swaps in the configured providers after an interactive cost confirm."""

import json
import logging
import tempfile
from dataclasses import fields
from pathlib import Path

from tiro import llm as llm_module
from tiro.config import TiroConfig
from tiro.database import get_connection, init_db, migrate_db
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

EVALS_DIR = Path(__file__).resolve().parent
AGENT_NAMES = ("metadata_extractor", "preference_classifier",
               "digest_writer", "ingenuity_analyst")


def load_fixtures(agent_name: str) -> list[dict]:
    path = EVALS_DIR / agent_name / "fixtures.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _build_eval_library(
    seed: dict | None, tmp: Path, *, real: bool, providers: dict | None = None
) -> TiroConfig:
    """Throwaway library. Articles are inserted with sequential ids starting
    at 1 (fixtures rely on it); a 'body' key writes the markdown file.

    Structural mode (real=False) is unchanged: the provider is always forced
    to "fake", `providers` is ignored. In --real mode, the caller's
    configured provider/model/key fields (`providers`) are overlaid onto
    this otherwise-throwaway config AFTER construction — library isolation
    (temp dir, fresh SQLite) is unaffected; only AI-routing fields are
    borrowed from the user's real config, and only fields that actually
    exist on TiroConfig are applied.
    """
    config = TiroConfig(library_path=str(tmp / "eval-library"))
    if not real:
        config.ai_heavy_provider = "fake"
        config.ai_light_provider = "fake"
    elif providers:
        valid_fields = {f.name for f in fields(TiroConfig)}
        for key, val in providers.items():
            if key in valid_fields:
                setattr(config, key, val)
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    if seed:
        conn = get_connection(config.db_path)
        try:
            sid = conn.execute(
                "INSERT INTO sources (name, domain, source_type) "
                "VALUES ('Eval Source', 'eval.example.com', 'web')").lastrowid
            for i, art in enumerate(seed.get("articles", []), start=1):
                slug = f"eval-{i}"
                fname = f"{slug}.md"
                (config.articles_dir / fname).write_text(
                    f"---\ntitle: {art['title']}\n---\n\n"
                    f"{art.get('body', art.get('summary', ''))}"
                )
                conn.execute(
                    """INSERT INTO articles (uid, source_id, title, url, slug,
                       markdown_path, word_count, reading_time_min,
                       ingested_at, rating, summary)
                       VALUES (?, ?, ?, ?, ?, ?, 5, 1, ?, ?, ?)""",
                    (new_ulid(), sid, art["title"],
                     f"https://eval.example.com/{slug}", slug, fname,
                     f"2026-07-01T00:00:{99 - i:02d}",
                     art.get("rating"), art.get("summary", "")),
                )
            conn.commit()
        finally:
            conn.close()
    return config


class _PromptRecorder:
    """Wraps llm_module.llm_call to capture prompts (structural mode)."""

    def __init__(self):
        self.prompts: list[str] = []
        self._orig = llm_module.llm_call
        self._installed = False

    def __enter__(self):
        orig = self._orig

        def wrapper(config, tier, prompt, **kw):
            self.prompts.append(prompt)
            return orig(config, tier, prompt, **kw)

        llm_module.llm_call = wrapper
        self._installed = True
        return self

    def __exit__(self, *exc):
        if self._installed:
            llm_module.llm_call = self._orig
            self._installed = False


def _check_fixture(
    agent_name: str, fixture: dict, *, real: bool, providers: dict | None = None
) -> list[str]:
    """Run one fixture; return a list of failure strings (empty = pass)."""
    from tiro.agents.runtime import run_agent

    with tempfile.TemporaryDirectory(prefix="tiro-eval-") as tmp:
        config = _build_eval_library(
            fixture.get("seed"), Path(tmp), real=real, providers=providers
        )
        if not real:
            llm_module._fake_responses.clear()
            llm_module.queue_fake_responses(*fixture.get("fake_responses", []))
        rec = _PromptRecorder()
        with rec:
            try:
                result = run_agent(config, agent_name, fixture.get("inputs", {}))
            except Exception as e:
                return [f"run failed: {e}"]
            finally:
                llm_module._fake_responses.clear()

        expect = fixture.get("expect", {})
        failures: list[str] = []
        out = result.outputs.model_dump()
        for needle in expect.get("prompt_contains", []):
            if not any(needle in p for p in rec.prompts):
                failures.append(f"prompt missing {needle!r}")
        for key, val in expect.get("output", {}).items():
            if out.get(key) != val:
                failures.append(f"output[{key!r}] = {out.get(key)!r}, expected {val!r}")
        for key in expect.get("output_keys", []):
            if key not in out:
                failures.append(f"output missing key {key!r}")
        if expect.get("citations_resolve"):
            conn = get_connection(config.db_path)
            try:
                uids = {r["uid"] for r in conn.execute(
                    "SELECT uid FROM articles").fetchall()}
            finally:
                conn.close()
            bogus = [c for c in result.citations if c not in uids]
            if bogus:
                failures.append(f"citations do not resolve: {bogus}")
    return failures


def run_structural(
    agent_name: str | None = None, *, real: bool = False, providers: dict | None = None
) -> dict:
    """Run fixtures for one agent (or all). Returns
    {agent: {"passed": n, "failed": n, "failures": ["name: msg", ...]}}.

    `providers` is only consulted when `real=True` (see
    `_build_eval_library`'s docstring). This function never loads the
    user's real config.yaml itself — that is the CALLER's job (cmd_evals in
    tiro/cli.py); the isolation test relies on that split staying true, so
    keep this module free of any `load_config` call.
    """
    from tiro.agents import registry

    registry.ensure_builtins()
    names = [agent_name] if agent_name else list(AGENT_NAMES)
    results: dict[str, dict] = {}
    for name in names:
        passed, failed, messages = 0, 0, []
        for fixture in load_fixtures(name):
            errs = _check_fixture(name, fixture, real=real, providers=providers)
            if errs:
                failed += 1
                messages.extend(f"{fixture['name']}: {e}" for e in errs)
            else:
                passed += 1
        results[name] = {"passed": passed, "failed": failed,
                         "failures": messages}
    return results

"""Structural evals as a pytest gate (the CI hook for spec §7): every
registered builtin agent's fixtures must pass in fake mode on every PR."""


def test_all_builtin_evals_pass_structural():
    from tiro.evals.runner import run_structural

    results = run_structural()          # all agents, temp libraries, fake LLM
    assert set(results) == {
        "metadata_extractor", "preference_classifier",
        "digest_writer", "ingenuity_analyst", "contradiction-detector",
    }
    failures = {
        agent: r["failures"] for agent, r in results.items() if r["failed"]
    }
    assert failures == {}
    assert all(r["passed"] >= 1 for r in results.values())


def test_eval_runner_never_touches_cwd_or_real_config(tmp_path, monkeypatch):
    """Runner must build its own temp library (OPEN decision 11) — a config
    file in CWD must remain unread/unwritten."""
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / "config.yaml"
    sentinel.write_text("library_path: ./should-never-be-used\n")
    from tiro.evals.runner import run_structural

    run_structural(agent_name="metadata_extractor")
    assert sentinel.read_text() == "library_path: ./should-never-be-used\n"
    assert not (tmp_path / "should-never-be-used").exists()


def test_real_mode_threads_providers_onto_isolated_eval_config(tmp_path, monkeypatch):
    """--real must overlay the caller's configured provider/model fields
    onto the eval config, while keeping the eval library isolated (temp
    dir, never CWD). The fixtures' own `fake_responses` are only consulted
    in structural (non-real) mode, so this test drives the fake backend by
    monkeypatching llm.llm_call directly and returning the two
    metadata_extractor fixtures' expected responses in file order."""
    import tiro.llm as llm_module
    from tiro.evals.runner import run_structural
    from tiro.llm import LLMResult

    monkeypatch.chdir(tmp_path)  # a real config.yaml here must never be read
    responses = [
        '{"tags": ["AI", "Transformers"], "entities": '
        '[{"name": "Google", "type": "company"}], '
        '"summary": "Introduces the transformer."}',
        '{"tags": [1, " X "], "entities": ["nope"], "summary": 42}',
    ]
    seen_configs = []

    def fake_llm_call(config, tier, prompt, *, purpose, max_tokens=1024, system=None):
        seen_configs.append(config)
        return LLMResult(text=responses.pop(0), provider="fake", model="fake-x")

    monkeypatch.setattr(llm_module, "llm_call", fake_llm_call)

    results = run_structural(
        "metadata_extractor",
        real=True,
        providers={
            "ai_light_provider": "fake",
            "ai_light_model": "fake-x",
            "ai_heavy_provider": "fake",
            "ai_heavy_model": "fake-x",
        },
    )

    assert results["metadata_extractor"]["failures"] == []
    assert results["metadata_extractor"]["passed"] == 2
    assert results["metadata_extractor"]["failed"] == 0

    assert seen_configs, "llm_call was never invoked"
    for config in seen_configs:
        assert config.ai_light_provider == "fake"
        assert config.ai_light_model == "fake-x"
        assert config.ai_heavy_provider == "fake"
        assert config.ai_heavy_model == "fake-x"
        # Isolation preserved: eval library is a temp dir, never CWD.
        assert "tiro-eval-" in str(config.library)
        assert not str(config.library).startswith(str(tmp_path))

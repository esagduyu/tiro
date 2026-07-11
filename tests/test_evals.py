"""Structural evals as a pytest gate (the CI hook for spec §7): every
registered builtin agent's fixtures must pass in fake mode on every PR."""


def test_all_builtin_evals_pass_structural():
    from tiro.evals.runner import run_structural

    results = run_structural()          # all agents, temp libraries, fake LLM
    assert set(results) == {
        "metadata_extractor", "preference_classifier",
        "digest_writer", "ingenuity_analyst",
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

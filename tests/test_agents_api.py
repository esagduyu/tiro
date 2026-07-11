"""/api/agents routes + /agents page (Phase 6 K2)."""

import json

import pytest


@pytest.fixture
def agents_client(authenticated_client):
    """Authed client whose app config also routes LLM tiers to fake."""
    from tiro import llm

    config = authenticated_client.app.state.config
    config.ai_heavy_provider = "fake"
    config.ai_light_provider = "fake"
    llm._fake_responses.clear()
    yield authenticated_client
    llm._fake_responses.clear()


def _run_metadata(client, text="hello"):
    from tiro import llm

    llm.queue_fake_responses(
        '{"tags": ["t"], "entities": [], "summary": "s"}')
    r = client.post("/api/agents/metadata_extractor/run", json={
        "inputs": {"title": "T", "content_md": text}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    return body["data"]


def test_list_agents_registry_and_last_run(agents_client):
    r = agents_client.get("/api/agents")
    assert r.status_code == 200
    names = {a["name"] for a in r.json()["data"]}
    assert {"metadata_extractor", "preference_classifier",
            "digest_writer", "ingenuity_analyst"} <= names
    by_name = {a["name"]: a for a in r.json()["data"]}
    assert by_name["metadata_extractor"]["last_run"] is None

    _run_metadata(agents_client)
    r2 = agents_client.get("/api/agents")
    last = {a["name"]: a for a in r2.json()["data"]}["metadata_extractor"]["last_run"]
    assert last["status"] == "ok"


def test_manual_run_and_run_listing(agents_client):
    data = _run_metadata(agents_client)
    assert data["status"] == "ok"
    assert data["output"]["summary"] == "s"

    r = agents_client.get("/api/agents/runs")
    body = r.json()["data"]
    assert body["total"] == 1
    run = body["runs"][0]
    assert run["agent_name"] == "metadata_extractor"
    assert run["trace_available"] is True

    # filters
    assert agents_client.get(
        "/api/agents/runs?agent=digest_writer").json()["data"]["total"] == 0
    assert agents_client.get(
        "/api/agents/runs?status=error").json()["data"]["total"] == 0
    assert agents_client.get(
        "/api/agents/runs?status=bogus").status_code == 400


def test_manual_run_error_shapes(agents_client):
    assert agents_client.post(
        "/api/agents/nope/run", json={"inputs": {}}).status_code == 404
    r = agents_client.post(
        "/api/agents/metadata_extractor/run", json={"inputs": {"title": "x"}})
    assert r.status_code == 400          # missing content_md (validation)
    # run-body failure -> success false + recorded run_uid (fake queue empty)
    r2 = agents_client.post(
        "/api/agents/metadata_extractor/run",
        json={"inputs": {"title": "T", "content_md": "b"}})
    assert r2.status_code == 200
    assert r2.json()["success"] is False
    assert r2.json()["data"]["run_uid"]


def test_run_detail_and_trace(agents_client):
    data = _run_metadata(agents_client)
    uid = data["run_uid"]

    r = agents_client.get(f"/api/agents/runs/{uid}")
    detail = r.json()["data"]
    assert detail["input"]["title"] == "T"
    assert detail["output"]["tags"] == ["t"]
    assert detail["citations"] == []

    t = agents_client.get(f"/api/agents/runs/{uid}?trace=1")
    assert t.status_code == 200
    lines = [json.loads(ln) for ln in t.text.splitlines()]
    assert lines[0]["kind"] == "run"
    assert lines[1]["kind"] == "llm"

    assert agents_client.get("/api/agents/runs/01NOPE").status_code == 404

    # expired trace: file pruned, row remains
    from tiro.agents.runtime import traces_dir

    (traces_dir(agents_client.app.state.config) / f"{uid}.jsonl").unlink()
    t2 = agents_client.get(f"/api/agents/runs/{uid}?trace=1")
    assert t2.status_code == 404
    assert "expired" in t2.json()["detail"]
    d2 = agents_client.get(f"/api/agents/runs/{uid}").json()["data"]
    assert d2["trace_available"] is False


def test_replay_sets_replay_of_and_leaves_original(agents_client):
    from tiro import llm

    first = _run_metadata(agents_client)
    llm.queue_fake_responses('{"tags": [], "entities": [], "summary": "s2"}')
    r = agents_client.post(
        f"/api/agents/runs/{first['run_uid']}/replay",
        json={"model_override": {"provider": "fake", "model": "fake-9"}})
    assert r.status_code == 200
    replay = r.json()["data"]
    assert replay["run_uid"] != first["run_uid"]

    detail = agents_client.get(
        f"/api/agents/runs/{replay['run_uid']}").json()["data"]
    assert detail["replay_of"] == first["run_uid"]
    assert detail["model"] == "fake-9"
    original = agents_client.get(
        f"/api/agents/runs/{first['run_uid']}").json()["data"]
    assert original["status"] == "ok" and original["replay_of"] is None

    # garbage override provider -> 400 before any run
    bad = agents_client.post(
        f"/api/agents/runs/{first['run_uid']}/replay",
        json={"model_override": {"provider": "skynet", "model": "m"}})
    assert bad.status_code == 400
    assert agents_client.post(
        "/api/agents/runs/01NOPE/replay", json={}).status_code == 404


def test_list_agents_last_run_ties_break_on_id(agents_client):
    """Two runs of the same agent sharing the same (second-granularity)
    started_at must resolve to the HIGHER-id run as last_run, not an
    arbitrary one (regression for the started_at-only tie)."""
    from tiro import llm
    from tiro.database import get_connection

    first = _run_metadata(agents_client, text="first")
    llm.queue_fake_responses(
        '{"tags": ["t"], "entities": [], "summary": "second"}')
    second = _run_metadata(agents_client, text="second")
    assert first["run_uid"] != second["run_uid"]

    config = agents_client.app.state.config
    conn = get_connection(config.db_path)
    try:
        # Force both rows onto the identical started_at value, simulating
        # a same-second tie; ids remain distinct and monotonic.
        conn.execute(
            "UPDATE agent_runs SET started_at = '2026-01-01 00:00:00' "
            "WHERE run_uid IN (?, ?)",
            (first["run_uid"], second["run_uid"]),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT id, run_uid FROM agent_runs WHERE run_uid IN (?, ?)",
            (first["run_uid"], second["run_uid"]),
        ).fetchall()
    finally:
        conn.close()
    by_uid = {r["run_uid"]: r["id"] for r in rows}
    assert by_uid[second["run_uid"]] > by_uid[first["run_uid"]]

    r = agents_client.get("/api/agents")
    last = {a["name"]: a for a in r.json()["data"]}["metadata_extractor"]["last_run"]
    assert last["run_uid"] == second["run_uid"]
    assert last["output"]["summary"] == "second"


def test_agents_api_requires_auth(auth_client):
    """API half of the route-walk intent, runs now (Task 11)."""
    assert auth_client.get("/api/agents").status_code == 401


def test_agents_page_requires_auth(auth_client):
    """Page half — deferred until the /agents page route exists (Task 12)."""
    assert auth_client.get("/agents").status_code == 302


def test_agents_page_renders(agents_client):
    r = agents_client.get("/agents")
    assert r.status_code == 200
    assert 'id="agents-root"' in r.text
    assert "agents.js" in r.text


def test_base_nav_has_agents_entry(agents_client):
    r = agents_client.get("/inbox")
    assert 'href="/agents"' in r.text

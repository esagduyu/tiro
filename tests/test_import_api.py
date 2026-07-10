"""Background import API (Phase 4 M4.2, spec D6): single-slot job, progress
polling, auth-gated (the route-walk enforces auth as an invariant; these tests
assert the happy-path fixture usage + the 409/400 branches).
"""

import json
import time


def _poll_until_done(client, timeout=10.0):
    """Poll GET /api/import/status until the job reports finished, pumping the
    event loop through each request so the background task advances."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get("/api/import/status").json()["data"]
        if data.get("running") is False and data.get("finished_at"):
            return data
        time.sleep(0.05)
    raise AssertionError("import job did not finish in time")


def test_import_status_none_before_any_run(authenticated_client):
    r = authenticated_client.get("/api/import/status")
    assert r.status_code == 200
    assert r.json()["data"] == {"running": False}


def test_post_import_requires_auth(auth_client):
    """Anonymous (no session) -> 401 (the route-walk allowlist excludes it)."""
    r = auth_client.post(
        "/api/import/readwise",
        files={"file": ("x.json", b"[]", "application/json")},
    )
    assert r.status_code == 401


def test_unknown_kind_400(authenticated_client):
    r = authenticated_client.post(
        "/api/import/bogus",
        files={"file": ("x.json", b"[]", "application/json")},
    )
    assert r.status_code == 400


def test_empty_upload_400(authenticated_client):
    r = authenticated_client.post(
        "/api/import/readwise",
        files={"file": ("x.json", b"", "application/json")},
    )
    assert r.status_code == 400


def test_second_post_while_running_409(authenticated_client):
    """A single-slot job: a second start while one is active -> 409
    import_running. Seeded deterministically (a real tiny job can finish before
    a second request lands)."""
    authenticated_client.app.state.import_job = {"kind": "readwise", "running": True}
    r = authenticated_client.post(
        "/api/import/readwise",
        files={"file": ("x.json", b"[]", "application/json")},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["error"] == "import_running"
    # Clean up so the fixture's app doesn't leak a fake running job.
    authenticated_client.app.state.import_job = None


def test_import_runs_and_reports_summary(authenticated_client):
    """POST starts a job (202 ack), status is live while running, and the final
    report carries the summary counts (a Readwise export with 2 URL items ->
    2 imported)."""
    export = json.dumps(
        [
            {"title": "One", "source_url": "https://ex.test/1", "highlights": []},
            {"title": "Two", "source_url": "https://ex.test/2", "highlights": []},
            {"title": "NoURL", "highlights": []},  # skipped by the adapter
        ]
    ).encode()

    r = authenticated_client.post(
        "/api/import/readwise",
        files={"file": ("readwise.json", export, "application/json")},
    )
    assert r.status_code == 202
    ack = r.json()["data"]
    assert ack["kind"] == "readwise"
    assert ack["running"] is True
    assert ack["started_at"]

    final = _poll_until_done(authenticated_client)
    assert final["kind"] == "readwise"
    assert final["error"] is None
    assert final["imported"] == 2
    assert final["finished_at"]


def test_import_status_shape_keys(authenticated_client):
    """The status dict exposes the D6 progress fields once a job has run."""
    export = json.dumps(
        [{"title": "S", "source_url": "https://ex.test/s", "highlights": []}]
    ).encode()
    authenticated_client.post(
        "/api/import/readwise",
        files={"file": ("s.json", export, "application/json")},
    )
    final = _poll_until_done(authenticated_client)
    for key in (
        "kind", "running", "total", "processed", "imported", "skipped", "failed",
        "stub_articles", "highlights_imported", "highlights_skipped",
        "error", "started_at", "finished_at",
    ):
        assert key in final

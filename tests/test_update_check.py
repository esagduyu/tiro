"""Notify-only update check (spec D5): version comparison, the GitHub Releases
request (exact URL + headers), ETag round-trip / 304, failure isolation, the
audit line, banner-context injection, and the config kill switch.

The suite is offline: conftest's autouse ``_no_update_check`` fixture patches
``tiro.update_check.fetch_latest`` to a no-op so no app startup phones home.
These tests exercise the REAL worker via ``_REAL_FETCH`` (captured at import,
before the fixture patches the module attribute) with an injected transport.
"""

import json

import httpx
import pytest

import tiro.update_check as update_check
from tiro.audit import read_audit_entries
from tiro.update_check import (
    RELEASES_URL,
    UPDATE_CHECK_INTERVAL_SECONDS,
    is_newer,
    update_context,
)

# Real network worker, captured before the autouse fixture stubs the attribute.
_REAL_FETCH = update_check.fetch_latest


# ---------------------------------------------------------------------------
# Version comparison table (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag,current,expected",
    [
        ("0.7.0", "0.6.0", True),        # newer
        ("v0.7.0", "0.6.0", True),       # newer, v-prefix
        ("0.6.1", "0.6.0", True),        # patch newer
        ("0.6.0", "0.6.0", False),       # equal
        ("v0.6.0", "0.6.0", False),      # equal, v-prefix
        ("0.5.9", "0.6.0", False),       # older
        ("garbage", "0.6.0", False),     # malformed → not newer
        ("", "0.6.0", False),            # empty → not newer
        ("v", "0.6.0", False),           # bare v → not newer
        ("1.2.3.4", "1.2.3", True),      # extra component compares greater
    ],
)
def test_is_newer_table(tag, current, expected):
    assert is_newer(tag, current) is expected


def test_is_newer_never_raises_on_junk():
    for junk in (None, "..", "x.y.z", "0.a.0", "beta"):
        assert is_newer(junk, "0.6.0") is False


def test_interval_is_24h():
    assert UPDATE_CHECK_INTERVAL_SECONDS == 86_400


# ---------------------------------------------------------------------------
# The request: exact URL + headers
# ---------------------------------------------------------------------------


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_request_url_and_headers(test_config):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["accept"] = request.headers.get("accept")
        seen["api_version"] = request.headers.get("x-github-api-version")
        seen["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(
            200,
            content=json.dumps({"tag_name": "v0.9.0", "html_url": "https://x/rel"}),
            headers={"ETag": '"abc"'},
        )

    with _client(handler) as c:
        _REAL_FETCH(test_config, {}, client=c)

    assert seen["url"] == RELEASES_URL
    assert seen["accept"] == "application/vnd.github+json"
    assert seen["api_version"] == "2022-11-28"
    # Version-string-free UA: a bare product token, no version on the wire.
    assert seen["user_agent"] == "Tiro"


def test_200_stores_etag_and_version(test_config):
    def handler(request):
        return httpx.Response(
            200,
            content=json.dumps(
                {"tag_name": "v0.9.0", "html_url": "https://github.com/x/releases/v0.9.0"}
            ),
            headers={"ETag": '"etag-1"'},
        )

    with _client(handler) as c:
        state = _REAL_FETCH(test_config, {}, client=c)

    assert state["etag"] == '"etag-1"'
    assert state["latest_version"] == "v0.9.0"
    assert state["html_url"] == "https://github.com/x/releases/v0.9.0"
    assert state["checked_at"]


def test_304_sends_if_none_match_and_preserves_state(test_config):
    seen = {}

    def handler(request):
        seen["inm"] = request.headers.get("if-none-match")
        return httpx.Response(304, headers={"ETag": '"ignored"'})

    prior = {
        "etag": '"held"',
        "latest_version": "v0.9.0",
        "html_url": "https://x/rel",
        "checked_at": "2020-01-01T00:00:00+00:00",
    }
    with _client(handler) as c:
        state = _REAL_FETCH(test_config, prior, client=c)

    assert seen["inm"] == '"held"'  # conditional request sent
    # Held result reused; only checked_at is allowed to change.
    assert state["etag"] == '"held"'
    assert state["latest_version"] == "v0.9.0"
    assert state["html_url"] == "https://x/rel"
    assert state["checked_at"] != prior["checked_at"]


def test_network_error_leaves_state_unchanged_no_raise(test_config):
    def handler(request):
        raise httpx.ConnectError("offline")

    prior = {"etag": '"held"', "latest_version": "v0.9.0", "html_url": "https://x/rel",
             "checked_at": "2020-01-01T00:00:00+00:00"}
    with _client(handler) as c:
        state = _REAL_FETCH(test_config, prior, client=c)

    assert state == prior  # entirely unchanged — no raise, no checked_at bump


def test_non_200_status_leaves_state_unchanged(test_config):
    def handler(request):
        return httpx.Response(403, content=b"rate limited")

    prior = {"latest_version": "v0.9.0", "html_url": "https://x/rel"}
    with _client(handler) as c:
        state = _REAL_FETCH(test_config, dict(prior), client=c)

    assert state.get("latest_version") == "v0.9.0"
    assert "checked_at" not in state


def test_malformed_json_does_not_raise(test_config):
    def handler(request):
        return httpx.Response(200, content=b"not json", headers={"ETag": '"e"'})

    with _client(handler) as c:
        state = _REAL_FETCH(test_config, {}, client=c)

    # No crash; nothing usable stored.
    assert state.get("latest_version") is None


# ---------------------------------------------------------------------------
# Audit line
# ---------------------------------------------------------------------------


def test_writes_audit_line(test_config):
    test_config.library.mkdir(parents=True, exist_ok=True)

    def handler(request):
        return httpx.Response(200, content=json.dumps({"tag_name": "v1.0.0"}),
                              headers={"ETag": '"e"'})

    with _client(handler) as c:
        _REAL_FETCH(test_config, {}, client=c)

    entries = read_audit_entries(test_config, service="update-check")
    assert len(entries) == 1
    assert entries[0]["service"] == "update-check"
    assert entries[0]["success"] is True
    assert entries[0]["duration_ms"] is not None


def test_audit_line_on_failure_marks_success_false(test_config):
    test_config.library.mkdir(parents=True, exist_ok=True)

    def handler(request):
        raise httpx.ConnectError("offline")

    with _client(handler) as c:
        _REAL_FETCH(test_config, {}, client=c)

    entries = read_audit_entries(test_config, service="update-check")
    assert len(entries) == 1
    assert entries[0]["success"] is False
    assert entries[0]["error"]


# ---------------------------------------------------------------------------
# Banner context injection
# ---------------------------------------------------------------------------


def test_update_context_positive_only_when_newer():
    ctx = update_context({"latest_version": "v99.0.0", "html_url": "https://x"}, current="0.6.0")
    assert ctx["update_available"] is True
    assert ctx["update_version"] == "99.0.0"  # v-stripped for display
    assert ctx["update_url"] == "https://x"


def test_update_context_negative_when_equal_or_older():
    for held in ("0.6.0", "0.5.0", None):
        ctx = update_context({"latest_version": held} if held else {}, current="0.6.0")
        assert ctx["update_available"] is False
        assert ctx["update_version"] is None
        assert ctx["update_url"] is None


def test_update_context_handles_empty_state():
    ctx = update_context(None, current="0.6.0")
    assert ctx == {"update_available": False, "update_version": None, "update_url": None}


# ---------------------------------------------------------------------------
# Scheduler registration + kill switch (integration; fetch stubbed offline)
# ---------------------------------------------------------------------------


def test_loop_registered_when_enabled(client):
    # default config → update_check_enabled True
    status = client.app.state.scheduler.periodic_status()
    assert "update_check" in status
    assert client.app.state.update_check_task is not None


def test_loop_not_registered_when_disabled(initialized_library):
    from fastapi.testclient import TestClient

    from tiro.app import create_app

    initialized_library.update_check_enabled = False
    app = create_app(initialized_library)
    with TestClient(app, base_url="http://localhost") as c:
        assert "update_check" not in c.app.state.scheduler.periodic_status()
        assert c.app.state.update_check_task is None
        # state still present + empty so _theme_context/healthz read safely
        assert c.app.state.update_state == {}


# ---------------------------------------------------------------------------
# Banner markup present/absent per held state (renders base.html)
# ---------------------------------------------------------------------------


def test_banner_renders_when_newer_release_held(authenticated_client):
    authenticated_client.app.state.update_state = {
        "latest_version": "v99.0.0",
        "html_url": "https://github.com/esagduyu/tiro/releases/v99.0.0",
    }
    resp = authenticated_client.get("/inbox", follow_redirects=True)
    assert resp.status_code == 200
    assert 'id="update-banner"' in resp.text
    assert 'data-update-version="99.0.0"' in resp.text
    assert "https://github.com/esagduyu/tiro/releases/v99.0.0" in resp.text


def test_banner_absent_when_no_update(authenticated_client):
    authenticated_client.app.state.update_state = {}
    resp = authenticated_client.get("/inbox", follow_redirects=True)
    assert resp.status_code == 200
    assert 'id="update-banner"' not in resp.text


def test_banner_absent_when_held_not_newer(authenticated_client):
    authenticated_client.app.state.update_state = {"latest_version": "0.0.1"}
    resp = authenticated_client.get("/inbox", follow_redirects=True)
    assert resp.status_code == 200
    assert 'id="update-banner"' not in resp.text

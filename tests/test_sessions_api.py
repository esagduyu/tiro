"""Reading-session telemetry API (tiro/api/routes_sessions.py, Phase 2
M2.3 Task 1).

Endpoint is POST (not the roadmap's original PATCH wording) — a plan-level
decision, since the reader-side tracker (Task 2) sends via
`navigator.sendBeacon`, which can only issue POST requests.
"""

import json

from tiro.database import get_connection
from tiro.migrations import new_ulid


def _seed_article(config, title="T"):
    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
            " VALUES (?, ?, ?, 'sl', 'f.md')",
            (new_ulid(), source_id, title),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return article_id
    finally:
        conn.close()


def _sessions(config, article_id):
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM reading_sessions WHERE article_id = ?", (article_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


VALID_BODY = {
    "started_at": "2026-07-05T10:00:00Z",
    "max_scroll_pct": 87,
    "active_seconds": 42,
    "dwell": [{"heading": "Intro", "seconds": 10}, {"heading": "Body", "seconds": 32}],
}


# --- happy path ---------------------------------------------------------


def test_session_recorded_when_telemetry_enabled(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    r = authenticated_client.post(
        f"/api/articles/{article_id}/session", json=VALID_BODY
    )
    assert r.status_code in (200, 201), r.text

    rows = _sessions(configured_library, article_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["uid"]
    assert row["started_at"] == "2026-07-05T10:00:00Z"
    assert row["ended_at"]  # server-set now
    assert row["max_scroll_pct"] == 87
    assert row["active_seconds"] == 42
    dwell = json.loads(row["dwell_json"])
    assert dwell == [
        {"heading": "Intro", "seconds": 10},
        {"heading": "Body", "seconds": 32},
    ]


def test_session_multiple_visits_insert_multiple_rows(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    authenticated_client.post(f"/api/articles/{article_id}/session", json=VALID_BODY)
    authenticated_client.post(f"/api/articles/{article_id}/session", json=VALID_BODY)

    assert len(_sessions(configured_library, article_id)) == 2


# --- 404 ------------------------------------------------------------------


def test_session_unknown_article_404(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    r = authenticated_client.post("/api/articles/999999/session", json=VALID_BODY)
    assert r.status_code == 404


# --- 400 malformed ----------------------------------------------------------


def test_session_malformed_json_body_400(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)
    r = authenticated_client.post(
        f"/api/articles/{article_id}/session",
        content=b"not json{{{",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_session_wrong_shape_dwell_400(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)
    r = authenticated_client.post(
        f"/api/articles/{article_id}/session",
        json={"max_scroll_pct": 10, "active_seconds": 5, "dwell": "not-a-list"},
    )
    assert r.status_code == 400


def test_session_dwell_entry_wrong_type_400(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)
    r = authenticated_client.post(
        f"/api/articles/{article_id}/session",
        json={
            "max_scroll_pct": 10,
            "active_seconds": 5,
            "dwell": [{"heading": 123, "seconds": "notanumber"}],
        },
    )
    assert r.status_code == 400


# --- 204 no-op when disabled -------------------------------------------------


def test_session_204_and_no_row_when_disabled(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = False
    article_id = _seed_article(configured_library)

    r = authenticated_client.post(
        f"/api/articles/{article_id}/session", json=VALID_BODY
    )
    assert r.status_code == 204
    assert _sessions(configured_library, article_id) == []


def test_session_204_even_for_unknown_article_when_disabled(authenticated_client, configured_library):
    """Privacy posture: the disabled check happens before article lookup, so
    a disabled server never leaks whether an article id exists via this
    endpoint either."""
    configured_library.reading_telemetry_enabled = False
    r = authenticated_client.post("/api/articles/999999/session", json=VALID_BODY)
    assert r.status_code == 204


# --- clamps -----------------------------------------------------------------


def test_session_clamps_out_of_range_values(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {
        "started_at": "2026-07-05T10:00:00Z",
        "max_scroll_pct": 150,       # > 100 -> clamp to 100
        "active_seconds": -5,       # negative -> clamp to 0
        "dwell": [{"heading": "x" * 500, "seconds": -1}],
    }
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text

    row = _sessions(configured_library, article_id)[0]
    assert row["max_scroll_pct"] == 100
    assert row["active_seconds"] == 0
    dwell = json.loads(row["dwell_json"])
    assert len(dwell[0]["heading"]) == 200
    assert dwell[0]["seconds"] == 0


def test_session_active_seconds_capped_at_86400(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {**VALID_BODY, "active_seconds": 999999}
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    assert row["active_seconds"] == 86400


def test_session_dwell_capped_at_100_entries(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {
        **VALID_BODY,
        "dwell": [{"heading": f"h{i}", "seconds": 1} for i in range(150)],
    }
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    dwell = json.loads(row["dwell_json"])
    assert len(dwell) == 100


# --- auth --------------------------------------------------------------------


def test_session_endpoint_requires_auth(auth_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)
    r = auth_client.post(f"/api/articles/{article_id}/session", json=VALID_BODY)
    assert r.status_code == 401

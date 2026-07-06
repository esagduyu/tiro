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


def test_session_204_even_for_malformed_body_when_disabled(authenticated_client, configured_library):
    """Privacy posture (review finding 2): the disabled check happens before
    request.json()/validation too, so a malformed body never 400s on a
    disabled server -- 'disabled means disabled' with no exceptions, and no
    row is inserted."""
    configured_library.reading_telemetry_enabled = False
    article_id = _seed_article(configured_library)
    r = authenticated_client.post(
        f"/api/articles/{article_id}/session",
        content=b"not json{{{",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 204
    assert _sessions(configured_library, article_id) == []


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


# --- exact boundary clamps (review finding 3) --------------------------------


def test_session_max_scroll_pct_exactly_100_not_clamped(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {**VALID_BODY, "max_scroll_pct": 100}
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    assert row["max_scroll_pct"] == 100


def test_session_max_scroll_pct_101_clamped_to_100(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {**VALID_BODY, "max_scroll_pct": 101}
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    assert row["max_scroll_pct"] == 100


def test_session_dwell_exactly_100_entries_all_kept(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {
        **VALID_BODY,
        "dwell": [{"heading": f"h{i}", "seconds": 1} for i in range(100)],
    }
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    dwell = json.loads(row["dwell_json"])
    assert len(dwell) == 100


def test_session_dwell_101_entries_truncated_to_100(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {
        **VALID_BODY,
        "dwell": [{"heading": f"h{i}", "seconds": 1} for i in range(101)],
    }
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    dwell = json.loads(row["dwell_json"])
    assert len(dwell) == 100


def test_session_heading_exactly_200_chars_kept(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {**VALID_BODY, "dwell": [{"heading": "x" * 200, "seconds": 1}]}
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    dwell = json.loads(row["dwell_json"])
    assert len(dwell[0]["heading"]) == 200


def test_session_heading_201_chars_truncated_to_200(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {**VALID_BODY, "dwell": [{"heading": "x" * 201, "seconds": 1}]}
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    dwell = json.loads(row["dwell_json"])
    assert len(dwell[0]["heading"]) == 200


def test_session_started_at_exactly_40_chars_kept(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {**VALID_BODY, "started_at": "s" * 40}
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    assert row["started_at"] == "s" * 40


def test_session_started_at_over_40_chars_trimmed(authenticated_client, configured_library):
    """Review finding 3 (LOW): started_at was unclamped -- a hostile or
    buggy client could stuff an arbitrarily long string in. Trim to 40 chars
    (mirrors the trim style used for dwell headings, not a 400)."""
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {**VALID_BODY, "started_at": "s" * 500}
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    assert row["started_at"] == "s" * 40
    assert len(row["started_at"]) == 40


def test_session_started_at_none_stays_none(authenticated_client, configured_library):
    """started_at is Optional -- the clamp must not choke on None."""
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)

    body = {**VALID_BODY, "started_at": None}
    r = authenticated_client.post(f"/api/articles/{article_id}/session", json=body)
    assert r.status_code in (200, 201), r.text
    row = _sessions(configured_library, article_id)[0]
    assert row["started_at"] is None


# --- auth --------------------------------------------------------------------


def test_session_endpoint_requires_auth(auth_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    article_id = _seed_article(configured_library)
    r = auth_client.post(f"/api/articles/{article_id}/session", json=VALID_BODY)
    assert r.status_code == 401


# --- reader.html template threading (Task 2) ----------------------------------
#
# The reader-side tracker in reader.js is gated entirely on `#reader`'s
# `data-telemetry` attribute (see reader.js's `setupTelemetry`) — the route
# handler in tiro/app.py must thread `config.reading_telemetry_enabled`
# through into that attribute the same way `_theme_context` threads theme
# hrefs, on every request (not just at startup), so a live toggle via
# `POST /api/settings/telemetry` takes effect on the next reader page load
# without a server restart.


def test_reader_page_data_telemetry_off_by_default(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = False
    r = authenticated_client.get("/articles/1")
    assert r.status_code == 200
    assert 'data-telemetry="off"' in r.text


def test_reader_page_data_telemetry_on_when_enabled(authenticated_client, configured_library):
    configured_library.reading_telemetry_enabled = True
    r = authenticated_client.get("/articles/1")
    assert r.status_code == 200
    assert 'data-telemetry="on"' in r.text

import json

from tiro.api.routes_views import MAX_SAVED_VIEWS
from tiro.database import get_connection


def _seed_view(config, name, position, filter_json='{"tag": "ai"}', sort_mode="unread"):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "INSERT INTO saved_views (uid, name, filter_json, sort_mode, position)"
            " VALUES (?, ?, ?, ?, ?)",
            (name[:26].ljust(26, "0"), name, filter_json, sort_mode, position),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    finally:
        conn.close()


# --- GET /api/views -----------------------------------------------------


def test_list_views_empty(authenticated_client, configured_library):
    r = authenticated_client.get("/api/views")
    assert r.status_code == 200, r.text
    assert r.json() == {"success": True, "data": []}


def test_list_views_ordered_by_position_then_id(authenticated_client, configured_library):
    _seed_view(configured_library, "Second", position=1)
    _seed_view(configured_library, "First", position=0)
    third = _seed_view(configured_library, "Third-tiebreak", position=1)

    r = authenticated_client.get("/api/views")
    assert r.status_code == 200, r.text
    names = [row["name"] for row in r.json()["data"]]
    assert names == ["First", "Second", "Third-tiebreak"]
    # tiebreak within same position is by id ASC
    same_pos = [row for row in r.json()["data"] if row["position"] == 1]
    assert [row["id"] for row in same_pos][-1] == third

    row = r.json()["data"][0]
    assert set(row.keys()) == {"id", "uid", "name", "filter_json", "sort_mode", "position"}


# --- POST /api/views ------------------------------------------------------


def test_create_view_round_trip(authenticated_client, configured_library):
    body = {"name": "My View", "filter_json": json.dumps({"tag": "ai", "rating_min": 1})}
    r = authenticated_client.post("/api/views", json=body)
    assert r.status_code == 200, r.text
    body_json = r.json()
    assert body_json["success"] is True
    data = body_json["data"]
    assert data["name"] == "My View"
    assert json.loads(data["filter_json"]) == {"tag": "ai", "rating_min": 1}
    assert data["sort_mode"] == "unread"  # default
    assert data["position"] == 0
    assert len(data["uid"]) == 26

    # Round-trips through GET too.
    r2 = authenticated_client.get("/api/views")
    assert r2.json()["data"] == [data]


def test_create_view_custom_sort_mode(authenticated_client, configured_library):
    r = authenticated_client.post(
        "/api/views",
        json={"name": "V", "filter_json": "{}", "sort_mode": "newest"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["sort_mode"] == "newest"


def test_create_view_position_is_max_plus_one(authenticated_client, configured_library):
    _seed_view(configured_library, "Existing A", position=0)
    _seed_view(configured_library, "Existing B", position=5)

    r = authenticated_client.post(
        "/api/views", json={"name": "New", "filter_json": "{}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["position"] == 6


def test_create_view_filter_json_not_json_400(authenticated_client, configured_library):
    r = authenticated_client.post(
        "/api/views", json={"name": "Bad", "filter_json": "not json at all"}
    )
    assert r.status_code == 400


def test_create_view_filter_json_array_400(authenticated_client, configured_library):
    r = authenticated_client.post(
        "/api/views", json={"name": "Bad Array", "filter_json": json.dumps([1, 2, 3])}
    )
    assert r.status_code == 400


def test_create_view_filter_json_scalar_400(authenticated_client, configured_library):
    r = authenticated_client.post(
        "/api/views", json={"name": "Bad Scalar", "filter_json": json.dumps("just a string")}
    )
    assert r.status_code == 400


def test_create_view_name_length_bounds(authenticated_client, configured_library):
    r = authenticated_client.post(
        "/api/views", json={"name": "", "filter_json": "{}"}
    )
    assert r.status_code == 422

    r = authenticated_client.post(
        "/api/views", json={"name": "x" * 101, "filter_json": "{}"}
    )
    assert r.status_code == 422

    r = authenticated_client.post(
        "/api/views", json={"name": "x" * 100, "filter_json": "{}"}
    )
    assert r.status_code == 200, r.text


def test_create_view_cap_at_20(authenticated_client, configured_library):
    for i in range(MAX_SAVED_VIEWS):
        r = authenticated_client.post(
            "/api/views", json={"name": f"View {i}", "filter_json": "{}"}
        )
        assert r.status_code == 200, r.text

    r = authenticated_client.post(
        "/api/views", json={"name": "One too many", "filter_json": "{}"}
    )
    assert r.status_code == 400

    conn = get_connection(configured_library.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM saved_views").fetchone()["n"]
    finally:
        conn.close()
    assert count == MAX_SAVED_VIEWS


# --- PATCH /api/views/{id} --------------------------------------------------


def test_patch_view_name(authenticated_client, configured_library):
    view_id = _seed_view(configured_library, "Original", position=0)
    r = authenticated_client.patch(f"/api/views/{view_id}", json={"name": "Renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["name"] == "Renamed"


def test_patch_view_position(authenticated_client, configured_library):
    view_id = _seed_view(configured_library, "Reorder Me", position=0)
    r = authenticated_client.patch(f"/api/views/{view_id}", json={"position": 3})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["position"] == 3


def test_patch_view_both_fields(authenticated_client, configured_library):
    view_id = _seed_view(configured_library, "Both", position=0)
    r = authenticated_client.patch(
        f"/api/views/{view_id}", json={"name": "Both Renamed", "position": 2}
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["name"] == "Both Renamed"
    assert data["position"] == 2


def test_patch_view_missing_404(authenticated_client, configured_library):
    r = authenticated_client.patch("/api/views/999999", json={"name": "Nope"})
    assert r.status_code == 404


# --- DELETE /api/views/{id} --------------------------------------------------


def test_delete_view(authenticated_client, configured_library):
    view_id = _seed_view(configured_library, "Delete Me", position=0)
    r = authenticated_client.delete(f"/api/views/{view_id}")
    assert r.status_code == 200, r.text

    conn = get_connection(configured_library.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM saved_views WHERE id = ?", (view_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_delete_view_missing_404(authenticated_client, configured_library):
    r = authenticated_client.delete("/api/views/999999")
    assert r.status_code == 404


# --- Frontend pins (Task 8: saved views UI) ---------------------------------


def test_base_html_has_sidebar_views_section(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'id="sidebar-views"' in r.text
    assert 'id="sidebar-views-list"' in r.text


def test_inbox_has_save_view_button(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'id="filter-save-view-btn"' in r.text


def test_sidebar_js_has_saved_views_functions():
    from pathlib import Path

    # app.js was split into js/sidebar.js + js/inbox.js in M2.0 Task 2 (see
    # docs/plans/2026-07-05-m2-0-frontend-modules-plan.md) — the saved-views
    # section is page chrome (every page), so it lives in sidebar.js now.
    sidebar_js = Path(__file__).parent.parent / "tiro" / "frontend" / "static" / "js" / "sidebar.js"
    content = sidebar_js.read_text()
    assert "function loadSavedViews" in content
    assert "function renderSavedViews" in content


def test_static_version_bumped_for_saved_views_ui():
    from tiro.app import STATIC_VERSION

    # Bumped again for Task 9 (/wiki views) -- see test_wiki_views.py for the
    # pin that owns "59" specifically; this test's job is just to confirm the
    # saved-views-era bump wasn't silently reverted.
    assert STATIC_VERSION in ("58", "59")

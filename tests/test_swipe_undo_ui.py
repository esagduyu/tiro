"""Swipe-triage + undo wiring pins and API extensions (M3.2 Task 3).

Structural pins mirror test_snooze_ui.py's style (grep the actual static
JS/CSS source for the wiring hooks — fast, no browser). Full gesture
interaction is covered by playwright-tests/swipe-triage.spec.js.

API tests cover the two minimal body extensions this task added to EXISTING
routes (documented as deviations in .superpowers/sdd/task-3-report.md):

- ``PATCH /api/articles/{id}/read`` with ``{"is_read": false}`` marks an
  article unread (the undo-archive path) WITHOUT incrementing
  ``opened_count`` and WITHOUT touching reading stats (transition-gated
  monotonic counters — un-reading never decrements).
- ``PATCH /api/articles/{id}/rate`` with ``{"rating": null}`` clears the
  rating back to unrated (the undo-rate path when the prior rating was
  NULL), likewise never decrementing ``articles_rated``.
"""

from datetime import date
from pathlib import Path

from tiro.database import get_connection

STATIC_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "static"


def _ingest_one(client):
    eml = (Path(__file__).parent / "fixtures" / "newsletter.eml").read_bytes()
    r = client.post("/api/ingest/email",
                    files={"file": ("newsletter.eml", eml, "message/rfc822")})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def _article_row(config, aid):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            "SELECT is_read, opened_count, rating FROM articles WHERE id = ?",
            (aid,),
        ).fetchone()
    finally:
        conn.close()


def _today_stats(config):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            "SELECT * FROM reading_stats WHERE date = ?",
            (date.today().isoformat(),),
        ).fetchone()
    finally:
        conn.close()


# --- Structural pins: swipe wiring -------------------------------------------


def test_inbox_js_imports_swipe_and_undo_cores():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert 'from "./swipe.js"' in content
    assert 'from "./undo.js"' in content
    assert "createSwipeState" in content
    assert "swipeEvent" in content
    assert "createUndoManager" in content


def test_inbox_js_wires_delegated_pointer_handlers():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function setupSwipe" in content
    for ev in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
        assert ev in content
    assert "setPointerCapture" in content


def test_inbox_js_guards_zero_width_card():
    # T2 review edge: a 0/NaN cardWidth must not engage the gesture at all.
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "Number.isFinite(width)" in content


def test_inbox_js_has_archive_and_snooze_swipe_actions():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function performArchive" in content
    assert '"archive"' in content
    assert '"snooze-sheet"' in content


def test_styles_css_has_touch_action_and_reduced_motion():
    content = (STATIC_DIR / "styles.css").read_text()
    assert "touch-action: pan-y" in content
    assert "prefers-reduced-motion" in content
    assert ".article-card.swipe-snap-back" in content
    assert ".article-card.swipe-right-hint" in content
    assert ".article-card.swipe-left-hint" in content


# --- Structural pins: undo binder --------------------------------------------


def test_inbox_js_has_undo_binder():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function offerUndo" in content
    assert "function triggerUndo" in content
    assert "UNDO_WINDOW_MS" in content
    assert "undo-toast-btn" in content


def test_inbox_js_binds_u_to_undo():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert 'case "u":' in content


def test_sidebar_shortcuts_document_undo():
    content = (STATIC_DIR / "js" / "sidebar.js").read_text()
    assert '"u"' in content
    assert "Undo" in content


def test_styles_css_has_undo_toast():
    content = (STATIC_DIR / "styles.css").read_text()
    assert ".undo-toast" in content
    assert "pointer-events: auto" in content


# --- Structural pins: M3.2 final review fixes (Findings 1, 2, 4, 7) ----------
#
# Full behavioral coverage for the race (Finding 2) lives in
# playwright-tests/swipe-triage.spec.js; these are fast source-level pins
# for the shape of each fix (grep-the-source, same style as the pins above).


def test_inbox_js_has_per_entity_action_tokens():
    # Finding 2: the per-article/per-source sequence-token guard.
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function bumpActionToken" in content
    assert "function isStaleActionToken" in content
    assert "function articleTokenKey" in content
    assert "function sourceTokenKey" in content
    # Wired into every undo-adjacent action named in the finding.
    for fn in ("rateSelected", "performArchive", "performSnooze", "toggleSelectedVip"):
        # Rough proximity check: the function body (up to the next
        # top-level function) contains a staleness check.
        start = content.index(f"function {fn}(")
        end = content.index("\nfunction ", start + 1)
        assert "isStaleActionToken" in content[start:end], fn


def test_inbox_js_skips_undo_on_cache_miss():
    # Finding 1: a card driven purely by search results (not in
    # cachedArticles) must not get a fabricated-prior undo offer.
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "Fabricated-prior guard" in content
    # Both performArchive and rateSelected must skip offerUndo on a miss.
    for fn in ("performArchive", "rateSelected"):
        start = content.index(f"function {fn}(")
        end = content.index("\nfunction ", start + 1)
        body = content[start:end]
        assert "if (!article)" in body
        assert "showToast(" in body


def test_inbox_js_search_active_removes_card_in_place():
    # Finding 1: performArchive/performSnooze must not clobber live search
    # results by re-rendering a stale cachedArticles snapshot mid-search.
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "searchActive" in content
    for fn in ("performArchive", "performSnooze"):
        start = content.index(f"function {fn}(")
        end = content.index("\nfunction ", start + 1)
        body = content[start:end]
        assert "if (searchActive)" in body
        assert ".remove()" in body


def test_inbox_js_mouse_rate_click_updates_cache():
    # Finding 4: the mouse rate-button click handler must keep
    # cachedArticles in sync so a later keyboard undo capture is accurate.
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    start = content.index('document.querySelectorAll(".rate-btn")')
    end = content.index("\n    // Card overflow menu", start)
    body = content[start:end]
    assert "article.rating = rating" in body


def test_core_js_show_toast_excludes_undo_toast():
    # Finding 7: showToast() must not silently remove a live #undo-toast
    # (it manages its own lifecycle independently).
    content = (STATIC_DIR / "js" / "core.js").read_text()
    start = content.index("export function showToast")
    end = content.index("\n}", start)
    body = content[start:end]
    assert ":not(#undo-toast)" in body


# --- API: PATCH read {"is_read": false} (undo-archive) ------------------------


def test_read_no_body_still_marks_read(authenticated_client, configured_library):
    aid = _ingest_one(authenticated_client)
    r = authenticated_client.patch(f"/api/articles/{aid}/read")
    assert r.status_code == 200
    assert r.json()["data"]["is_read"] == 1
    row = _article_row(configured_library, aid)
    assert row["is_read"] == 1
    assert row["opened_count"] == 1


def test_unread_clears_flag_without_touching_open_count_or_stats(
    authenticated_client, configured_library
):
    aid = _ingest_one(authenticated_client)
    authenticated_client.patch(f"/api/articles/{aid}/read")
    stats_before = _today_stats(configured_library)["articles_read"]

    r = authenticated_client.patch(
        f"/api/articles/{aid}/read", json={"is_read": False}
    )
    assert r.status_code == 200
    assert r.json()["data"]["is_read"] == 0

    row = _article_row(configured_library, aid)
    assert row["is_read"] == 0
    assert row["opened_count"] == 1  # unmark never counts as an open

    # Monotonic counters: un-reading never decrements articles_read.
    assert _today_stats(configured_library)["articles_read"] == stats_before


def test_unread_then_reread_counts_stats_again(
    authenticated_client, configured_library
):
    # Transition-gated on 0 -> 1: a re-read after an unmark is a new
    # transition and counts again (documented, accepted semantics).
    aid = _ingest_one(authenticated_client)
    authenticated_client.patch(f"/api/articles/{aid}/read")
    authenticated_client.patch(f"/api/articles/{aid}/read", json={"is_read": False})
    authenticated_client.patch(f"/api/articles/{aid}/read")
    assert _today_stats(configured_library)["articles_read"] == 2
    assert _article_row(configured_library, aid)["opened_count"] == 2


def test_unread_unknown_article_404(authenticated_client):
    r = authenticated_client.patch(
        "/api/articles/999999/read", json={"is_read": False}
    )
    assert r.status_code == 404


def test_unread_on_already_unread_article_is_idempotent(
    authenticated_client, configured_library
):
    aid = _ingest_one(authenticated_client)
    r = authenticated_client.patch(
        f"/api/articles/{aid}/read", json={"is_read": False}
    )
    assert r.status_code == 200
    row = _article_row(configured_library, aid)
    assert row["is_read"] == 0
    assert row["opened_count"] == 0


# --- API: PATCH rate {"rating": null} (undo-rate) -----------------------------


def test_rate_null_clears_rating(authenticated_client, configured_library):
    aid = _ingest_one(authenticated_client)
    assert authenticated_client.patch(
        f"/api/articles/{aid}/rate", json={"rating": 2}
    ).status_code == 200

    r = authenticated_client.patch(
        f"/api/articles/{aid}/rate", json={"rating": None}
    )
    assert r.status_code == 200
    assert r.json()["data"]["rating"] is None
    assert _article_row(configured_library, aid)["rating"] is None


def test_rate_invalid_value_still_400(authenticated_client):
    aid = _ingest_one(authenticated_client)
    r = authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": 5})
    assert r.status_code == 400


# --- Finding 3 (M3.2 final review): RateRequest.rating required-but-nullable --
#
# The field previously defaulted to `None`, so an empty body `{}` (e.g. a
# client bug — a missing key) was silently indistinguishable from an
# explicit `{"rating": null}` clear: both hit the `body.rating is None`
# branch and cleared a real rating. `Field(...)` makes the KEY required
# (missing -> 422) while the `int | None` type still accepts an explicit
# null to clear.


def test_rate_empty_body_is_422_not_a_silent_clear(authenticated_client, configured_library):
    aid = _ingest_one(authenticated_client)
    authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": 2})

    r = authenticated_client.patch(f"/api/articles/{aid}/rate", json={})
    assert r.status_code == 422

    # The real rating must be untouched by the rejected request.
    assert _article_row(configured_library, aid)["rating"] == 2


def test_rate_missing_body_entirely_is_422(authenticated_client):
    aid = _ingest_one(authenticated_client)
    r = authenticated_client.patch(f"/api/articles/{aid}/rate")
    assert r.status_code == 422


def test_rate_explicit_null_still_clears(authenticated_client, configured_library):
    # The one case Finding 3 must NOT break: an explicit null still clears.
    aid = _ingest_one(authenticated_client)
    authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": 1})
    r = authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": None})
    assert r.status_code == 200
    assert _article_row(configured_library, aid)["rating"] is None


def test_rate_null_unknown_article_404(authenticated_client):
    r = authenticated_client.patch(
        "/api/articles/999999/rate", json={"rating": None}
    )
    assert r.status_code == 404


def test_rate_clear_never_decrements_stats_but_rerate_counts_again(
    authenticated_client, configured_library
):
    aid = _ingest_one(authenticated_client)
    authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": 1})
    assert _today_stats(configured_library)["articles_rated"] == 1

    authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": None})
    assert _today_stats(configured_library)["articles_rated"] == 1  # no decrement

    # NULL -> value is a first-rating transition again (documented).
    authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": 2})
    assert _today_stats(configured_library)["articles_rated"] == 2

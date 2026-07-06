"""Snooze triage UI wiring pins (M3.2 Task 1): inbox toolbar "Snoozed"
toggle, per-card overflow menu, and the snooze preset sheet.

Mirrors test_highlights_views.py's structural-pin style (grep the actual
static JS/template source for the DOM hooks, plus a rendered-page check for
the template-side button). Full interaction is covered by
playwright-tests/snooze-ui.spec.js — these are fast, no-browser-needed pins
that the wiring exists at all and doesn't silently regress.
"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "static"
TEMPLATES_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "templates"


# --- Toolbar toggle ----------------------------------------------------------


def test_inbox_html_has_snoozed_toggle_button():
    content = (TEMPLATES_DIR / "inbox.html").read_text()
    assert 'id="snoozed-toggle"' in content


def test_inbox_page_renders_snoozed_toggle(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'id="snoozed-toggle"' in r.text


def test_inbox_js_wires_snoozed_toggle():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert 'getElementById("snoozed-toggle")' in content
    assert "include_snoozed" in content
    assert "function toggleSnoozed" in content


def test_inbox_js_renders_wake_chip_and_button():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "snoozed-chip" in content
    assert "wake-now-btn" in content
    assert "is-snoozed" in content


# --- Card overflow menu ------------------------------------------------------


def test_inbox_js_has_card_overflow_menu():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "card-menu-btn" in content
    assert "card-menu-dropdown" in content
    assert 'data-action="snooze"' in content
    assert "function closeAllCardMenus" in content


# --- Snooze preset sheet ------------------------------------------------------


def test_inbox_js_has_snooze_preset_sheet():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function openSnoozeSheet" in content
    assert "snooze-sheet-overlay" in content
    assert "snooze-preset-btn" in content
    for preset in ("tonight", "tomorrow", "weekend", "next_week"):
        assert preset in content


def test_inbox_js_snooze_sheet_patches_preset():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function performSnooze" in content
    assert "/snooze" in content
    assert '"PATCH"' in content


# --- Styling ------------------------------------------------------------------


def test_styles_css_has_snooze_treatment():
    content = (STATIC_DIR / "styles.css").read_text()
    assert ".article-card.is-snoozed" in content
    assert ".card-menu-dropdown" in content
    assert ".snooze-preset-grid" in content

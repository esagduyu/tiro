"""Triage progress pill + inbox-zero state + logout SW-cache hardening
(M3.2 Task 4).

Mirrors test_snooze_ui.py's / test_swipe_undo_ui.py's structural-pin style
(grep the actual static JS/template source for the DOM hooks and wiring --
fast, no-browser pins that the feature exists at all and doesn't silently
regress). Full interaction (triage-to-zero, undo, logout cache clear) is
covered by playwright-tests/triage-pill.spec.js.
"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "static"
TEMPLATES_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "templates"


# --- "N to zero" pill ---------------------------------------------------------


def test_inbox_html_has_triage_pill():
    content = (TEMPLATES_DIR / "inbox.html").read_text()
    assert 'id="triage-pill"' in content


def test_inbox_page_renders_triage_pill(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'id="triage-pill"' in r.text


def test_inbox_js_renders_triage_pill_from_shared_unread_count():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function renderTriagePill" in content
    assert "to zero" in content
    # Sourced from sidebar.js's shared state, not a parallel local count.
    assert "getUnreadCount" in content
    assert "adjustUnreadCount" in content


def test_sidebar_js_exposes_shared_unread_count_state():
    content = (STATIC_DIR / "js" / "sidebar.js").read_text()
    assert "function getUnreadCount" in content
    assert "function adjustUnreadCount" in content
    assert "getUnreadCount, adjustUnreadCount" in content or (
        "getUnreadCount" in content and "adjustUnreadCount" in content
    )


def test_inbox_js_adjusts_unread_count_on_triage_actions():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    # Archive (unread -> read) decrements; undo-archive increments.
    assert "adjustUnreadCount(-1)" in content
    assert "adjustUnreadCount(1)" in content
    # Snooze/wake gate on whether the article actually leaves/re-enters the
    # unread-and-not-snoozed count (not every snooze/wake call).
    assert "leavesUnreadCount" in content
    assert "wasUnread" in content


# --- Finding 1 fix (review): snoozed-unread archive/delete must not drift
# the shared count -------------------------------------------------------


def test_inbox_js_has_single_shared_snoozed_and_unread_helpers():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function isCurrentlySnoozed" in content
    assert "function countsAsUnread" in content
    # countsAsUnread is unread AND not snoozed -- not just unread.
    assert "!a.is_read && !isCurrentlySnoozed(a)" in content


def test_inbox_js_archive_and_delete_use_countsAsUnread_not_bare_is_read():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    # performArchive: decrement AND undo-increment both gated on the SAME
    # captured boolean (wasCountedUnread), not a bare wasRead/is_read check
    # and not recomputed at undo time.
    assert "const wasCountedUnread = countsAsUnread(article);" in content
    assert "if (wasCountedUnread) adjustUnreadCount(-1);" in content
    assert "if (wasCountedUnread) adjustUnreadCount(1);" in content
    # performDelete: the deletedUnread filter routes through the shared
    # helper rather than a local `!a.is_read` check.
    assert "return countsAsUnread(a);" in content


def test_inbox_js_renderarticle_reuses_shared_snoozed_helper():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "const isSnoozed = isCurrentlySnoozed(a);" in content


def test_inbox_js_performsnooze_reuses_shared_snoozed_helper():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "const priorStillFuture = isCurrentlySnoozed(prior);" in content


# --- Finding 2 fix (review): pill re-renders once the count refetch settles,
# not just on the synchronous save event -----------------------------------


def test_sidebar_js_dispatches_event_when_unread_count_refetch_resolves():
    content = (STATIC_DIR / "js" / "sidebar.js").read_text()
    assert "function updateUnreadBadge" in content
    assert 'new CustomEvent("tiro:unread-count-updated")' in content


def test_inbox_js_listens_for_unread_count_updated_and_refreshes_pill():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert (
        'addEventListener("tiro:unread-count-updated", refreshTriageUI)' in content
    )


# --- Inbox-zero celebratory state ---------------------------------------------


def test_inbox_html_has_distinct_zero_state_from_onboarding_empty_state():
    content = (TEMPLATES_DIR / "inbox.html").read_text()
    assert 'id="inbox-zero-state"' in content
    # Onboarding empty-state copy must be untouched (never broken by this task).
    assert "No articles yet" in content
    assert "curl -X POST" in content
    # Zero-state copy is distinct wording, not a duplicate of onboarding copy.
    assert "Inbox zero" in content


def test_inbox_js_has_zero_state_logic_distinct_from_filtered_and_onboarding_empty():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function updateInboxZeroState" in content
    assert "function isDefaultTriageView" in content
    # Gated on a sticky "has this session ever seen real data" flag, not on
    # cachedArticles.length being literally 0 -- a real user's default view
    # usually still has read articles in it after triaging to zero unread,
    # and this must never be confused with the "never saved anything"
    # onboarding case (which never sets the flag; see loadInbox()).
    assert "libraryEverHadArticles" in content


def test_inbox_js_default_triage_view_excludes_filters_search_and_toggles():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "!searchActive" in content
    assert "!showArchived && !showSnoozed && !showVIPOnly" in content


def test_styles_css_has_inbox_zero_state_styling():
    content = (STATIC_DIR / "styles.css").read_text()
    assert ".inbox-zero-state" in content
    assert ".inbox-zero-mark" in content
    assert ".triage-pill" in content


# --- Logout SW-cache hardening -------------------------------------------------


def test_sidebar_js_clears_article_caches_on_logout():
    content = (STATIC_DIR / "js" / "sidebar.js").read_text()
    assert "logout-btn" in content
    assert "'caches' in window" in content
    assert "caches.keys()" in content
    assert "tiro-.*-articles" in content
    assert "caches.delete" in content


def test_logout_cache_clear_never_blocks_logout_flow():
    content = (STATIC_DIR / "js" / "sidebar.js").read_text()
    # The logout POST + redirect must still be present and reachable
    # regardless of the cache-clear branch (try/catch around it, not
    # wrapping the fetch/redirect too).
    assert "/api/auth/logout" in content
    assert "window.location.href = '/login'" in content
    # try/catch around the cache-clear specifically (best-effort posture).
    assert "Best-effort only" in content or "best-effort" in content.lower()

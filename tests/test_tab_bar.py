"""Phone chrome: bottom tab bar present, hamburger gone (design pass)."""


def test_base_pages_have_tab_bar_and_no_hamburger(authenticated_client):
    resp = authenticated_client.get("/inbox")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="tab-bar"' in html
    assert 'id="tab-save-btn"' in html
    assert 'id="library-sheet"' in html
    assert 'id="more-sheet"' in html
    assert 'id="mobile-menu-btn"' not in html
    # sidebar logout affordance must still exist alongside the sheet's
    assert 'id="logout-btn"' in html
    assert 'id="logout-btn-sheet"' in html


def test_login_page_has_no_tab_bar(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'id="tab-bar"' not in resp.text

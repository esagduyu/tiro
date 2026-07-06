"""M3.1 Task 1: PWA manifest + icons.

Covers the unauthenticated GET /manifest.webmanifest route in tiro/app.py,
the generated icon assets in tiro/frontend/static/icons/, and the manifest-
related tags in base.html. The route-walk allowlist entry for this path
lives in tests/test_auth.py (test_route_walk_everything_gated) alongside
its own explanatory comment, not here.
"""

import json

from tiro.app import FRONTEND_DIR


def test_manifest_fetchable_unauthenticated(auth_client):
    """A PWA install prompt can't fetch an authenticated resource before the
    user has a session -- the manifest must be reachable with zero auth."""
    r = auth_client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")


def test_manifest_is_valid_json_with_required_fields(auth_client):
    r = auth_client.get("/manifest.webmanifest")
    data = json.loads(r.content)
    assert data["name"] == "Tiro"
    assert data["short_name"] == "Tiro"
    assert data["display"] == "standalone"
    assert data["start_url"] == "/inbox"
    assert data["scope"] == "/"
    assert isinstance(data["icons"], list) and len(data["icons"]) >= 2
    for icon in data["icons"]:
        assert {"src", "sizes", "type"} <= icon.keys()
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert {"192x192", "512x512"} <= sizes


def test_manifest_theme_and_background_from_papyrus_palette(auth_client):
    """theme_color = --tiro-accent (terra cotta, the brand's chrome/UI tint
    everywhere else), background_color = --tiro-bg (papyrus cream, the
    splash-screen backdrop shown before the app itself has painted)."""
    r = auth_client.get("/manifest.webmanifest")
    data = json.loads(r.content)
    assert data["theme_color"] == "#C45B3E"
    assert data["background_color"] == "#FAF6F0"


def test_manifest_icon_files_exist_on_disk():
    icons_dir = FRONTEND_DIR / "static" / "icons"
    assert (icons_dir / "tiro-192.png").is_file()
    assert (icons_dir / "tiro-512.png").is_file()


def test_base_html_has_manifest_link_and_icon_tags():
    base_html = (FRONTEND_DIR / "templates" / "base.html").read_text()
    assert '<link rel="manifest" href="/manifest.webmanifest">' in base_html
    assert 'name="theme-color"' in base_html
    assert 'rel="apple-touch-icon"' in base_html
    assert "/static/icons/tiro-192.png" in base_html

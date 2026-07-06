"""M3.1 Task 1 + Task 2: PWA manifest/icons, service worker, offline page.

Covers the unauthenticated GET /manifest.webmanifest route in tiro/app.py,
the generated icon assets in tiro/frontend/static/icons/, and the manifest-
related tags in base.html (Task 1); GET /sw.js and GET /offline plus the
offline.html template (Task 2). The route-walk allowlist entries for these
paths live in tests/test_auth.py (test_route_walk_everything_gated)
alongside their own explanatory comments, not here.
"""

import json

from tiro.app import FRONTEND_DIR, STATIC_VERSION


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


def _png_dimensions(path):
    # PNG header: bytes 16-24 of the IHDR chunk are width/height (big-endian).
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def test_manifest_icons_have_declared_dimensions():
    """A regenerated icon with wrong dimensions must fail CI, not ship."""
    icons_dir = FRONTEND_DIR / "static" / "icons"
    assert _png_dimensions(icons_dir / "tiro-192.png") == (192, 192)
    assert _png_dimensions(icons_dir / "tiro-512.png") == (512, 512)


def test_login_page_carries_pwa_tags():
    """login.html is standalone (doesn't extend base.html) but is the
    first-run surface on a phone — installability must start there."""
    login_html = (FRONTEND_DIR / "templates" / "login.html").read_text()
    assert '<link rel="manifest" href="/manifest.webmanifest">' in login_html
    assert '<meta name="theme-color" content="#C45B3E">' in login_html
    assert 'apple-touch-icon' in login_html


def test_base_html_has_manifest_link_and_icon_tags():
    base_html = (FRONTEND_DIR / "templates" / "base.html").read_text()
    assert '<link rel="manifest" href="/manifest.webmanifest">' in base_html
    assert 'name="theme-color"' in base_html
    assert 'rel="apple-touch-icon"' in base_html
    assert "/static/icons/tiro-192.png" in base_html


# ---------------------------------------------------------------------------
# M3.1 Task 2: service worker + offline fallback
# ---------------------------------------------------------------------------


def test_sw_js_fetchable_unauthenticated_with_correct_content_type(auth_client):
    r = auth_client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")


def test_sw_js_embeds_current_static_version_and_no_placeholder_leaks(auth_client):
    """The served response must have the literal __STATIC_VERSION__
    placeholder substituted for the real, current STATIC_VERSION constant --
    never hardcode the version number here, since T5 will bump it and this
    test must not need touching."""
    r = auth_client.get("/sw.js")
    body = r.text
    assert "__STATIC_VERSION__" not in body
    # Cache names are built at runtime from a `VERSION` const via template
    # literals (`tiro-${VERSION}-static`), not literal "tiro-63-static"
    # text in the source -- assert the substituted VERSION constant itself.
    assert f'const VERSION = "{STATIC_VERSION}"' in body
    assert "tiro-${VERSION}-static" in body
    assert "tiro-${VERSION}-articles" in body
    assert f"/static/js/core.js?v={STATIC_VERSION}" in body


def test_sw_js_sets_service_worker_allowed_and_no_cache_headers(auth_client):
    r = auth_client.get("/sw.js")
    assert r.headers.get("service-worker-allowed") == "/"
    assert r.headers.get("cache-control") == "no-cache"


def test_sw_js_source_file_is_valid_at_rest_with_placeholder(auth_client):
    """The on-disk file (also harmlessly reachable via the open /static
    mount, same as manifest.webmanifest in Task 1) still carries the literal
    placeholder -- only the /sw.js route substitutes it."""
    source = (FRONTEND_DIR / "static" / "sw.js").read_text()
    assert "__STATIC_VERSION__" in source
    r = auth_client.get("/static/sw.js")
    assert r.status_code == 200
    assert "__STATIC_VERSION__" in r.text


def test_offline_page_fetchable_unauthenticated(auth_client):
    r = auth_client.get("/offline")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_offline_html_is_standalone_and_pulls_core_and_vendor():
    offline_html = (FRONTEND_DIR / "templates" / "offline.html").read_text()
    # Standalone: doesn't extend base.html (no {% extends %} block).
    assert "{% extends" not in offline_html
    assert "/static/js/core.js" in offline_html
    assert "/static/vendor/marked.min.js" in offline_html
    assert "/static/vendor/purify.min.js" in offline_html
    assert "renderMarkdown" in offline_html


def test_sw_routing_module_exists_and_is_imported_by_sw_js():
    routing_path = FRONTEND_DIR / "static" / "js" / "sw-routing.js"
    assert routing_path.is_file()
    assert "export function swRouteFor" in routing_path.read_text()
    sw_source = (FRONTEND_DIR / "static" / "sw.js").read_text()
    assert "/static/js/sw-routing.js" in sw_source
    assert "swRouteFor" in sw_source


def test_sw_register_module_exists_and_is_wired_into_sidebar_and_login():
    register_path = FRONTEND_DIR / "static" / "js" / "sw-register.js"
    assert register_path.is_file()
    assert "export function registerServiceWorker" in register_path.read_text()
    sidebar_js = (FRONTEND_DIR / "static" / "js" / "sidebar.js").read_text()
    assert "registerServiceWorker" in sidebar_js
    login_html = (FRONTEND_DIR / "templates" / "login.html").read_text()
    assert "registerServiceWorker" in login_html

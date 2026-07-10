"""Smoke tests: the app boots against an isolated library and responds."""


def test_client_boots_with_isolated_library(client, test_config):
    # Lifespan ran: the isolated stores exist and the real library was untouched
    assert test_config.db_path.exists()
    assert test_config.chroma_dir.exists()
    assert "tiro-library" not in str(test_config.library)


def test_root_redirects_to_inbox(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/inbox"


def test_html_pages_redirect_unauthenticated(client):
    # `client` is UNCONFIGURED (no password). Phase 5 D6 flips the unconfigured
    # page-auth redirect target to /welcome — an unconfigured visitor to /inbox
    # lands in the first-run wizard, not on a login page that would bounce them.
    # (The configured+anonymous case still redirects to /login — see the
    # auth_client-based page tests.)
    for path in ["/inbox", "/digest", "/stats", "/settings", "/graph", "/sources", "/wiki"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, f"{path} -> {r.status_code}"
        assert r.headers["location"] == "/welcome"


def test_wiki_item_page_redirects_unauthenticated(client):
    # {slug:path} route -- exercise it with an actual slash in the slug.
    # Unconfigured -> /welcome (see the note above).
    r = client.get("/wiki/entities/anthropic", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/welcome"


def test_articles_list_empty_library(authenticated_client):
    r = authenticated_client.get("/api/articles")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == []


def test_article_detail_404_for_missing(authenticated_client):
    r = authenticated_client.get("/api/articles/999")
    assert r.status_code == 404


def test_filters_endpoint_responds(authenticated_client):
    r = authenticated_client.get("/api/filters")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_api_401_without_auth(client):
    for path in ["/api/articles", "/api/filters", "/api/stats?period=week"]:
        assert client.get(path).status_code == 401, path


def test_docs_surface_closed(client):
    for path in ["/openapi.json", "/docs", "/redoc"]:
        assert client.get(path).status_code == 404, path


def test_cwd_is_isolated(tmp_path):
    from pathlib import Path

    assert Path.cwd() == tmp_path
    Path("config.yaml").write_text("library_path: ./scratch\n")  # must land in tmp
    assert (tmp_path / "config.yaml").exists()


def test_no_cdn_references_in_templates():
    from pathlib import Path

    # Templates live in the package, not CWD — resolve from the tiro package
    import tiro

    templates = Path(tiro.__file__).parent / "frontend" / "templates"
    offenders = []
    for tpl in templates.glob("*.html"):
        text = tpl.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            if ("<script" in line or "<link" in line) and "http" in line and "://" in line:
                offenders.append(f"{tpl.name}:{line_no}")
    assert not offenders, f"CDN references remain: {offenders}"


def test_no_cdn_references_in_static_js():
    from pathlib import Path

    import tiro

    static = Path(tiro.__file__).parent / "frontend" / "static"
    # Top-level *.js (the historical per-page files) plus static/js/*.js (the
    # M2.0 ES module entry points, e.g. sidebar.js/inbox.js/digest.js) — but
    # NOT static/js/tests/ (node:test files) or vendor/ (the vendored copies
    # themselves, which legitimately mention their own CDN origins in
    # comments/sourcemaps).
    js_files = list(static.glob("*.js")) + list((static / "js").glob("*.js"))
    offenders = []
    for js in js_files:
        text = js.read_text()
        for marker in ("cdn.jsdelivr", "unpkg.com", "cdnjs.", "googleapis.com"):
            if marker in text:
                offenders.append(f"{js.name}: {marker}")
    assert not offenders, f"CDN references in static JS: {offenders}"


def test_page_renders_configured_custom_theme(authenticated_client, configured_library):
    themes_dir = configured_library.library / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    (themes_dir / "my-custom.css").write_text(":root { --tiro-bg: #123456; }")
    configured_library.theme_light = "my-custom"

    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert "/library/themes/my-custom.css" in r.text
    assert 'data-dark-href="/static/themes/roman-night.css' in r.text


def test_inbox_has_logout_affordance(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'id="logout-btn"' in r.text


def test_graph_page_has_wiki_node_panel_affordances(authenticated_client):
    r = authenticated_client.get("/graph")
    assert r.status_code == 200
    assert 'id="generate-page-btn"' in r.text
    assert 'id="open-page-link-container"' in r.text


def test_mcp_config_env_override(monkeypatch, tmp_path):
    cfg = tmp_path / "elsewhere.yaml"
    cfg.write_text(f'library_path: "{tmp_path / "lib"}"\n')
    monkeypatch.setenv("TIRO_CONFIG", str(cfg))

    from tiro.mcp.server import _config_path

    assert str(_config_path()) == str(cfg)

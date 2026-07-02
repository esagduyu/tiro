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
    for path in ["/inbox", "/digest", "/stats", "/settings", "/graph"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, f"{path} -> {r.status_code}"
        assert r.headers["location"] == "/login"


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


def test_cwd_is_isolated(tmp_path):
    from pathlib import Path

    assert Path.cwd() == tmp_path
    Path("config.yaml").write_text("library_path: ./scratch\n")  # must land in tmp
    assert (tmp_path / "config.yaml").exists()

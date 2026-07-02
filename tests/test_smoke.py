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


def test_html_pages_render(client):
    for path in ["/inbox", "/digest", "/stats", "/settings", "/graph"]:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert "text/html" in r.headers["content-type"]


def test_articles_list_empty_library(client):
    r = client.get("/api/articles")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == []


def test_article_detail_404_for_missing(client):
    r = client.get("/api/articles/999")
    assert r.status_code == 404


def test_filters_endpoint_responds(client):
    r = client.get("/api/filters")
    assert r.status_code == 200
    assert r.json()["success"] is True

"""M4b: analysis GET is a pure cache read; running Opus is POST-only."""

CANNED_ANALYSIS = {
    "overall_summary": "Fine.",
    "bias": {"score": 7},
    "factual_confidence": {"score": 8},
    "novelty": {"score": 5},
    "analyzed_at": "2026-07-02T10:00:00",
}


def _boom(*a, **k):
    raise AssertionError("analyze_article must not be called by a GET")


def test_get_analysis_returns_cached_never_runs_opus(authenticated_client, monkeypatch):
    import tiro.api.routes_articles as ra

    monkeypatch.setattr(ra, "analyze_article", _boom)
    monkeypatch.setattr(ra, "get_cached_analysis", lambda config, aid: CANNED_ANALYSIS)
    r = authenticated_client.get("/api/articles/1/analysis")
    assert r.status_code == 200
    assert r.json()["data"]["bias"]["score"] == 7


def test_get_analysis_null_when_uncached(authenticated_client, monkeypatch):
    import tiro.api.routes_articles as ra

    monkeypatch.setattr(ra, "analyze_article", _boom)
    monkeypatch.setattr(ra, "get_cached_analysis", lambda config, aid: None)
    r = authenticated_client.get("/api/articles/1/analysis")
    assert r.status_code == 200
    assert r.json()["data"] is None
    # Legacy side-effect params must be inert
    r = authenticated_client.get("/api/articles/1/analysis?refresh=true")
    assert r.status_code == 200
    assert r.json()["data"] is None


def test_post_analysis_runs_opus(authenticated_client, monkeypatch):
    import tiro.api.routes_articles as ra

    calls = []
    monkeypatch.setattr(
        ra, "analyze_article", lambda config, aid: calls.append(aid) or CANNED_ANALYSIS
    )
    r = authenticated_client.post("/api/articles/42/analysis")
    assert r.status_code == 200
    assert r.json()["data"]["overall_summary"] == "Fine."
    assert calls == [42]


def test_post_analysis_error_mapping(authenticated_client, monkeypatch):
    import tiro.api.routes_articles as ra

    def raise_(exc):
        def f(*a, **k):
            raise exc
        return f

    monkeypatch.setattr(ra, "analyze_article", raise_(ValueError("Article 999 not found")))
    assert authenticated_client.post("/api/articles/999/analysis").status_code == 404

    monkeypatch.setattr(ra, "analyze_article", raise_(RuntimeError("no api key")))
    assert authenticated_client.post("/api/articles/1/analysis").status_code == 503


def test_post_analysis_generic_error_maps_500(authenticated_client, monkeypatch):
    import tiro.api.routes_articles as ra

    def boom(*a, **k):
        raise KeyError("boom")

    monkeypatch.setattr(ra, "analyze_article", boom)
    assert authenticated_client.post("/api/articles/1/analysis").status_code == 500

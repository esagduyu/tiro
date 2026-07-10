"""Imported-highlight anchoring (Phase 4 M4.2, spec D7.4 — the trust-critical
ON-7 Q7 law): imported highlights anchor through the SAME server-side
`tiro/anchors.py` machinery as user-created ones, sidecar-first, and an
unlocatable quote is SKIPPED + counted, never hand-placed.

Round-trip assertions go through `GET /api/articles/{id}/annotations` so the
live `anchor_status` (server-computed via `reconcile_anchor`) is exercised end
to end, exactly as the reader loads it.
"""

from tiro.annotations import read_annotations, sidecar_stem
from tiro.database import get_connection
from tiro.ingestion.importers.base import ImportHighlight, ImportItem, run_import


def _config(client):
    return client.app.state.config


def _article_by_title(config, title):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            "SELECT id, uid, markdown_path FROM articles WHERE title = ?", (title,)
        ).fetchone()
    finally:
        conn.close()


def _highlight_rows(config, article_id):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            "SELECT * FROM highlights WHERE article_id = ?", (article_id,)
        ).fetchall()
    finally:
        conn.close()


# --- anchoring core ----------------------------------------------------------


def test_present_quote_anchored_and_roundtrips(authenticated_client):
    config = _config(authenticated_client)
    quote = "The lighthouse stood against the storm."
    item = ImportItem(
        url="https://ex.test/anchor",
        title="Anchor Me",
        content_md=f"An opening line.\n\n{quote}\n\nA closing line.",
        highlights=[ImportHighlight(quote=quote, note="keeper")],
    )
    summary = run_import(config, [item], kind="readwise")
    assert summary["highlights_imported"] == 1
    assert summary["highlights_skipped"] == 0

    art = _article_by_title(config, "Anchor Me")

    # Sidecar line is first-class truth.
    lines = read_annotations(config, sidecar_stem(art))
    assert len(lines) == 1
    line = lines[0]
    assert line["quote"] == quote
    assert line["color"] == "yellow"
    assert line["note_markdown"] == "keeper"
    assert line["prefix"]  # real context derived from the body, not empty
    assert line["content_hash"]

    # Derived row matches the line.
    rows = _highlight_rows(config, art["id"])
    assert len(rows) == 1
    assert rows[0]["quote_text"] == quote
    assert rows[0]["uid"] == line["uid"]

    # Round-trips through the reader payload with a LIVE anchor_status.
    r = authenticated_client.get(f"/api/articles/{art['id']}/annotations")
    assert r.status_code == 200
    hls = r.json()["data"]["highlights"]
    assert len(hls) == 1
    assert hls[0]["anchor_status"]["status"] == "exact"
    assert hls[0]["note_markdown"] == "keeper"


def test_absent_quote_skipped_no_write(authenticated_client):
    config = _config(authenticated_client)
    item = ImportItem(
        url="https://ex.test/absent",
        title="No Anchor",
        content_md="This body does not contain the highlighted sentence at all.",
        highlights=[ImportHighlight(quote="a sentence that is simply not here")],
    )
    summary = run_import(config, [item], kind="readwise")
    assert summary["highlights_imported"] == 0
    assert summary["highlights_skipped"] == 1

    art = _article_by_title(config, "No Anchor")
    assert read_annotations(config, sidecar_stem(art)) == []
    assert _highlight_rows(config, art["id"]) == []


def test_quote_twice_yields_single_highlight(authenticated_client):
    config = _config(authenticated_client)
    quote = "echo echo"
    item = ImportItem(
        url="https://ex.test/twice",
        title="Twice",
        content_md=f"Before {quote} middle {quote} after.",
        highlights=[ImportHighlight(quote=quote)],
    )
    summary = run_import(config, [item], kind="readwise")
    assert summary["highlights_imported"] == 1

    art = _article_by_title(config, "Twice")
    lines = read_annotations(config, sidecar_stem(art))
    assert len(lines) == 1  # one highlight, at the first/best candidate


def test_rerun_adds_zero_duplicate_lines(authenticated_client):
    config = _config(authenticated_client)
    quote = "idempotent passage of prose."
    item = ImportItem(
        url="https://ex.test/idem",
        title="Idem",
        content_md=f"Lead in. {quote} Trailing text.",
        highlights=[ImportHighlight(quote=quote)],
    )
    first = run_import(config, [item], kind="readwise")
    assert first["highlights_imported"] == 1

    # Re-run the SAME import: article deduped, highlight deduped (no-op).
    second = run_import(config, [item], kind="readwise")
    assert second["skipped"] == 1  # existing article
    assert second["highlights_imported"] == 0
    assert second["highlights_skipped"] == 0  # dedupe is a silent no-op, not a failure

    art = _article_by_title(config, "Idem")
    assert len(read_annotations(config, sidecar_stem(art))) == 1  # still exactly one


def test_highlights_attach_to_existing_article(authenticated_client):
    """A refugee's highlights land on an article Tiro already has (spec D7.1):
    the dedup-skipped item still runs highlight import against the existing
    article's current body."""
    config = _config(authenticated_client)
    quote = "shared knowledge worth keeping."
    url = "https://ex.test/existing"

    # First: create the article WITHOUT highlights.
    run_import(
        config,
        [ImportItem(url=url, title="Existing", content_md=f"Intro. {quote} Outro.")],
        kind="readwise",
    )
    art = _article_by_title(config, "Existing")
    assert _highlight_rows(config, art["id"]) == []

    # Then: re-import the same URL carrying a highlight -> article skipped,
    # highlight anchored onto the existing article.
    summary = run_import(
        config,
        [ImportItem(url=url, title="Existing", highlights=[ImportHighlight(quote=quote)])],
        kind="readwise",
    )
    assert summary["skipped"] == 1
    assert summary["highlights_imported"] == 1

    rows = _highlight_rows(config, art["id"])
    assert len(rows) == 1 and rows[0]["quote_text"] == quote

    r = authenticated_client.get(f"/api/articles/{art['id']}/annotations")
    assert r.json()["data"]["highlights"][0]["anchor_status"]["status"] == "exact"


# --- acceptance: 50 highlights across articles, mixed anchorability ---------


def test_50_highlights_import_anchored_correctly(authenticated_client):
    """Roadmap acceptance: a 50-highlight export imports highlights anchored
    correctly to articles. 5 articles x 10 highlights; 8 present (exact/shifted
    -> anchored) + 2 unlocatable (skipped) per article = 40 imported, 10
    skipped, and each anchored highlight re-anchors live as `exact`."""
    config = _config(authenticated_client)
    items = []
    for a in range(5):
        # Eight unique present sentences form the body; two absent quotes won't
        # be found anywhere in it.
        present = [f"Article {a} sentence {s} carries meaning." for s in range(8)]
        body = "Prologue paragraph.\n\n" + "\n\n".join(present)
        highlights = [ImportHighlight(quote=q) for q in present]
        highlights += [
            ImportHighlight(quote=f"Article {a} phantom line {p} is missing.") for p in range(2)
        ]
        assert len(highlights) == 10
        items.append(
            ImportItem(
                url=f"https://ex.test/acc/{a}",
                title=f"Acc {a}",
                content_md=body,
                highlights=highlights,
            )
        )

    summary = run_import(config, items, kind="readwise")
    assert summary["imported"] == 5
    assert summary["highlights_imported"] == 40
    assert summary["highlights_skipped"] == 10

    # Spot-check one article end to end: 8 anchored lines, all live-exact.
    art = _article_by_title(config, "Acc 2")
    assert len(read_annotations(config, sidecar_stem(art))) == 8
    r = authenticated_client.get(f"/api/articles/{art['id']}/annotations")
    hls = r.json()["data"]["highlights"]
    assert len(hls) == 8
    assert all(h["anchor_status"]["status"] == "exact" for h in hls)

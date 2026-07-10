"""Importer core + Instapaper CSV + Omnivore zip adapters (Phase 4 M4.2).

All offline: the re-fetch function (`base.fetch_and_extract_sync`) is
monkeypatched so no network is ever touched. Highlight anchoring is a STUB in
Task 4 (counts only) — those tests assert the honest "counted, not anchored"
behavior; Task 5 replaces the stub.
"""

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from tiro.annotations import read_annotations, sidecar_stem
from tiro.audit import read_audit_entries
from tiro.database import get_connection
from tiro.ingestion.importers import base, instapaper, omnivore, readwise
from tiro.ingestion.importers.base import ImportHighlight, ImportItem, run_import
from tiro.migrations import new_ulid

FIXTURES = Path(__file__).parent / "fixtures" / "imports"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def omnivore_zip(tmp_path):
    """Zip the committed loose Omnivore export tree into a real .zip."""
    src = FIXTURES / "omnivore"
    zpath = tmp_path / "omnivore.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src).as_posix())
    return zpath


@pytest.fixture
def no_network(monkeypatch):
    """Record re-fetch calls and always fail — proves content came from the
    export, not the network."""
    calls = []

    def _boom(url):
        calls.append(url)
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(base, "fetch_and_extract_sync", _boom)
    return calls


def _tag_names(config, article_id):
    conn = get_connection(config.db_path)
    try:
        return {
            r["name"]
            for r in conn.execute(
                "SELECT t.name FROM tags t JOIN article_tags at ON at.tag_id = t.id"
                " WHERE at.article_id = ?",
                (article_id,),
            ).fetchall()
        }
    finally:
        conn.close()


def _seed_article(config, *, url="", title="Seed", source_name="seed.com"):
    conn = get_connection(config.db_path)
    try:
        src = conn.execute(
            "INSERT INTO sources (name, source_type) VALUES (?, 'web')", (source_name,)
        ).lastrowid
        aid = conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, url,"
            " ingestion_method) VALUES (?, ?, ?, ?, ?, ?, 'manual')",
            (new_ulid(), src, title, f"seed-{new_ulid()}", "seed.md", url),
        ).lastrowid
        conn.commit()
        return aid
    finally:
        conn.close()


# --- Instapaper adapter -----------------------------------------------------


def test_instapaper_parse_yields_items_and_highlight():
    items = list(instapaper.parse_export(FIXTURES / "instapaper.csv"))
    # 3 valid rows (the empty-URL row is skipped).
    assert len(items) == 3
    alpha = items[0]
    assert alpha.url == "https://example.com/alpha"
    assert alpha.title == "Alpha Article"
    assert alpha.tags == ["research"]
    assert alpha.saved_at == datetime.fromtimestamp(1683000000)
    assert len(alpha.highlights) == 1
    assert alpha.highlights[0].quote == "This is a highlighted selection"
    # beta has no Selection -> no highlight.
    assert items[1].highlights == []
    assert items[1].tags == ["reading"]


def test_instapaper_malformed_timestamp_is_lenient():
    items = list(instapaper.parse_export(FIXTURES / "instapaper.csv"))
    gamma = items[2]
    assert gamma.title == "Gamma Article"
    assert gamma.saved_at is None  # "notanumber" -> None, row still imported


def test_instapaper_header_tolerant(tmp_path):
    p = tmp_path / "weird.csv"
    p.write_text(" url , Title ,Selection, Folder ,Timestamp\nhttps://x.com/a,A,,,1683000000\n")
    items = list(instapaper.parse_export(p))
    assert len(items) == 1
    assert items[0].url == "https://x.com/a"


# --- Omnivore adapter -------------------------------------------------------


def test_omnivore_parse_maps_content_and_dates(omnivore_zip):
    items = {it.title: it for it in omnivore.parse_export(omnivore_zip)}
    assert set(items) == {"Markdown Article", "HTML Article"}

    md = items["Markdown Article"]
    assert md.content_md is not None and "markdown" in md.content_md
    assert md.content_html is None
    assert md.published_at.date() == datetime(2023, 4, 15).date()  # publishedAt
    assert md.saved_at.date() == datetime(2023, 5, 1).date()
    assert set(md.tags) == {"tech", "reading"}
    assert len(md.highlights) == 1

    html = items["HTML Article"]
    assert html.content_html is not None and "<b>HTML</b>" in html.content_html
    assert html.content_md is None
    assert html.published_at is None  # no publishedAt in export
    assert html.saved_at.date() == datetime(2023, 6, 1).date()
    assert html.tags == ["science"]


def test_omnivore_zip_slip_member_ignored(tmp_path):
    """A hostile member path (../ traversal) must never be processed nor
    written outside the archive."""
    zpath = tmp_path / "evil.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "metadata_0.json",
            json.dumps([{"slug": "ok", "title": "OK", "url": "https://ok.test/a"}]),
        )
        # Traversal-named metadata chunk with a poisoned item.
        zf.writestr(
            "metadata_../../evil.json",
            json.dumps([{"slug": "evil", "title": "EVIL", "url": "https://evil.test/x"}]),
        )
    zpath.write_bytes(buf.getvalue())

    titles = [it.title for it in omnivore.parse_export(zpath)]
    assert "OK" in titles
    assert "EVIL" not in titles
    # Nothing escaped to the parent dir.
    assert not (tmp_path.parent / "evil.json").exists()


def test_omnivore_parse_highlights_md_conservative():
    """Blockquote-shaped parsing: contiguous `>` lines are one quote, the next
    non-blank paragraph its note; headings/rules ignored, trailing link marker
    stripped, non-blockquote text contributes nothing."""
    text = (
        "# Title\n#### Highlights\n\n"
        "> first quote line\n> continued here\n\n"
        "a note paragraph\n\n"
        "> second quote [⤴️](https://omnivore.app/h/abc)\n\n"
        "---\n"
        "loose text that is not a blockquote is ignored\n"
    )
    hls = omnivore._parse_highlights_md(text)
    assert len(hls) == 2
    assert hls[0].quote == "first quote line\ncontinued here"
    assert hls[0].note == "a note paragraph"
    assert hls[1].quote == "second quote"  # trailing [link](url) marker stripped
    assert hls[1].note is None


def test_omnivore_parse_reads_highlights_file(omnivore_zip):
    """The `highlights/{slug}.md` member is matched to its article by slug and
    parsed into the item's highlights (in addition to any inline ones)."""
    items = {it.title: it for it in omnivore.parse_export(omnivore_zip)}
    html = items["HTML Article"]  # has no inline highlights; only the file
    quotes = {h.quote for h in html.highlights}
    assert quotes == {
        "content that gets sanitized and markdownified",
        "a phantom quote that never appears in the body",
    }
    anchorable = next(
        h for h in html.highlights
        if h.quote == "content that gets sanitized and markdownified"
    )
    assert anchorable.note == "a note attached to this highlight"


def test_omnivore_highlights_file_anchors_and_skips(initialized_library, no_network, omnivore_zip):
    """Through the full import pipeline: the anchorable quote from the
    highlights file is imported, the phantom one is skipped + counted (the
    skip-with-count law — never silently dropped)."""
    config = initialized_library
    html_item = next(
        it for it in omnivore.parse_export(omnivore_zip) if it.title == "HTML Article"
    )
    summary = run_import(config, [html_item], kind="omnivore")
    assert summary["highlights_imported"] == 1  # the anchorable quote
    assert summary["highlights_skipped"] == 1  # the phantom quote


# --- Readwise adapter -------------------------------------------------------


def test_readwise_parse_items_and_highlights():
    items = {it.title: it for it in readwise.parse_export(FIXTURES / "readwise.json")}
    # The no-source_url item is skipped -> only 2 items.
    assert set(items) == {"The Alpha Essay", "Beta Book"}

    alpha = items["The Alpha Essay"]
    assert alpha.url == "https://example.com/alpha"
    assert alpha.author == "Ada Alpha"
    assert alpha.published_at.date() == datetime(2023, 1, 2).date()
    assert alpha.saved_at.date() == datetime(2023, 2, 3).date()  # last_highlight_at fallback
    assert len(alpha.highlights) == 2
    assert alpha.highlights[0].quote == "the first anchored quote"
    assert alpha.highlights[0].note == "a reader note"
    assert alpha.highlights[1].note is None

    beta = items["Beta Book"]  # a "book" imports the same as an "article"
    assert beta.url == "https://example.com/beta"
    assert len(beta.highlights) == 1


def test_readwise_item_without_source_url_skipped_with_count(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        items = list(readwise.parse_export(FIXTURES / "readwise.json"))
    assert len(items) == 2  # the URL-less "No URL Orphan" is dropped
    assert any("no source_url" in r.message for r in caplog.records)


def test_readwise_accepts_wrapped_list(tmp_path):
    """Lenient top-level shape: a dict wrapping the list under `results`."""
    p = tmp_path / "wrapped.json"
    p.write_text(
        json.dumps(
            {"results": [{"title": "W", "source_url": "https://x.test/w", "highlights": []}]}
        )
    )
    items = list(readwise.parse_export(p))
    assert len(items) == 1 and items[0].url == "https://x.test/w"


def test_readwise_bad_json_yields_nothing(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert list(readwise.parse_export(p)) == []


# --- zip-slip guard (unit) --------------------------------------------------


def test_is_safe_member_rejects_escaping_paths():
    """Direct unit assertion on the zip-slip predicate: any member that
    normpaths OUTSIDE the archive root is rejected; ordinary members pass."""
    # Escapes root (normpath -> starts with `..`): rejected.
    assert omnivore._is_safe_member("../secret") is False
    assert omnivore._is_safe_member("content/../../escape.md") is False
    assert omnivore._is_safe_member("a/../../b") is False
    assert omnivore._is_safe_member("/abs/path") is False
    assert omnivore._is_safe_member("\\windows\\evil") is False
    # An internal `..` that still normpaths OUTSIDE root is caught.
    import posixpath

    assert posixpath.normpath("content/../../escape.md").startswith("..")
    # Legitimate members pass.
    assert omnivore._is_safe_member("metadata_0.json") is True
    assert omnivore._is_safe_member("content/article.md") is True
    assert omnivore._is_safe_member("a/b/c.html") is True


# --- run_import core --------------------------------------------------------


def test_run_import_ingestion_method_and_stub_tag(initialized_library, no_network):
    config = initialized_library
    # url-only item, re-fetch fails -> stub.
    item = ImportItem(url="https://paywalled.example/story", title="Paywalled Story")
    summary = run_import(config, [item], kind="instapaper")

    assert summary["imported"] == 1
    assert summary["stub_articles"] == 1
    assert summary["failed"] == 0
    assert no_network == ["https://paywalled.example/story"]  # a re-fetch WAS attempted

    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id, ingestion_method, markdown_path FROM articles"
        ).fetchone()
    finally:
        conn.close()
    assert row["ingestion_method"] == "import"
    body = (config.articles_dir / row["markdown_path"]).read_text()
    assert "https://paywalled.example/story" in body  # stub body carries the URL
    assert base.STUB_TAG in _tag_names(config, row["id"])


def test_run_import_uses_export_content_no_refetch(initialized_library, no_network):
    config = initialized_library
    item = ImportItem(
        url="https://example.org/md",
        title="Has Content",
        content_md="# Has Content\n\nThis body came from the export file directly.",
    )
    summary = run_import(config, [item], kind="omnivore")
    assert summary["imported"] == 1
    assert summary["stub_articles"] == 0
    assert no_network == []  # never re-fetched

    conn = get_connection(config.db_path)
    try:
        row = conn.execute("SELECT id, markdown_path FROM articles").fetchone()
    finally:
        conn.close()
    body = (config.articles_dir / row["markdown_path"]).read_text()
    assert "came from the export file" in body
    assert base.STUB_TAG not in _tag_names(config, row["id"])


def test_run_import_html_content_sanitized(initialized_library, no_network):
    config = initialized_library
    item = ImportItem(
        url="https://example.org/html",
        title="HTML Body",
        content_html="<p>Safe <b>text</b>.</p><script>alert('xss')</script>",
    )
    run_import(config, [item], kind="omnivore")
    conn = get_connection(config.db_path)
    try:
        row = conn.execute("SELECT markdown_path FROM articles").fetchone()
    finally:
        conn.close()
    body = (config.articles_dir / row["markdown_path"]).read_text()
    assert "Safe" in body
    assert "alert(" not in body  # script stripped by sanitize_html


def test_run_import_url_dedup_skips_existing(initialized_library, no_network):
    config = initialized_library
    _seed_article(config, url="https://example.com/already", title="Already Saved")
    item = ImportItem(
        url="https://example.com/already?utm_source=x",
        title="Already Saved",
        content_md="body",
    )
    summary = run_import(config, [item], kind="instapaper")
    assert summary["skipped"] == 1
    assert summary["imported"] == 0
    # Still only the one seeded row.
    conn = get_connection(config.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_run_import_title_source_dedup_skips(initialized_library, no_network):
    config = initialized_library
    # Seed an article whose source name matches the import URL's domain.
    _seed_article(
        config, url="https://other/path", title="Shared Title", source_name="example.com"
    )
    item = ImportItem(
        url="https://example.com/different-path", title="Shared Title", content_md="body"
    )
    summary = run_import(config, [item], kind="instapaper")
    assert summary["skipped"] == 1
    assert summary["imported"] == 0


def test_run_import_published_at_fallback_order(initialized_library, no_network):
    config = initialized_library
    saved = datetime(2022, 1, 2, 3, 4, 5)
    published = datetime(2021, 6, 7, 8, 9, 10)

    only_saved = ImportItem(
        url="https://ex.test/a", title="Only Saved", content_md="b", saved_at=saved
    )
    both = ImportItem(
        url="https://ex.test/b",
        title="Both",
        content_md="b",
        saved_at=saved,
        published_at=published,
    )
    run_import(config, [only_saved, both], kind="instapaper")

    conn = get_connection(config.db_path)
    try:
        rows = {
            r["title"]: r["published_at"]
            for r in conn.execute("SELECT title, published_at FROM articles").fetchall()
        }
    finally:
        conn.close()
    assert rows["Only Saved"].startswith("2022-01-02")  # fell back to saved_at
    assert rows["Both"].startswith("2021-06-07")  # publishedAt wins


def test_run_import_audit_line_per_run(initialized_library, no_network):
    config = initialized_library
    run_import(
        config,
        [ImportItem(url="https://ex.test/a", title="A", content_md="b")],
        kind="omnivore",
    )
    entries = [e for e in read_audit_entries(config) if e["service"] == "import"]
    assert len(entries) == 1
    assert entries[0]["endpoint"] == "omnivore"
    assert entries[0]["count"] == 1


def test_run_import_per_row_isolation(initialized_library, no_network, monkeypatch):
    """One item that blows up mid-ingest must not abort the whole run."""
    config = initialized_library
    real = base.process_article

    def flaky(**kwargs):
        if kwargs.get("title") == "BOOM":
            raise RuntimeError("simulated ingest failure")
        return real(**kwargs)

    monkeypatch.setattr(base, "process_article", flaky)

    items = [
        ImportItem(url="https://ex.test/1", title="One", content_md="b"),
        ImportItem(url="https://ex.test/boom", title="BOOM", content_md="b"),
        ImportItem(url="https://ex.test/3", title="Three", content_md="b"),
    ]
    summary = run_import(config, items, kind="instapaper")
    assert summary["imported"] == 2
    assert summary["failed"] == 1
    assert summary["total"] == 3
    # Audit: a run with per-item failures is not a clean success (mirrors
    # check_feeds' `success = failed == 0` semantics).
    entries = [e for e in read_audit_entries(config) if e["service"] == "import"]
    assert len(entries) == 1
    assert entries[0]["success"] is False


def test_import_tag_case_unifies_with_existing_lowercase(initialized_library, no_network):
    """A folder/label tag like "Tech" folds into the lowercase "tech" row the
    RSS folder-tag path writes — one tag row, not two (attach_tags lowercases)."""
    config = initialized_library
    conn = get_connection(config.db_path)
    try:
        # Simulate the RSS folder-tag path having already created lowercase "tech".
        conn.execute("INSERT INTO tags (uid, name) VALUES (?, 'tech')", (new_ulid(),))
        conn.commit()
    finally:
        conn.close()

    item = ImportItem(url="https://ex.test/t", title="T", content_md="b", tags=["Tech"])
    run_import(config, [item], kind="instapaper")

    conn = get_connection(config.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM tags WHERE name IN ('tech', 'Tech')"
        ).fetchone()["n"]
        row = conn.execute("SELECT id FROM articles").fetchone()
    finally:
        conn.close()
    assert n == 1  # "Tech" folded into the existing lowercase row, not a second
    assert "tech" in _tag_names(config, row["id"])


def test_run_import_highlights_anchored_sidecar_written(initialized_library, no_network):
    """Task 5: quotes present in the body anchor (sidecar-first) and are
    counted as imported (replaces the Task 4 counted-not-anchored stub)."""
    config = initialized_library
    item = ImportItem(
        url="https://ex.test/hl",
        title="Has Highlights",
        content_md="Some body text to anchor against.",
        highlights=[ImportHighlight(quote="Some body"), ImportHighlight(quote="text")],
    )
    summary = run_import(config, [item], kind="instapaper")
    assert summary["highlights_imported"] == 2
    assert summary["highlights_skipped"] == 0

    conn = get_connection(config.db_path)
    try:
        row = conn.execute("SELECT markdown_path FROM articles").fetchone()
    finally:
        conn.close()
    lines = read_annotations(config, sidecar_stem(row))
    assert len(lines) == 2
    assert {ln["quote"] for ln in lines} == {"Some body", "text"}


def test_run_import_progress_callback(initialized_library, no_network):
    config = initialized_library
    seen = []
    items = [
        ImportItem(url=f"https://ex.test/{i}", title=f"T{i}", content_md="b")
        for i in range(3)
    ]
    run_import(config, items, kind="instapaper", progress_cb=lambda s: seen.append(s["processed"]))
    assert seen == [1, 2, 3]


def test_cli_import_instapaper_verb(initialized_library, no_network, capsys):
    from types import SimpleNamespace

    from tiro import cli

    config = initialized_library
    args = SimpleNamespace(
        file=str(FIXTURES / "instapaper.csv"), config="config.yaml", _config_override=config
    )
    rc = cli.cmd_import_instapaper(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Import complete (instapaper)" in out
    conn = get_connection(config.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    finally:
        conn.close()
    assert n == 3  # 3 valid CSV rows (all stubs, no network)


def test_cli_import_omnivore_verb(initialized_library, no_network, omnivore_zip, capsys):
    from types import SimpleNamespace

    from tiro import cli

    config = initialized_library
    args = SimpleNamespace(
        file=str(omnivore_zip), config="config.yaml", _config_override=config
    )
    rc = cli.cmd_import_omnivore(args)
    assert rc == 0
    assert "Import complete (omnivore)" in capsys.readouterr().out
    assert no_network == []  # both Omnivore items carry content


def test_cli_import_readwise_verb(initialized_library, no_network, capsys):
    from types import SimpleNamespace

    from tiro import cli

    config = initialized_library
    args = SimpleNamespace(
        file=str(FIXTURES / "readwise.json"), config="config.yaml", _config_override=config
    )
    rc = cli.cmd_import_readwise(args)
    assert rc == 0
    assert "Import complete (readwise)" in capsys.readouterr().out
    # 2 items with URLs -> 2 articles (both stubs: Readwise carries no body).
    conn = get_connection(config.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    finally:
        conn.close()
    assert n == 2


def test_100_article_import_preserves_timestamps(initialized_library, no_network, tmp_path):
    """Acceptance criterion: a 100-article import keeps each item's original
    saved date, not the import day."""
    config = initialized_library
    lines = ["URL,Title,Selection,Folder,Timestamp"]
    base_ts = 1_600_000_000
    for i in range(100):
        lines.append(f"https://ex.test/a{i},Article {i},,Batch,{base_ts + i * 86400}")
    csv_path = tmp_path / "big.csv"
    csv_path.write_text("\n".join(lines) + "\n")

    summary = run_import(config, instapaper.parse_export(csv_path), kind="instapaper")
    assert summary["imported"] == 100
    assert summary["failed"] == 0

    conn = get_connection(config.db_path)
    try:
        rows = conn.execute("SELECT title, published_at FROM articles").fetchall()
    finally:
        conn.close()
    assert len(rows) == 100
    by_title = {r["title"]: r["published_at"] for r in rows}
    expected0 = datetime.fromtimestamp(base_ts).isoformat()
    assert by_title["Article 0"] == expected0

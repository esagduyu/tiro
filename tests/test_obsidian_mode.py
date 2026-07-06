"""Obsidian-compatible write mode (M2.3 Task 3): frontmatter format toggle.

`obsidian_compatible_mode` (tiro/config.py, default False) changes ONLY how
NEW article frontmatter is written at ingest (tiro/ingestion/processor.py).
Flag OFF must stay byte-identical to pre-M2.3 output -- the golden test
below pins that as the keystone regression. Flag ON adds Obsidian-standard
`aliases`/`created` fields immediately (first frontmatter write), and a
`related` wikilink list via a targeted THIRD frontmatter rewrite once
relations are computed -- they're only known after the ChromaDB add, later
in the same ingest call (see processor.py's `_related_wikilinks` and the
comments around it). No background rewriter is introduced; this is the
same ingest call, best-effort like the ChromaDB/relations steps around it.
"""

from pathlib import Path

import frontmatter as fm

FIXTURE = Path(__file__).parent / "fixtures" / "newsletter.eml"


def _extracted():
    from tiro.ingestion.email import parse_eml

    return parse_eml(FIXTURE.read_bytes())


# --- Golden regression: flag OFF must stay byte-identical to today -------


def test_golden_frontmatter_flag_off_matches_today(initialized_library):
    """Keystone test: pins the exact frontmatter keys/format the writer
    produces today with the flag at its default (False). Any future change
    to processor.py's writer that isn't gated behind
    `obsidian_compatible_mode` must fail this test.
    """
    from tiro.ingestion.processor import process_article

    assert initialized_library.obsidian_compatible_mode is False
    ex = _extracted()
    result = process_article(**ex, config=initialized_library, ingestion_method="email")
    md_path = initialized_library.articles_dir / result["markdown_path"]
    post = fm.load(md_path)

    assert set(post.metadata.keys()) == {
        "title", "author", "source", "url", "published", "ingested",
        "tags", "entities", "word_count", "reading_time",
    }
    # Confirms the brief's investigation: tags/entities are ALREADY plain
    # YAML lists today, regardless of this flag -- nothing to change there.
    assert post.metadata["tags"] == [] and isinstance(post.metadata["tags"], list)
    assert post.metadata["entities"] == [] and isinstance(post.metadata["entities"], list)
    assert "aliases" not in post.metadata
    assert "created" not in post.metadata
    assert "related" not in post.metadata

    # Empty lists render inline ("tags: []"), not block style -- part of the
    # pinned format, since it changes character-for-character.
    raw = md_path.read_text()
    assert "tags: []" in raw
    assert "entities: []" in raw


# --- Flag ON: aliases / created / tags-stay-a-list -----------------------


def test_flag_on_adds_aliases_and_created_tags_stay_a_list(initialized_library, fake_llm):
    from tiro.ingestion.processor import process_article

    initialized_library.obsidian_compatible_mode = True
    fake_llm('{"tags": ["local-first", "sync"], "entities": [], "summary": "A test summary."}')

    ex = _extracted()
    result = process_article(**ex, config=initialized_library, ingestion_method="email")
    md_path = initialized_library.articles_dir / result["markdown_path"]
    post = fm.load(md_path)

    assert post.metadata["aliases"] == []
    assert post.metadata["created"] == ex["published_at"].isoformat(timespec="seconds")
    assert post.metadata["tags"] == ["local-first", "sync"]
    assert isinstance(post.metadata["tags"], list)
    # Only article in the library -- no relations possible yet, but the
    # field is still present (empty list), not omitted.
    assert post.metadata["related"] == []

    raw = md_path.read_text()
    assert "aliases: []" in raw
    # Non-empty lists render block-style ("- item"), same as tags always has.
    assert "- local-first" in raw
    assert "- sync" in raw


def test_flag_off_process_article_never_writes_obsidian_keys(initialized_library, fake_llm):
    """Belt-and-suspenders on top of the golden test: even with AI-populated
    tags/entities/summary (a fuller write than the golden test's empty-AI
    path), flag OFF must never introduce aliases/created/related."""
    from tiro.ingestion.processor import process_article

    fake_llm('{"tags": ["local-first"], "entities": [{"name": "Tiro", "type": "product"}], "summary": "Sum."}')
    ex = _extracted()
    result = process_article(**ex, config=initialized_library, ingestion_method="email")
    md_path = initialized_library.articles_dir / result["markdown_path"]
    post = fm.load(md_path)

    assert "aliases" not in post.metadata
    assert "created" not in post.metadata
    assert "related" not in post.metadata


# --- Flag ON: related wikilinks reference real markdown_path stems -------


def test_flag_on_related_wikilinks_reference_real_stems(initialized_library, fake_llm):
    from tiro.ingestion.processor import process_article

    initialized_library.obsidian_compatible_mode = True

    fake_llm('{"tags": [], "entities": [], "summary": "Article A summary."}')
    article_a = process_article(
        title="Local-First Software Basics",
        author=None,
        content_md="Local-first software keeps your data on your own device first.",
        url="https://example.com/obsidian-a",
        config=initialized_library,
        ingestion_method="manual",
    )

    # B's ingestion makes two llm_call()s: extract_metadata, then
    # generate_connection_notes (since B will find A as a relation --
    # find_related_articles has no similarity threshold, just count>1).
    fake_llm(
        '{"tags": [], "entities": [], "summary": "Article B summary."}',
        f'{{"notes": [{{"article_id": {article_a["id"]}, "note": "Both discuss local-first design."}}]}}',
    )
    article_b = process_article(
        title="More on Local-First Sync",
        author=None,
        content_md="Synchronization strategies for local-first apps, continuing the discussion.",
        url="https://example.com/obsidian-b",
        config=initialized_library,
        ingestion_method="manual",
    )

    md_path_b = initialized_library.articles_dir / article_b["markdown_path"]
    post_b = fm.load(md_path_b)
    expected_stem_a = Path(article_a["markdown_path"]).stem
    assert post_b.metadata["related"] == [f"[[{expected_stem_a}]]"]

    # A was ingested first, alone in the library -- relations are computed
    # (and stored/written) once, at ingest time, with no background
    # rewriter, so A's own frontmatter never learns about B after the fact.
    # This mirrors the existing article_relations asymmetry (retroactive
    # fix-up is POST /api/recompute-relations, out of scope here).
    md_path_a = initialized_library.articles_dir / article_a["markdown_path"]
    post_a = fm.load(md_path_a)
    assert post_a.metadata["related"] == []


# --- Export -> import round-trip: unknown frontmatter keys survive -------


def test_import_round_trip_preserves_obsidian_frontmatter_fields(initialized_library, tmp_path, fake_llm):
    """python-frontmatter preserves unknown keys, and tiro/importer.py copies
    each article's markdown bytes verbatim from the bundle rather than
    reparsing/rewriting frontmatter -- so obsidian-mode fields should
    round-trip byte-for-byte through export -> import, and matching (by uid)
    should be unaffected by their presence."""
    from tiro.config import TiroConfig
    from tiro.database import get_connection, init_db
    from tiro.export import export_library
    from tiro.importer import import_bundle
    from tiro.ingestion.processor import process_article

    initialized_library.obsidian_compatible_mode = True
    fake_llm('{"tags": ["local-first"], "entities": [], "summary": "A summary."}')
    article = process_article(
        title="Obsidian Round Trip",
        author="Test Author",
        content_md="Body text for the round-trip test.",
        url="https://example.com/obsidian-roundtrip",
        config=initialized_library,
        ingestion_method="manual",
    )
    source_md = initialized_library.articles_dir / article["markdown_path"]
    source_raw = source_md.read_text()
    # Sanity: the obsidian fields really are in the source file we're about
    # to round-trip.
    assert "aliases: []" in source_raw
    assert "created:" in source_raw

    conn = get_connection(initialized_library.db_path)
    try:
        source_uid = conn.execute(
            "SELECT uid FROM articles WHERE id = ?", (article["id"],)
        ).fetchone()["uid"]
    finally:
        conn.close()

    bundle = export_library(initialized_library)
    try:
        target_config = TiroConfig(library_path=str(tmp_path / "obsidian-target-library"))
        target_config.articles_dir.mkdir(parents=True)
        init_db(target_config.db_path)

        result = import_bundle(target_config, bundle)
        assert result["imported"] == 1
        assert result["skipped"] == 0

        conn2 = get_connection(target_config.db_path)
        try:
            row = conn2.execute("SELECT uid, markdown_path FROM articles").fetchone()
        finally:
            conn2.close()

        # uid-based matching (import_bundle's first match strategy) works
        # unchanged -- the new frontmatter fields don't interfere.
        assert row["uid"] == source_uid

        imported_raw = (target_config.articles_dir / row["markdown_path"]).read_text()
        assert imported_raw == source_raw
    finally:
        bundle.unlink(missing_ok=True)

"""import_bundle: export round-trip + conflict modes."""

import pytest

from tiro.authors import link_article_author
from tiro.database import get_connection
from tiro.export import export_library
from tiro.importer import import_bundle


def _seed(config, *, title="T1", slug="art-1", uid="01AAAAAAAAAAAAAAAAAAAAAAAA",
          url="https://x.com/a", rating=None, author=None):
    conn = get_connection(config.db_path)
    conn.execute("INSERT OR IGNORE INTO sources (name, source_type) VALUES ('Src', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, author, slug, markdown_path, url, rating)"
        " VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (uid, title, author, slug, f"{slug}.md", url, rating),
    )
    conn.execute("INSERT OR IGNORE INTO tags (uid, name) VALUES ('01TAG000000000000000000000', 'ai')")
    conn.execute("INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (1, 1)")
    conn.commit()
    conn.close()
    (config.articles_dir / f"{slug}.md").write_text(f"---\ntitle: {title}\n---\nbody of {slug}")


def _fresh_library(tmp_path):
    """Second, empty library to import into. Import only writes SQLite +
    markdown (no ChromaDB/embedding calls), so unlike `initialized_library`
    this doesn't need `_shared_embeddings`/init_vectorstore."""
    from tiro.config import TiroConfig
    from tiro.database import init_db

    lib = tmp_path / "lib2"
    config = TiroConfig(library_path=str(lib))
    config.articles_dir.mkdir(parents=True)
    init_db(config.db_path)
    return config


def test_round_trip_into_empty_library(initialized_library, tmp_path):
    config = initialized_library
    _seed(config, rating=2)
    bundle = export_library(config)
    try:
        target = _fresh_library(tmp_path)
        result = import_bundle(target, bundle)
        assert result["imported"] == 1 and result["skipped"] == 0
        conn = get_connection(target.db_path)
        row = conn.execute(
            "SELECT a.title, a.uid, a.rating, a.vector_status, s.name AS src"
            " FROM articles a JOIN sources s ON a.source_id = s.id"
        ).fetchone()
        tag = conn.execute(
            "SELECT t.name FROM tags t JOIN article_tags at ON t.id = at.tag_id"
        ).fetchone()
        conn.close()
        assert row["title"] == "T1" and row["uid"] == "01AAAAAAAAAAAAAAAAAAAAAAAA"
        assert row["rating"] == 2 and row["vector_status"] == "pending"
        assert row["src"] == "Src" and tag["name"] == "ai"
        assert (target.articles_dir / "art-1.md").read_text().endswith("body of art-1")
    finally:
        bundle.unlink()


def test_conflict_skip_and_overwrite(initialized_library, tmp_path):
    config = initialized_library
    _seed(config, rating=1)
    bundle = export_library(config)
    try:
        # same library = guaranteed uid match
        r_skip = import_bundle(config, bundle, conflicts="skip")
        assert r_skip["skipped"] == 1 and r_skip["imported"] == 0

        conn = get_connection(config.db_path)
        conn.execute("UPDATE articles SET rating = -1, title = 'CHANGED'")
        # Local-only tag, not present in the bundle, linked to the article.
        conn.execute(
            "INSERT OR IGNORE INTO tags (uid, name) VALUES ('01TAGLOCAL0000000000000000', 'local-only')"
        )
        conn.execute(
            "INSERT INTO article_tags (article_id, tag_id)"
            " SELECT 1, id FROM tags WHERE name = 'local-only'"
        )
        conn.commit()
        conn.close()

        r_over = import_bundle(config, bundle, conflicts="overwrite")
        assert r_over["overwritten"] == 1
        conn = get_connection(config.db_path)
        row = conn.execute("SELECT title, rating, vector_status FROM articles").fetchone()
        tag_names = {
            r["name"] for r in conn.execute(
                "SELECT t.name FROM tags t JOIN article_tags at ON t.id = at.tag_id"
                " WHERE at.article_id = 1"
            ).fetchall()
        }
        conn.close()
        assert row["title"] == "T1" and row["rating"] == 1
        assert row["vector_status"] == "pending"
        # Overwrite means the bundle's state wins: the local-only tag link
        # is gone (bundle only had 'ai'); the tag row itself may remain.
        assert tag_names == {"ai"}
    finally:
        bundle.unlink()


def test_conflict_keep_both(initialized_library, tmp_path):
    config = initialized_library
    _seed(config)
    bundle = export_library(config)
    try:
        r = import_bundle(config, bundle, conflicts="keep-both")
        assert r["kept_both"] == 1
        conn = get_connection(config.db_path)
        rows = conn.execute("SELECT slug, uid FROM articles ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[1]["slug"] == "art-1-imported"
        assert rows[1]["uid"] != rows[0]["uid"]
        assert (initialized_library.articles_dir / "art-1-imported.md").exists()
    finally:
        bundle.unlink()


def test_invalid_conflicts_mode(initialized_library, tmp_path):
    with pytest.raises(ValueError, match="conflicts"):
        import_bundle(initialized_library, tmp_path / "x.zip", conflicts="merge")


def test_cli_import(initialized_library, tmp_path, capsys):
    from tiro.cli import cmd_import_bundle

    cfg = initialized_library
    _seed(cfg)
    bundle_path = export_library(cfg)

    class Args:
        # NOTE: `config`/`bundle` here are class attributes (unused —
        # `_config_override` wins over `config`), not references to the
        # outer `cfg`/`bundle_path`. Reusing those names for both the outer
        # local and the class attribute would make Python treat them as
        # local to the class body for the whole block (same rule as
        # function scoping), breaking a same-named reference above the
        # assignment with a NameError.
        _config_override = cfg
        config = "unused"
        bundle = str(bundle_path)
        conflicts = "keep-both"

    try:
        rc = cmd_import_bundle(Args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "kept-both: 1" in out
    finally:
        bundle_path.unlink()


def test_import_marks_existing_wiki_page_stale(initialized_library, tmp_path):
    """Import is an ingest path like web/email ingestion -- an imported
    article that links to a tag/entity with an existing wiki page must
    stale-mark that page, mirroring processor.py's mark_pages_stale hook."""
    from tiro.wiki import write_page

    config = initialized_library
    _seed(config, rating=1)  # links article 1 to tag 'ai'
    bundle = export_library(config)
    try:
        target = _fresh_library(tmp_path)
        write_page(
            target,
            slug="concepts/ai",
            kind="concept",
            title="Ai",
            entity_type=None,
            article_uids=[],
            body="AI body.",
            generated_by=None,
        )

        result = import_bundle(target, bundle)
        assert result["imported"] == 1

        conn = get_connection(target.db_path)
        try:
            row = conn.execute(
                "SELECT status FROM wiki_pages WHERE slug = 'concepts/ai'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["status"] == "stale"
    finally:
        bundle.unlink()


def test_import_leaves_doctor_clean(initialized_library, tmp_path):
    """Roadmap acceptance: doctor reports zero structural inconsistencies
    after a keep-both import. Imported articles land with
    vector_status='pending' and no ChromaDB vector — scan() only flags
    vector_missing for status='indexed' and vector_unmarked for a vector
    that IS present, so a freshly-imported pending/no-vector row matches
    neither and is correctly treated as expected-pending, not structural."""
    from tiro.doctor import scan

    config = initialized_library
    _seed(config)
    bundle = export_library(config)
    try:
        result = import_bundle(config, bundle, conflicts="keep-both")
        assert result["kept_both"] == 1

        report = scan(config)
        assert report["structurally_consistent"] is True, report
        assert report["orphaned_markdown"] == []
        assert report["missing_markdown"] == []
        assert report["vector_missing"] == []
        assert report["vector_unmarked"] == []
        assert report["vector_failed"] == []
    finally:
        bundle.unlink()


def test_overwrite_preserves_fields_absent_from_bundle(initialized_library, tmp_path):
    """M1.1 review item 8: a bundle produced by a schema that predates one
    of _OVERWRITE_FIELDS (here simulated by hand-stripping 'is_read' from
    the article dict) must not null that field out on overwrite-import —
    only fields actually present in the bundle should be applied."""
    import json
    import zipfile

    config = initialized_library
    _seed(config, rating=1)
    conn = get_connection(config.db_path)
    conn.execute("UPDATE articles SET is_read = 1")
    conn.commit()
    conn.close()

    bundle = export_library(config)
    stripped = tmp_path / "stripped.zip"
    try:
        # Rewrite the bundle with 'is_read' removed from every article dict,
        # simulating a pre-schema bundle that never had the field.
        with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(stripped, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "metadata.json":
                    meta = json.loads(data)
                    for art in meta["articles"]:
                        art.pop("is_read", None)
                    data = json.dumps(meta).encode()
                zout.writestr(item, data)

        result = import_bundle(config, stripped, conflicts="overwrite")
        assert result["overwritten"] == 1

        conn = get_connection(config.db_path)
        row = conn.execute("SELECT is_read, title FROM articles").fetchone()
        conn.close()
        # is_read survives untouched; other bundle-present fields still apply.
        assert row["is_read"] == 1
        assert row["title"] == "T1"
    finally:
        bundle.unlink()
        stripped.unlink()


def _authors_for(conn, article_id):
    return {
        r["name"] for r in conn.execute(
            "SELECT au.name FROM authors au"
            " JOIN article_authors aa ON aa.author_id = au.id"
            " WHERE aa.article_id = ?",
            (article_id,),
        ).fetchall()
    }


def test_round_trip_links_author(initialized_library, tmp_path):
    """Final-review item 1: importing a fresh article carrying an author
    must create the authors row + article_authors junction, not just write
    the free-text articles.author column — otherwise imported articles are
    invisible to author VIP (decay/digest) and /api/authors counts."""
    config = initialized_library
    _seed(config, author="Jane Reporter")
    bundle = export_library(config)
    try:
        target = _fresh_library(tmp_path)
        result = import_bundle(target, bundle)
        assert result["imported"] == 1

        conn = get_connection(target.db_path)
        row = conn.execute("SELECT id, author FROM articles").fetchone()
        assert row["author"] == "Jane Reporter"
        assert _authors_for(conn, row["id"]) == {"Jane Reporter"}
        conn.close()
    finally:
        bundle.unlink()


def test_overwrite_changing_author_updates_junction(initialized_library, tmp_path):
    """Overwrite-import applies the bundle's author to the free-text column
    (existing behavior via _OVERWRITE_FIELDS) AND must swap the
    article_authors junction to match: drop the old author's link, link the
    new one."""
    config = initialized_library
    _seed(config, author="Old Author")
    conn = get_connection(config.db_path)
    link_article_author(conn, 1, "Old Author")
    conn.commit()
    conn.close()
    bundle = export_library(config)
    try:
        # Simulate the bundle carrying a different author than what's
        # currently in the target library (edit the exported bundle).
        import json
        import zipfile

        changed = tmp_path / "changed.zip"
        with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(changed, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "metadata.json":
                    meta = json.loads(data)
                    for art in meta["articles"]:
                        art["author"] = "New Author"
                    data = json.dumps(meta).encode()
                zout.writestr(item, data)

        result = import_bundle(config, changed, conflicts="overwrite")
        assert result["overwritten"] == 1

        conn = get_connection(config.db_path)
        row = conn.execute("SELECT id, author FROM articles").fetchone()
        assert row["author"] == "New Author"
        assert _authors_for(conn, row["id"]) == {"New Author"}
        conn.close()
    finally:
        bundle.unlink()
        changed.unlink()


def test_overwrite_without_author_key_leaves_junction_untouched(initialized_library, tmp_path):
    """M1.1 absent-field guard extended to authors: when the bundle's article
    dict lacks the 'author' key entirely (older schema), the existing
    article_authors junction must be left alone — not cleared, not
    re-derived from the (missing) bundle value."""
    config = initialized_library
    _seed(config, author="Kept Author")
    # _seed writes the articles row directly (bypassing process_article),
    # so link the junction by hand to set up the "existing junction" this
    # test is about preserving.
    conn = get_connection(config.db_path)
    link_article_author(conn, 1, "Kept Author")
    conn.commit()
    conn.close()
    bundle = export_library(config)
    try:
        import json
        import zipfile

        stripped = tmp_path / "stripped-author.zip"
        with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(stripped, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "metadata.json":
                    meta = json.loads(data)
                    for art in meta["articles"]:
                        art.pop("author", None)
                    data = json.dumps(meta).encode()
                zout.writestr(item, data)

        result = import_bundle(config, stripped, conflicts="overwrite")
        assert result["overwritten"] == 1

        conn = get_connection(config.db_path)
        row = conn.execute("SELECT id, author FROM articles").fetchone()
        # author column itself untouched (already covered by the M1.1 test);
        # the point here is the junction survives too.
        assert row["author"] == "Kept Author"
        assert _authors_for(conn, row["id"]) == {"Kept Author"}
        conn.close()
    finally:
        bundle.unlink()
        stripped.unlink()


# --- Highlights + notes sidecar merge (Phase 2 M2.1 Task 4) ------------------


def _add_highlight(config, article_id, article_uid, *, h_uid=None, quote="hello", note=None):
    """Add one highlight (SQLite row + sidecar line) to an already-seeded
    article. Mirrors routes_annotations.py's sidecar-first convention."""
    from tiro.annotations import read_annotations, sidecar_stem, write_annotations
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    h_uid = h_uid or new_ulid()
    conn = get_connection(config.db_path)
    row = conn.execute(
        "SELECT markdown_path FROM articles WHERE id = ?", (article_id,)
    ).fetchone()
    stem = sidecar_stem(row)
    conn.execute(
        """INSERT INTO highlights
           (uid, article_id, quote_text, prefix_context, suffix_context,
            text_position_start, text_position_end, content_hash, color,
            created_at, updated_at)
           VALUES (?, ?, ?, 'pre', 'suf', 0, 5, 'hash', 'yellow',
                   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (h_uid, article_id, quote),
    )
    if note:
        conn.execute(
            "INSERT INTO notes (uid, article_id, highlight_id, body_markdown, created_at, updated_at)"
            " SELECT ?, ?, id, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z' FROM highlights WHERE uid = ?",
            (new_ulid(), article_id, note, h_uid),
        )
    conn.commit()
    conn.close()

    lines = read_annotations(config, stem)
    lines.append({
        "uid": h_uid, "article_uid": article_uid, "quote": quote,
        "prefix": "pre", "suffix": "suf", "position_start": 0, "position_end": 5,
        "content_hash": "hash", "color": "yellow", "note_markdown": note,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    write_annotations(config, stem, lines)
    return h_uid


def _add_article_note(config, article_id, *, body="article note"):
    from tiro.annotations import sidecar_stem, write_note
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    conn = get_connection(config.db_path)
    row = conn.execute(
        "SELECT markdown_path FROM articles WHERE id = ?", (article_id,)
    ).fetchone()
    stem = sidecar_stem(row)
    conn.execute(
        "INSERT INTO notes (uid, article_id, highlight_id, body_markdown, created_at, updated_at)"
        " VALUES (?, ?, NULL, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (new_ulid(), article_id, body),
    )
    conn.commit()
    conn.close()
    write_note(config, stem, body)


def test_round_trip_imports_highlight_and_note_into_empty_library(initialized_library, tmp_path):
    from tiro.annotations import read_annotations, read_note

    config = initialized_library
    _seed(config)
    h_uid = _add_highlight(config, 1, "01AAAAAAAAAAAAAAAAAAAAAAAA", note="hl note")
    _add_article_note(config, 1, body="article note")

    bundle = export_library(config)
    try:
        target = _fresh_library(tmp_path)
        result = import_bundle(target, bundle)
        assert result["imported"] == 1

        conn = get_connection(target.db_path)
        try:
            hrow = conn.execute("SELECT * FROM highlights WHERE uid = ?", (h_uid,)).fetchone()
            assert hrow is not None and hrow["quote_text"] == "hello"
            note_row = conn.execute(
                "SELECT body_markdown FROM notes WHERE highlight_id = ?", (hrow["id"],)
            ).fetchone()
            assert note_row["body_markdown"] == "hl note"
        finally:
            conn.close()

        lines = read_annotations(target, "art-1")
        assert len(lines) == 1 and lines[0]["uid"] == h_uid
        assert read_note(target, "art-1") == "article note"
    finally:
        bundle.unlink()


def test_import_skip_merges_new_uid_lines_keeps_existing_untouched(initialized_library, tmp_path):
    """conflicts='skip': article row untouched, but sidecars still merge --
    existing local lines survive as-is, new bundle uids get appended."""
    from tiro.annotations import read_annotations

    config = initialized_library
    _seed(config, rating=1)
    local_h_uid = _add_highlight(config, 1, "01AAAAAAAAAAAAAAAAAAAAAAAA", quote="local one")
    bundle = export_library(config)  # bundle now carries local_h_uid too
    try:
        # A second highlight added to the bundle's copy AFTER the export was
        # taken — simulate by adding one more highlight to the local library
        # and re-exporting.
        new_h_uid = _add_highlight(
            config, 1, "01AAAAAAAAAAAAAAAAAAAAAAAA", quote="new one"
        )
        bundle2 = export_library(config)

        # Roll the local library "back" to only have the first highlight —
        # simulate a peer whose copy is missing the second one.
        conn = get_connection(config.db_path)
        conn.execute("DELETE FROM highlights WHERE uid = ?", (new_h_uid,))
        conn.commit()
        conn.close()
        from tiro.annotations import write_annotations

        write_annotations(
            config, "art-1",
            [ln for ln in read_annotations(config, "art-1") if ln["uid"] != new_h_uid],
        )

        result = import_bundle(config, bundle2, conflicts="skip")
        assert result["skipped"] == 1

        lines = read_annotations(config, "art-1")
        uids = {ln["uid"] for ln in lines}
        assert local_h_uid in uids and new_h_uid in uids
        assert len(lines) == 2

        # import_bundle's trailing reconcile_annotations() rebuilds the row
        # for the merged-in line automatically.
        conn = get_connection(config.db_path)
        try:
            assert conn.execute(
                "SELECT 1 FROM highlights WHERE uid = ?", (new_h_uid,)
            ).fetchone() is not None
        finally:
            conn.close()
    finally:
        bundle.unlink()
        bundle2.unlink()


def test_import_overwrite_replaces_sidecars_wholesale(initialized_library, tmp_path):
    """conflicts='overwrite': the bundle's sidecars win outright -- a local
    highlight absent from the bundle must be gone, not merged."""
    from tiro.annotations import read_annotations, read_note

    config = initialized_library
    _seed(config, rating=1)
    _add_highlight(config, 1, "01AAAAAAAAAAAAAAAAAAAAAAAA", quote="from bundle")
    _add_article_note(config, 1, body="bundle note")
    bundle = export_library(config)
    try:
        # Local library now diverges: an extra local-only highlight/note not
        # in the bundle, plus a changed article-level note.
        _add_highlight(config, 1, "01AAAAAAAAAAAAAAAAAAAAAAAA", quote="local-only")
        from tiro.annotations import write_note

        write_note(config, "art-1", "locally changed note")

        result = import_bundle(config, bundle, conflicts="overwrite")
        assert result["overwritten"] == 1

        lines = read_annotations(config, "art-1")
        assert len(lines) == 1
        assert lines[0]["quote"] == "from bundle"
        assert read_note(config, "art-1") == "bundle note"
    finally:
        bundle.unlink()


def test_import_keep_both_mints_fresh_uids_under_new_stem(initialized_library, tmp_path):
    """conflicts='keep-both': the new copy's sidecars must NOT reuse the
    original article's highlight uids (which remain live under the
    original stem) -- fresh uids, new stem."""
    from tiro.annotations import read_annotations

    config = initialized_library
    _seed(config)
    orig_h_uid = _add_highlight(config, 1, "01AAAAAAAAAAAAAAAAAAAAAAAA", quote="dup me")
    bundle = export_library(config)
    try:
        result = import_bundle(config, bundle, conflicts="keep-both")
        assert result["kept_both"] == 1

        conn = get_connection(config.db_path)
        try:
            new_article = conn.execute(
                "SELECT id FROM articles WHERE slug = 'art-1-imported'"
            ).fetchone()
        finally:
            conn.close()
        assert new_article is not None

        new_lines = read_annotations(config, "art-1-imported")
        assert len(new_lines) == 1
        assert new_lines[0]["quote"] == "dup me"
        assert new_lines[0]["uid"] != orig_h_uid  # fresh uid, no collision

        # Original stem's own sidecar is untouched.
        orig_lines = read_annotations(config, "art-1")
        assert len(orig_lines) == 1 and orig_lines[0]["uid"] == orig_h_uid
    finally:
        bundle.unlink()


def test_import_article_without_sidecars_is_noop(initialized_library, tmp_path):
    """An article with no highlights/notes at all must not gain empty
    sidecar files after import."""
    from tiro.annotations import annotations_dir, notes_dir

    config = initialized_library
    _seed(config)
    bundle = export_library(config)
    try:
        target = _fresh_library(tmp_path)
        result = import_bundle(target, bundle)
        assert result["imported"] == 1
        assert not (annotations_dir(target) / "art-1.jsonl").exists()
        assert not (notes_dir(target) / "art-1.md").exists()
    finally:
        bundle.unlink()


def test_import_reconciles_sidecars_into_rows_after_run(initialized_library, tmp_path):
    """The final reconcile_annotations() call must actually rebuild rows
    from whatever sidecar merging left on disk -- checked end-to-end via the
    skip-mode merge (which appends a bundle line to the local file without
    inserting a row itself)."""
    from tiro.annotations import reconcile_annotations, write_annotations

    config = initialized_library
    _seed(config, rating=1)
    _add_highlight(config, 1, "01AAAAAAAAAAAAAAAAAAAAAAAA", quote="one")
    bundle = export_library(config)
    try:
        extra_uid = _add_highlight(
            config, 1, "01AAAAAAAAAAAAAAAAAAAAAAAA", quote="two"
        )
        bundle2 = export_library(config)
        conn = get_connection(config.db_path)
        conn.execute("DELETE FROM highlights WHERE uid = ?", (extra_uid,))
        conn.commit()
        conn.close()
        from tiro.annotations import read_annotations

        write_annotations(
            config, "art-1",
            [ln for ln in read_annotations(config, "art-1") if ln["uid"] != extra_uid],
        )

        result = import_bundle(config, bundle2, conflicts="skip")
        assert result["skipped"] == 1

        conn = get_connection(config.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM highlights WHERE uid = ?", (extra_uid,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None  # reconcile_annotations rebuilt it from the file

        # Idempotent: reconciling again changes nothing further.
        counts = reconcile_annotations(config)
        assert counts["highlights_inserted"] == 0
    finally:
        bundle.unlink()
        bundle2.unlink()


# --- Phase 4: feed subscription merge (spec D5) ------------------------------


def _seed_feed(config, *, url, title="RSS Blog", folder=None):
    """Insert a feed + its backing rss source into `config`'s library."""
    from tiro.migrations import new_ulid

    conn = get_connection(config.db_path)
    conn.execute(
        "INSERT INTO sources (name, domain, source_type) VALUES (?, 'blog.example.com', 'rss')",
        (title,),
    )
    source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        "INSERT INTO feeds (uid, url, title, site_url, folder, source_id, "
        "fetch_interval_minutes, status) "
        "VALUES (?, ?, ?, 'https://blog.example.com', ?, ?, 45, 'active')",
        (new_ulid(), url, title, folder, source_id),
    )
    conn.commit()
    conn.close()


def test_import_merges_feeds_by_url_with_fresh_source(initialized_library, tmp_path):
    config = initialized_library
    _seed(config)
    _seed_feed(config, url="https://blog.example.com/feed.xml", title="RSS Blog", folder="Tech")
    bundle = export_library(config)
    try:
        target = _fresh_library(tmp_path)
        result = import_bundle(target, bundle)
        assert result["feeds_imported"] == 1
        conn = get_connection(target.db_path)
        try:
            feed = conn.execute(
                "SELECT f.url, f.title, f.folder, f.fetch_interval_minutes, "
                "f.error_count, f.last_etag, s.source_type, s.name AS src_name "
                "FROM feeds f JOIN sources s ON f.source_id = s.id"
            ).fetchone()
        finally:
            conn.close()
        assert feed["url"] == "https://blog.example.com/feed.xml"
        assert feed["title"] == "RSS Blog"
        assert feed["folder"] == "Tech"
        assert feed["fetch_interval_minutes"] == 45
        # Fresh source resolved locally, backed by an rss source.
        assert feed["source_type"] == "rss"
        assert feed["src_name"] == "RSS Blog"
        # Transient state starts clean (not in the bundle).
        assert feed["error_count"] == 0
        assert feed["last_etag"] is None
    finally:
        bundle.unlink()


def test_import_skips_feed_with_existing_url(initialized_library, tmp_path):
    config = initialized_library
    _seed(config)
    _seed_feed(config, url="https://blog.example.com/feed.xml")
    bundle = export_library(config)
    try:
        target = _fresh_library(tmp_path)
        # Pre-existing feed in the target with the SAME url.
        _seed_feed(target, url="https://blog.example.com/feed.xml", title="Already Here")
        result = import_bundle(target, bundle)
        assert result["feeds_imported"] == 0
        conn = get_connection(target.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"]
            title = conn.execute("SELECT title FROM feeds").fetchone()["title"]
        finally:
            conn.close()
        assert n == 1  # no duplicate row
        assert title == "Already Here"  # existing subscription untouched
    finally:
        bundle.unlink()


def test_import_pre_phase4_bundle_without_feeds_key_is_noop(initialized_library, tmp_path):
    import json
    import zipfile

    config = initialized_library
    _seed(config)
    bundle = export_library(config)
    try:
        # Simulate an older bundle: strip the feeds key from metadata.json.
        stripped = tmp_path / "old.zip"
        with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(stripped, "w") as zout:
            for item in zin.namelist():
                data = zin.read(item)
                if item == "metadata.json":
                    meta = json.loads(data)
                    meta.pop("feeds", None)
                    data = json.dumps(meta).encode("utf-8")
                zout.writestr(item, data)
        target = _fresh_library(tmp_path)
        result = import_bundle(target, stripped)
        assert result["feeds_imported"] == 0
        assert result["imported"] == 1
    finally:
        bundle.unlink()

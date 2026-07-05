"""Wiki page store: files-as-truth (tiro/wiki.py)."""

import re

import pytest

from tiro.database import get_connection
from tiro.migrations import canonical_key, new_ulid
from tiro.wiki import (
    WIKI_KINDS,
    append_log,
    ensure_schema_file,
    mark_pages_stale,
    page_path,
    read_page,
    reconcile_wiki_index,
    regenerate_index,
    wiki_slugify,
    write_page,
)

# --- seeding helpers ---------------------------------------------------------


def _seed_article(config, slug, uid=None, title="T"):
    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
        article_uid = uid or new_ulid()
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
            " VALUES (?, last_insert_rowid(), ?, ?, ?)",
            (article_uid, title, slug, f"{slug}.md"),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return article_id, article_uid
    finally:
        conn.close()


def _link_entity(config, article_id, name, entity_type="company"):
    conn = get_connection(config.db_path)
    try:
        key = canonical_key(name)
        existing = conn.execute(
            "SELECT id FROM entities WHERE entity_type = ? AND canonical_key = ?",
            (entity_type, key),
        ).fetchone()
        if existing:
            entity_id = existing["id"]
        else:
            conn.execute(
                "INSERT INTO entities (uid, name, entity_type, canonical_key) VALUES (?, ?, ?, ?)",
                (new_ulid(), name, entity_type, key),
            )
            entity_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO article_entities (article_id, entity_id) VALUES (?, ?)",
            (article_id, entity_id),
        )
        conn.commit()
        return entity_id
    finally:
        conn.close()


def _link_tag(config, article_id, name):
    conn = get_connection(config.db_path)
    try:
        existing = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if existing:
            tag_id = existing["id"]
        else:
            conn.execute("INSERT INTO tags (uid, name) VALUES (?, ?)", (new_ulid(), name))
            tag_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
            (article_id, tag_id),
        )
        conn.commit()
        return tag_id
    finally:
        conn.close()


# --- wiki_slugify -------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Context Engineering", "context-engineering"),
        ("Anthropic", "anthropic"),
        ("  leading/trailing spaces  ", "leading-trailing-spaces"),
        ("C++ & Rust!!", "c-rust"),
        ("Already-Slugged", "already-slugged"),
        ("Multiple   Spaces", "multiple-spaces"),
    ],
)
def test_wiki_slugify_cases(name, expected):
    assert wiki_slugify(name) == expected


# --- page_path -----------------------------------------------------------------


def test_page_path_builds_expected_file(initialized_library):
    path = page_path(initialized_library, "entities/anthropic")
    assert path == initialized_library.wiki_dir / "entities" / "anthropic.md"


@pytest.mark.parametrize("bad_slug", ["../etc/passwd", "/etc/passwd", "a/../../b", ""])
def test_page_path_rejects_traversal(initialized_library, bad_slug):
    with pytest.raises(ValueError):
        page_path(initialized_library, bad_slug)


# --- write_page / read_page round-trip -----------------------------------------


def test_write_page_then_read_page_round_trip(initialized_library):
    _, article_uid = _seed_article(initialized_library, "a1")
    result = write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[article_uid],
        body="Anthropic is an AI safety company. [[a1|source]]",
        generated_by={"model": "claude-haiku-4-5", "tier": "light"},
        user_pinned_note="Don't mention the lawsuit.",
    )
    assert result["slug"] == "entities/anthropic"
    assert result["source_count"] == 1

    page = read_page(initialized_library, "entities/anthropic")
    assert page is not None
    assert page["title"] == "Anthropic"
    assert page["kind"] == "entity"
    assert page["entity_type"] == "company"
    assert page["status"] == "fresh"
    assert page["article_uids"] == [article_uid]
    assert page["source_count"] == 1
    assert page["generated_by"] == {"model": "claude-haiku-4-5", "tier": "light"}
    assert page["user_pinned_note"] == "Don't mention the lawsuit."
    assert page["body"] == "Anthropic is an AI safety company. [[a1|source]]"
    assert page["uid"] == result["uid"]
    assert page["updated_at"] == result["updated_at"]

    # Derived index row + junction populated.
    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM wiki_pages WHERE slug = 'entities/anthropic'"
        ).fetchone()
        assert row is not None
        assert row["uid"] == result["uid"]
        assert row["source_count"] == 1
        linked = conn.execute(
            "SELECT article_id FROM wiki_page_articles WHERE page_id = ?", (row["id"],)
        ).fetchall()
        assert len(linked) == 1
    finally:
        conn.close()


def test_read_page_missing_returns_none(initialized_library):
    assert read_page(initialized_library, "entities/nope") is None


def test_read_page_tolerates_hand_edited_file_missing_fields(initialized_library):
    path = page_path(initialized_library, "concepts/bare")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: Bare Page\n---\nJust a body, hand-written.\n")

    page = read_page(initialized_library, "concepts/bare")
    assert page["title"] == "Bare Page"
    assert page["kind"] == ""
    assert page["status"] == "fresh"
    assert page["article_uids"] == []
    assert page["source_count"] == 0
    assert page["user_pinned_note"] == ""
    assert page["body"] == "Just a body, hand-written."


def test_write_page_rejects_invalid_kind(initialized_library):
    with pytest.raises(ValueError):
        write_page(
            initialized_library,
            slug="syntheses/foo",
            kind="synthesis",
            title="Foo",
            entity_type=None,
            article_uids=[],
            body="body",
            generated_by=None,
        )


def test_write_page_uid_stability_across_rewrite(initialized_library):
    first = write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="v1",
        generated_by=None,
    )
    uid = first["uid"]
    assert len(uid) == 26  # ULID

    second = write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="v2",
        generated_by=None,
        uid=uid,
    )
    assert second["uid"] == uid
    assert read_page(initialized_library, "entities/anthropic")["uid"] == uid

    # Only one derived row -- the slug was updated in place, not duplicated.
    conn = get_connection(initialized_library.db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM wiki_pages WHERE slug = 'entities/anthropic'"
        ).fetchone()["n"]
        assert count == 1
    finally:
        conn.close()


def test_write_page_skips_unknown_article_uid(initialized_library, caplog):
    with caplog.at_level("WARNING"):
        result = write_page(
            initialized_library,
            slug="entities/anthropic",
            kind="entity",
            title="Anthropic",
            entity_type="company",
            article_uids=["01UNKNOWNUIDDOESNOTEXIST01"],
            body="body",
            generated_by=None,
        )
    # Cited even though unresolved -- source_count reflects what's cited.
    assert result["source_count"] == 1
    assert "unknown article uid" in caplog.text

    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM wiki_pages WHERE slug = 'entities/anthropic'"
        ).fetchone()
        linked = conn.execute(
            "SELECT * FROM wiki_page_articles WHERE page_id = ?", (row["id"],)
        ).fetchall()
        assert linked == []
    finally:
        conn.close()


def test_write_page_returned_body_matches_read_page_after_trailing_whitespace(
    initialized_library,
):
    """frontmatter.dumps() strips leading/trailing whitespace from the body
    when writing the file, so a body with trailing newlines would otherwise
    make write_page's returned "body" disagree with what a later read_page
    sees. write_page must strip once, up front, and return the stripped
    form so both call sites agree."""
    result = write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="Body with trailing whitespace.\n\n\n",
        generated_by=None,
    )
    page = read_page(initialized_library, "entities/anthropic")
    assert result["body"] == page["body"]
    assert result["body"] == "Body with trailing whitespace."


def test_write_page_creates_schema_index_and_log(initialized_library):
    write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="body",
        generated_by=None,
    )
    assert (initialized_library.wiki_dir / "_schema.md").exists()
    assert (initialized_library.wiki_dir / "index.md").exists()
    assert (initialized_library.wiki_dir / "log.md").exists()


# --- ensure_schema_file ---------------------------------------------------------


def test_ensure_schema_file_creates_once_never_overwrites(initialized_library):
    from tiro.intelligence.prompts import load_template

    path = ensure_schema_file(initialized_library)
    assert path.exists()
    original = path.read_text()
    assert len(original.strip()) > 0
    # T3 lands the real packaged default (not the old 3-line placeholder) --
    # ensure_schema_file must yield it byte-for-byte.
    assert original == load_template("wiki_schema_default", ext="md")
    assert "Citation rules" in original
    assert "Compression rules" in original

    path.write_text("# My Own Rules\nDo not touch this.\n")
    path2 = ensure_schema_file(initialized_library)
    assert path2 == path
    assert path.read_text() == "# My Own Rules\nDo not touch this.\n"


# --- log.md ----------------------------------------------------------------------


def test_append_log_appends_greppable_entries(initialized_library):
    append_log(initialized_library, "create", "entities/anthropic")
    append_log(initialized_library, "update", "concepts/context-engineering")

    text = (initialized_library.wiki_dir / "log.md").read_text()
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2
    pattern = re.compile(r"^## \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] (create|update) \| \S+$")
    for line in lines:
        assert pattern.match(line), line
    assert "create | entities/anthropic" in text
    assert "update | concepts/context-engineering" in text


# --- index.md ----------------------------------------------------------------------


def test_regenerate_index_lists_pages_grouped_by_kind(initialized_library):
    write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="body",
        generated_by=None,
    )
    write_page(
        initialized_library,
        slug="concepts/context-engineering",
        kind="concept",
        title="Context Engineering",
        entity_type=None,
        article_uids=[],
        body="body",
        generated_by=None,
    )
    regenerate_index(initialized_library)
    text = (initialized_library.wiki_dir / "index.md").read_text()

    assert "## Entities" in text
    assert "## Concepts" in text
    assert "Anthropic" in text
    assert "Context Engineering" in text
    ent_idx = text.index("## Entities")
    con_idx = text.index("## Concepts")
    anthropic_idx = text.index("Anthropic")
    ctx_idx = text.index("Context Engineering")
    assert ent_idx < anthropic_idx < con_idx < ctx_idx


def test_regenerate_index_empty_kind_shows_placeholder(initialized_library):
    regenerate_index(initialized_library)
    text = (initialized_library.wiki_dir / "index.md").read_text()
    assert "_None yet._" in text


# --- reconcile_wiki_index -------------------------------------------------------


def test_reconcile_rebuilds_index_from_files_after_db_wipe(initialized_library):
    _, article_uid = _seed_article(initialized_library, "a1")
    result = write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[article_uid],
        body="body",
        generated_by=None,
    )

    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute("DELETE FROM wiki_page_articles")
        conn.execute("DELETE FROM wiki_pages")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM wiki_pages").fetchone()["n"] == 0
    finally:
        conn.close()

    counts = reconcile_wiki_index(initialized_library)
    assert counts["pages"] == 1
    assert counts["skipped"] == 0
    assert counts["unresolved_articles"] == 0

    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM wiki_pages WHERE slug = 'entities/anthropic'"
        ).fetchone()
        assert row is not None
        assert row["uid"] == result["uid"]
        assert row["source_count"] == 1
        linked = conn.execute(
            "SELECT article_id FROM wiki_page_articles WHERE page_id = ?", (row["id"],)
        ).fetchall()
        assert len(linked) == 1
    finally:
        conn.close()


def test_reconcile_drops_rows_for_deleted_files_but_never_touches_files(initialized_library):
    write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="keep me",
        generated_by=None,
    )
    write_page(
        initialized_library,
        slug="entities/openai",
        kind="entity",
        title="OpenAI",
        entity_type="company",
        article_uids=[],
        body="delete my file",
        generated_by=None,
    )

    kept_path = page_path(initialized_library, "entities/anthropic")
    deleted_path = page_path(initialized_library, "entities/openai")
    kept_bytes_before = kept_path.read_bytes()
    deleted_path.unlink()  # simulate manual/out-of-band file removal

    counts = reconcile_wiki_index(initialized_library)
    assert counts["pages"] == 1

    conn = get_connection(initialized_library.db_path)
    try:
        slugs = {r["slug"] for r in conn.execute("SELECT slug FROM wiki_pages").fetchall()}
    finally:
        conn.close()
    assert slugs == {"entities/anthropic"}

    # Reconcile never writes/deletes files itself -- the kept file's bytes
    # are untouched, and the already-deleted file was NOT recreated.
    assert kept_path.read_bytes() == kept_bytes_before
    assert not deleted_path.exists()


def test_reconcile_skips_unparseable_files_and_counts_them(initialized_library):
    write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="body",
        generated_by=None,
    )
    bad_path = initialized_library.wiki_dir / "entities" / "corrupt.md"
    bad_path.write_text("---\ntitle: [unterminated\n---\nbody\n")

    counts = reconcile_wiki_index(initialized_library)
    assert counts["pages"] == 1
    assert counts["skipped"] == 1
    # The corrupt file itself is left alone.
    assert bad_path.exists()


def test_reconcile_excludes_bookkeeping_files(initialized_library):
    write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="body",
        generated_by=None,
    )
    counts = reconcile_wiki_index(initialized_library)
    # _schema.md/index.md/log.md exist alongside the one real page but must
    # not be counted as pages.
    assert counts["pages"] == 1


# --- mark_pages_stale ------------------------------------------------------------


def test_mark_pages_stale_entity_updates_db_and_file_preserving_body(initialized_library):
    article_id, article_uid = _seed_article(initialized_library, "a1")
    _link_entity(initialized_library, article_id, "Anthropic", entity_type="company")

    write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="Original body text, must survive byte-exact.",
        generated_by=None,
        status="fresh",
    )
    path = page_path(initialized_library, "entities/anthropic")
    body_before = read_page(initialized_library, "entities/anthropic")["body"]

    # A second (new) article also mentions Anthropic -- this is the "gained
    # a new citing article" event the stale hook fires on.
    article_id2, _ = _seed_article(initialized_library, "a2")
    _link_entity(initialized_library, article_id2, "Anthropic", entity_type="company")

    conn = get_connection(initialized_library.db_path)
    try:
        count = mark_pages_stale(initialized_library, conn, article_id2)
        conn.commit()
        row = conn.execute(
            "SELECT status FROM wiki_pages WHERE slug = 'entities/anthropic'"
        ).fetchone()
    finally:
        conn.close()

    assert count == 1
    assert row["status"] == "stale"

    page_after = read_page(initialized_library, "entities/anthropic")
    assert page_after["status"] == "stale"
    assert page_after["body"] == body_before
    assert path.exists()


def test_mark_pages_stale_concept_via_tag_junction(initialized_library):
    article_id, _ = _seed_article(initialized_library, "a1")
    _link_tag(initialized_library, article_id, "context-engineering")

    write_page(
        initialized_library,
        slug="concepts/context-engineering",
        kind="concept",
        title="context-engineering",
        entity_type=None,
        article_uids=[],
        body="concept body",
        generated_by=None,
        status="fresh",
    )

    article_id2, _ = _seed_article(initialized_library, "a2")
    _link_tag(initialized_library, article_id2, "context-engineering")

    conn = get_connection(initialized_library.db_path)
    try:
        count = mark_pages_stale(initialized_library, conn, article_id2)
        conn.commit()
        row = conn.execute(
            "SELECT status FROM wiki_pages WHERE slug = 'concepts/context-engineering'"
        ).fetchone()
    finally:
        conn.close()

    assert count == 1
    assert row["status"] == "stale"


def test_mark_pages_stale_matches_by_slug_despite_prettified_title(initialized_library):
    """Regression test for the slug-based rewrite: a concept page's TITLE is
    a prettified display string ("Context Engineering") but its slug is the
    raw slugified tag ("concepts/context-engineering"). Title-based matching
    (canonical_key("Context Engineering") == "context engineering", a space)
    would never equal canonical_key("context-engineering") ==
    "context-engineering" (a hyphen) -- exactly the bug the slug switch
    fixes, since matching now goes through wiki_slugify() on both sides."""
    article_id, _ = _seed_article(initialized_library, "a1")
    _link_tag(initialized_library, article_id, "context-engineering")

    write_page(
        initialized_library,
        slug="concepts/context-engineering",
        kind="concept",
        title="Context Engineering",
        entity_type=None,
        article_uids=[],
        body="concept body",
        generated_by=None,
        status="fresh",
    )

    article_id2, _ = _seed_article(initialized_library, "a2")
    _link_tag(initialized_library, article_id2, "context-engineering")

    conn = get_connection(initialized_library.db_path)
    try:
        count = mark_pages_stale(initialized_library, conn, article_id2)
        conn.commit()
        row = conn.execute(
            "SELECT status FROM wiki_pages WHERE slug = 'concepts/context-engineering'"
        ).fetchone()
    finally:
        conn.close()

    assert count == 1
    assert row["status"] == "stale"


def test_mark_pages_stale_returns_zero_when_no_matching_page(initialized_library):
    article_id, _ = _seed_article(initialized_library, "a1")
    _link_entity(initialized_library, article_id, "Unrelated Corp")

    conn = get_connection(initialized_library.db_path)
    try:
        count = mark_pages_stale(initialized_library, conn, article_id)
    finally:
        conn.close()
    assert count == 0


def test_mark_pages_stale_is_idempotent_for_already_stale_page(initialized_library):
    article_id, _ = _seed_article(initialized_library, "a1")
    _link_entity(initialized_library, article_id, "Anthropic", entity_type="company")
    write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="body",
        generated_by=None,
        status="stale",
    )

    conn = get_connection(initialized_library.db_path)
    try:
        count = mark_pages_stale(initialized_library, conn, article_id)
        conn.commit()
    finally:
        conn.close()
    assert count == 1  # still reported as matched, even though unchanged


# --- process_article ingest hook (Task 5, seam 1) ---------------------------------


def test_ingest_marks_existing_wiki_page_stale_same_call(initialized_library, fake_llm):
    """A wiki page already exists for an entity ("Anthropic"). Ingesting a
    NEW article whose Haiku extraction cites that same entity must flip the
    page stale within the same process_article() call -- no second pass, no
    background job. Free SQL + frontmatter rewrite, no LLM involved in the
    stale-marking itself (fake_llm here only stands in for the extraction
    call that discovers the entity link)."""
    from tiro.database import get_connection
    from tiro.ingestion.processor import process_article

    write_page(
        initialized_library,
        slug="entities/anthropic",
        kind="entity",
        title="Anthropic",
        entity_type="company",
        article_uids=[],
        body="Original body, must survive byte-exact.",
        generated_by=None,
        status="fresh",
    )

    fake_llm(
        '{"tags": [], "entities": [{"name": "Anthropic", "type": "company"}], '
        '"summary": "About Anthropic."}'
    )
    result = process_article(
        title="Anthropic ships something new",
        author=None,
        content_md="Anthropic body text here.",
        url="https://example.com/anthropic-news",
        config=initialized_library,
    )
    assert result["id"]  # ingest succeeded, not rolled back

    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM wiki_pages WHERE slug = 'entities/anthropic'"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "stale"

    page = read_page(initialized_library, "entities/anthropic")
    assert page["status"] == "stale"
    assert page["body"] == "Original body, must survive byte-exact."


def test_ingest_mark_pages_stale_failure_is_nonfatal(initialized_library, fake_llm, monkeypatch):
    """The stale hook is best-effort bookkeeping -- a failure in it must not
    roll back an otherwise-successful ingest (unlike a real enrichment
    failure, which does roll back via delete_article())."""
    from tiro.database import get_connection
    from tiro.ingestion import processor

    def boom(config, conn, article_id):
        raise RuntimeError("stale marking exploded")

    monkeypatch.setattr(processor, "mark_pages_stale", boom)
    fake_llm('{"tags": [], "entities": [], "summary": ""}')
    result = processor.process_article(
        title="Some article",
        author=None,
        content_md="body text here",
        url="https://example.com/x",
        config=initialized_library,
    )
    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE id = ?", (result["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["n"] == 1  # article survives; not rolled back


# --- WIKI_KINDS sanity -------------------------------------------------------------


def test_wiki_kinds_are_entity_and_concept():
    assert WIKI_KINDS == ("entity", "concept")

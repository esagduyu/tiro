"""Sync S2: diff(manifest, shadow) -> ops. Pure-data tests — manifests and
shadows are built by hand, no library needed except for hydrate_bodies."""

from tiro.sync.journal import (
    Alias,
    FileDel,
    FilePut,
    HLCClock,
    LineDel,
    LinePut,
    Meta,
    RowDel,
    RowPut,
)
from tiro.sync.manifest import Manifest, ManifestEntry, Shadow, diff


def _clock():
    return HLCClock("dev-a", now_ms=lambda: 1720000000000)


def _article(uid="01A", h="h1", **over):
    fields = {
        "path_hint": "articles/2026-07-10_x.md", "url": "https://e.com/x",
        "rating": None, "is_read": 0, "snoozed_until": None,
        "opened_count": 0, "source_uid": "01S",
        "meta_updated_at": None,
    }
    fields.update(over)
    return ManifestEntry(kind="article", uid=uid, hash=h, fields=fields)


def _manifest(*entries):
    m = Manifest()
    for e in entries:
        m.add(e)
    return m


def _shadow(*entries, tombstones=None):
    s = Shadow()
    for e in entries:
        s.entries[(e.kind, e.uid)] = e
    for key, when in (tombstones or {}).items():
        s.tombstones[key] = when
    return s


class TestDiff:
    def test_empty_vs_empty_is_no_ops(self):
        assert diff(_manifest(), _shadow(), clock=_clock()) == []

    def test_unchanged_entry_emits_nothing(self):
        e = _article()
        prev = ManifestEntry(kind="article", uid="01A", hash="h1",
                             fields=dict(e.fields),
                             hlc="0000000000001-000000-dev-a")
        assert diff(_manifest(e), _shadow(prev), clock=_clock()) == []

    def test_new_article_emits_file_put_and_nondefault_meta(self):
        e = _article(rating=2, opened_count=3)
        ops = diff(_manifest(e), _shadow(), clock=_clock())
        puts = [o for o in ops if isinstance(o, FilePut)]
        metas = {o.field: o for o in ops if isinstance(o, Meta)}
        assert len(puts) == 1
        assert puts[0].uid == "01A"
        assert puts[0].object_hash == "h1"
        assert puts[0].base_hash is None
        assert puts[0].body is None  # diff never reads disk
        # Non-default meta fields travel with the create; defaults don't.
        assert set(metas) == {"rating", "opened_count", "source_uid"}
        assert metas["rating"].value == 2

    def test_body_change_emits_file_put_with_base_hash(self):
        prev = ManifestEntry(kind="article", uid="01A", hash="h0",
                             fields=_article().fields, hlc="0000000000001-000000-x")
        ops = diff(_manifest(_article(h="h1")), _shadow(prev), clock=_clock())
        assert len(ops) == 1
        assert isinstance(ops[0], FilePut) and ops[0].base_hash == "h0"

    def test_meta_change_emits_per_field_ops_only(self):
        cur = _article(rating=1, is_read=1,
                       meta_updated_at="2026-07-10T00:00:05Z")
        prev = ManifestEntry(kind="article", uid="01A", hash="h1",
                             fields=_article().fields, hlc="0000000000001-000000-x")
        ops = diff(_manifest(cur), _shadow(prev), clock=_clock())
        assert all(isinstance(o, Meta) for o in ops)
        assert {o.field for o in ops} == {"rating", "is_read"}
        assert all(o.ts == "2026-07-10T00:00:05Z" for o in ops)

    def test_meta_ts_falls_back_to_hlc_wall_clock(self):
        cur = _article(rating=1)  # meta_updated_at stays None
        prev = ManifestEntry(kind="article", uid="01A", hash="h1",
                             fields=_article().fields, hlc="0000000000001-000000-x")
        (op,) = diff(_manifest(cur), _shadow(prev), clock=_clock())
        assert op.ts == "2024-07-03T09:46:40Z"  # 1720000000000 ms epoch, UTC

    def test_deleted_article_emits_row_del_with_observed_hash(self):
        prev = ManifestEntry(kind="article", uid="01A", hash="h1",
                             fields=_article().fields, hlc="0000000000001-000000-x")
        (op,) = diff(_manifest(), _shadow(prev), clock=_clock())
        assert isinstance(op, RowDel)
        assert op.table == "articles" and op.uid == "01A" and op.observed == "h1"

    def test_tombstoned_entry_not_redeleted_but_recreate_wins(self):
        tomb = {("article", "01A"): "2026-07-09T00:00:00Z"}
        assert diff(_manifest(), _shadow(tombstones=tomb), clock=_clock()) == []
        ops = diff(_manifest(_article()), _shadow(tombstones=tomb), clock=_clock())
        assert any(isinstance(o, FilePut) for o in ops)  # resurrection = create

    def test_highlight_line_ops(self):
        line = {"uid": "01H", "article_uid": "01A", "quote": "q",
                "updated_at": "2026-07-10T00:00:00Z"}
        cur = ManifestEntry(kind="highlight", uid="01H", hash="lh1",
                            fields={"article_uid": "01A", "line": line})
        ops = diff(_manifest(cur), _shadow(), clock=_clock())
        assert isinstance(ops[0], LinePut) and ops[0].line == line
        prev = ManifestEntry(kind="highlight", uid="01H", hash="lh1",
                             fields={"article_uid": "01A", "line": line},
                             hlc="0000000000001-000000-x")
        (op,) = diff(_manifest(), _shadow(prev), clock=_clock())
        assert isinstance(op, LineDel)
        assert op.observed_updated_at == "2026-07-10T00:00:00Z"
        assert op.article_uid == "01A"

    def test_note_and_wiki_and_pathfile_file_ops(self):
        note = ManifestEntry(kind="note", uid="01A", hash="n1",
                             fields={"path_hint": "notes/x.md"})
        prevw = ManifestEntry(kind="wiki", uid="01W", hash="w1",
                              fields={"path_hint": "wiki/entities/e.md"},
                              hlc="0000000000001-000000-x")
        ops = diff(_manifest(note), _shadow(prevw), clock=_clock())
        kinds = {type(o) for o in ops}
        assert kinds == {FilePut, FileDel}
        fd = next(o for o in ops if isinstance(o, FileDel))
        assert fd.path_hint == "wiki/entities/e.md" and fd.base_hash == "w1"

    def test_row_and_link_ops(self):
        row = ManifestEntry(kind="row:tags", uid="01T", hash="r1",
                            fields={"uid": "01T", "name": "ml"})
        prev_link = ManifestEntry(kind="link:article_tags", uid="01A:01T",
                                  hash="l1", fields={"a_uid": "01A", "b_uid": "01T"},
                                  hlc="0000000000009-000000-x")
        ops = diff(_manifest(row), _shadow(prev_link), clock=_clock())
        rp = next(o for o in ops if isinstance(o, RowPut))
        rd = next(o for o in ops if isinstance(o, RowDel))
        assert rp.table == "tags" and rp.row == {"uid": "01T", "name": "ml"}
        assert rd.table == "article_tags"
        assert rd.observed == "0000000000009-000000-x"  # add-wins context

    def test_ops_deterministically_ordered(self):
        a, b = _article(uid="01A"), _article(uid="01B", h="h2")
        ops1 = diff(_manifest(a, b), _shadow(), clock=_clock())
        ops2 = diff(_manifest(b, a), _shadow(), clock=_clock())
        assert [(type(o).kind, o.uid) for o in ops1] == \
               [(type(o).kind, o.uid) for o in ops2]

    def test_never_emits_alias(self):
        # diff never invents aliases — only apply's dedupe does (decision #12).
        ops = diff(_manifest(_article()), _shadow(), clock=_clock())
        assert not any(isinstance(o, Alias) for o in ops)


class TestUnreadable:
    """manifest.unreadable = files that EXIST but could not be read at build
    time. diff must mirror save_shadow's posture (S2.1+S2.2 review, Major #2):
    unreadable is UNKNOWN, never deleted and never a phantom body change."""

    def test_shadow_entry_with_unreadable_file_emits_no_delete(self):
        prev = ManifestEntry(kind="article", uid="01A", hash="h1",
                             fields=_article().fields, hlc="0000000000001-000000-x")
        prevn = ManifestEntry(kind="note", uid="01A", hash="n1",
                              fields={"path_hint": "notes/x.md"},
                              hlc="0000000000001-000000-x")
        m = _manifest()
        m.unreadable = {"articles/2026-07-10_x.md", "notes/x.md"}
        # No RowDel for the article, no FileDel for the note — unreadable is
        # unknown, not deleted.
        assert diff(m, _shadow(prev, prevn), clock=_clock()) == []

    def test_unreadable_article_file_emits_no_file_put_but_meta_still_flows(self):
        # Article row is readable (SQLite) but its file was not: hash is None
        # and its path_hint sits in manifest.unreadable. Meta ops still flow;
        # no FilePut is ever emitted for an unread body.
        cur = _article(h=None, rating=2)
        prev = ManifestEntry(kind="article", uid="01A", hash="h1",
                             fields=_article().fields, hlc="0000000000001-000000-x")
        m = _manifest(cur)
        m.unreadable = {"articles/2026-07-10_x.md"}
        ops = diff(m, _shadow(prev), clock=_clock())
        assert not any(isinstance(o, FilePut) for o in ops)
        assert [o.field for o in ops if isinstance(o, Meta)] == ["rating"]


class TestHydrate:
    def test_hydrate_fills_bodies_and_drops_vanished(self, initialized_library):
        from tiro.anchors import content_hash
        from tiro.sync.manifest import hydrate_bodies

        p = initialized_library.articles_dir / "x.md"
        p.write_text("# X\n")
        clock = _clock()
        ops = [
            FilePut(op_id="1", hlc=clock.tick(), device="dev-a", uid="01A",
                    path_hint="articles/x.md", object_hash=content_hash("# X\n")),
            FilePut(op_id="2", hlc=clock.tick(), device="dev-a", uid="01B",
                    path_hint="articles/gone.md", object_hash="dead" * 16),
        ]
        out = hydrate_bodies(initialized_library, ops)
        assert len(out) == 1 and out[0].body == "# X\n"

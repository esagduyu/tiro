"""Sync S2: meta/row/link ops, article tombstones, mass-delete guard."""
import pytest

from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.migrations import new_ulid
from tiro.sync import reconcile as rec
from tiro.sync.journal import HLCClock, Meta, RowDel, RowPut
from tiro.sync.merge import apply_ops


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)


def _ingest(config, title="Hello World", body="# Hello\n\nSome body text.",
            url="https://example.com/hello"):
    return process_article(
        title=title, author="A. Writer", content_md=body, url=url, config=config,
    )


def _row(config, table, uid):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(f"SELECT * FROM {table} WHERE uid = ?",
                            (uid,)).fetchone()
    finally:
        conn.close()


def _clock(ms=1720000000000, device="dev-b"):
    return HLCClock(device, now_ms=lambda: ms)


def _meta(uid, field, value, ts, clock=None):
    clock = clock or _clock()
    return Meta(op_id=new_ulid(), hlc=clock.tick(), device=clock.device,
                uid=uid, field=field, value=value, ts=ts)


class TestMetaOps:
    def test_lww_field_apply_and_stale_skip(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            arow = conn.execute("SELECT uid FROM articles WHERE id = ?",
                                (art["id"],)).fetchone()
        finally:
            conn.close()
        uid = arow["uid"]
        r1 = apply_ops(initialized_library,
                       [_meta(uid, "rating", 2, "2026-07-11T00:00:00Z")])
        assert r1.applied == 1
        # An OLDER ts for the same field loses:
        r2 = apply_ops(initialized_library,
                       [_meta(uid, "rating", -1, "2026-07-10T00:00:00Z")])
        assert r2.skipped_stale == 1
        conn = get_connection(initialized_library.db_path)
        try:
            a = conn.execute(
                "SELECT rating, meta_updated_at FROM articles WHERE uid = ?",
                (uid,)).fetchone()
            assert a["rating"] == 2
            assert a["meta_updated_at"] == "2026-07-11T00:00:00Z"
        finally:
            conn.close()

    def test_unmark_ops_are_ordinary_writes(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            uid = conn.execute("SELECT uid FROM articles WHERE id = ?",
                               (art["id"],)).fetchone()["uid"]
        finally:
            conn.close()
        apply_ops(initialized_library,
                  [_meta(uid, "is_read", 1, "2026-07-11T00:00:00Z")])
        apply_ops(initialized_library,
                  [_meta(uid, "is_read", 0, "2026-07-12T00:00:00Z"),
                   _meta(uid, "rating", None, "2026-07-12T00:00:01Z")])
        conn = get_connection(initialized_library.db_path)
        try:
            a = conn.execute(
                "SELECT is_read, rating, opened_count FROM articles WHERE uid = ?",
                (uid,)).fetchone()
            assert a["is_read"] == 0 and a["rating"] is None
        finally:
            conn.close()

    def test_opened_count_is_max_merge(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            uid = conn.execute("SELECT uid FROM articles WHERE id = ?",
                               (art["id"],)).fetchone()["uid"]
            conn.execute("UPDATE articles SET opened_count = 5 WHERE uid = ?",
                         (uid,))
            conn.commit()
        finally:
            conn.close()
        apply_ops(initialized_library,
                  [_meta(uid, "opened_count", 3, "2099-01-01T00:00:00Z")])
        assert _row(initialized_library, "articles", uid)["opened_count"] == 5
        apply_ops(initialized_library,
                  [_meta(uid, "opened_count", 9, "2099-01-02T00:00:00Z")])
        assert _row(initialized_library, "articles", uid)["opened_count"] == 9

    def test_disallowed_field_is_error(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            uid = conn.execute("SELECT uid FROM articles WHERE id = ?",
                               (art["id"],)).fetchone()["uid"]
        finally:
            conn.close()
        r = apply_ops(initialized_library,
                      [_meta(uid, "title", "hax", "2099-01-01T00:00:00Z")])
        assert r.errors == 1

    def test_unknown_article_deferred(self, initialized_library):
        r = apply_ops(initialized_library,
                      [_meta("01NOPE", "rating", 1, "2026-07-11T00:00:00Z")])
        assert r.deferred == 1

    def test_source_uid_repoint(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            uid = conn.execute("SELECT uid FROM articles WHERE id = ?",
                               (art["id"],)).fetchone()["uid"]
            src_uid = new_ulid()
            conn.execute(
                "INSERT INTO sources (uid, name, source_type) "
                "VALUES (?, 'Other', 'web')", (src_uid,))
            conn.commit()
        finally:
            conn.close()
        r = apply_ops(initialized_library,
                      [_meta(uid, "source_uid", src_uid, "2099-01-01T00:00:00Z")])
        assert r.applied == 1
        conn = get_connection(initialized_library.db_path)
        try:
            a = conn.execute(
                "SELECT s.uid AS suid FROM articles a JOIN sources s "
                "ON s.id = a.source_id WHERE a.uid = ?", (uid,)).fetchone()
            assert a["suid"] == src_uid
        finally:
            conn.close()


class TestRowOps:
    def test_row_put_insert_then_lww_update(self, initialized_library):
        suid = new_ulid()
        row = {"uid": suid, "name": "Remote Source", "domain": "r.example.com",
               "email_sender": None, "source_type": "web", "is_vip": 1,
               "created_at": "2026-07-01 00:00:00"}
        clock_old = _clock(ms=1000)
        clock_new = _clock(ms=2000)
        op_new = RowPut(op_id=new_ulid(), hlc=clock_new.tick(), device="dev-b",
                        uid=suid, table="sources",
                        row={**row, "name": "Renamed"})
        op_old = RowPut(op_id=new_ulid(), hlc=clock_old.tick(), device="dev-c",
                        uid=suid, table="sources", row=row)
        r = apply_ops(initialized_library, [op_new])
        assert r.applied == 1
        assert _row(initialized_library, "sources", suid)["name"] == "Renamed"
        r2 = apply_ops(initialized_library, [op_old])   # older op arrives late
        assert r2.skipped_stale == 1
        assert _row(initialized_library, "sources", suid)["name"] == "Renamed"

    def test_digest_prefer_newer_created_at(self, initialized_library):
        base = {"date": "2026-07-10", "digest_type": "ranked",
                "article_ids": "[]"}
        clock = _clock()
        newer = RowPut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                       uid="2026-07-10:ranked", table="digests",
                       row={**base, "content": "NEW",
                            "created_at": "2026-07-10 09:00:00"})
        older = RowPut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                       uid="2026-07-10:ranked", table="digests",
                       row={**base, "content": "OLD",
                            "created_at": "2026-07-10 07:00:00"})
        apply_ops(initialized_library, [newer])
        apply_ops(initialized_library, [older])  # prefer-newer: no downgrade
        conn = get_connection(initialized_library.db_path)
        try:
            d = conn.execute(
                "SELECT content FROM digests WHERE date='2026-07-10' "
                "AND digest_type='ranked'").fetchone()
            assert d["content"] == "NEW"
        finally:
            conn.close()

    def test_unknown_table_is_error(self, initialized_library):
        clock = _clock()
        op = RowPut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                    uid="x", table="api_tokens", row={"uid": "x"})
        r = apply_ops(initialized_library, [op])
        assert r.errors == 1  # auth tables are NEVER synced (spec §2)


class TestLinkOps:
    def _seed(self, config):
        art = _ingest(config)
        conn = get_connection(config.db_path)
        try:
            a_uid = conn.execute("SELECT uid FROM articles WHERE id = ?",
                                 (art["id"],)).fetchone()["uid"]
            t_uid = new_ulid()
            conn.execute("INSERT INTO tags (uid, name) VALUES (?, 'synced')",
                         (t_uid,))
            conn.commit()
        finally:
            conn.close()
        return art, a_uid, t_uid

    def _has_link(self, config, a_uid, t_uid):
        conn = get_connection(config.db_path)
        try:
            return conn.execute(
                "SELECT 1 FROM article_tags j JOIN articles a ON a.id = j.article_id "
                "JOIN tags t ON t.id = j.tag_id WHERE a.uid = ? AND t.uid = ?",
                (a_uid, t_uid)).fetchone() is not None
        finally:
            conn.close()

    def test_link_add_and_remove(self, initialized_library):
        art, a_uid, t_uid = self._seed(initialized_library)
        clock = _clock(ms=1000)
        add = RowPut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=f"{a_uid}:{t_uid}", table="article_tags",
                     row={"a_uid": a_uid, "b_uid": t_uid})
        r = apply_ops(initialized_library, [add])
        assert r.applied == 1 and self._has_link(initialized_library, a_uid, t_uid)
        rem_clock = _clock(ms=2000)
        rem = RowDel(op_id=new_ulid(), hlc=rem_clock.tick(), device="dev-b",
                     uid=f"{a_uid}:{t_uid}", table="article_tags",
                     observed=add.hlc.to_str())
        r2 = apply_ops(initialized_library, [rem])
        assert r2.applied == 1
        assert not self._has_link(initialized_library, a_uid, t_uid)

    def test_link_add_wins_over_concurrent_remove(self, initialized_library):
        art, a_uid, t_uid = self._seed(initialized_library)
        old_clock = _clock(ms=1000)
        old_add_hlc = old_clock.tick()
        new_clock = _clock(ms=3000)
        readd = RowPut(op_id=new_ulid(), hlc=new_clock.tick(), device="dev-c",
                       uid=f"{a_uid}:{t_uid}", table="article_tags",
                       row={"a_uid": a_uid, "b_uid": t_uid})
        apply_ops(initialized_library, [readd])
        # Concurrent remover only ever SAW the old add:
        rem_clock = _clock(ms=2000)
        rem = RowDel(op_id=new_ulid(), hlc=rem_clock.tick(), device="dev-b",
                     uid=f"{a_uid}:{t_uid}", table="article_tags",
                     observed=old_add_hlc.to_str())
        r = apply_ops(initialized_library, [rem])
        assert self._has_link(initialized_library, a_uid, t_uid)  # add won
        assert r.skipped_stale == 1

    def test_link_referencing_missing_row_deferred(self, initialized_library):
        art, a_uid, _t = self._seed(initialized_library)
        clock = _clock()
        op = RowPut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                    uid=f"{a_uid}:01MISSINGTAG", table="article_tags",
                    row={"a_uid": a_uid, "b_uid": "01MISSINGTAG"})
        r = apply_ops(initialized_library, [op])
        assert r.deferred == 1 and r.errors == 0


class TestArticleTombstone:
    def test_clean_delete_goes_through_delete_article(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT uid, body_hash, markdown_path FROM articles WHERE id = ?",
                (art["id"],)).fetchone()
        finally:
            conn.close()
        clock = _clock()
        op = RowDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                    uid=row["uid"], table="articles", observed=row["body_hash"])
        r = apply_ops(initialized_library, [op])
        assert r.applied == 1 and r.tombstones == 1
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT 1 FROM articles WHERE uid = ?",
                                (row["uid"],)).fetchone() is None
        finally:
            conn.close()
        assert not (initialized_library.articles_dir / row["markdown_path"]).exists()

    def test_delete_vs_concurrent_edit_edit_wins(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT uid, body_hash FROM articles WHERE id = ?",
                (art["id"],)).fetchone()
        finally:
            conn.close()
        clock = _clock()
        op = RowDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                    uid=row["uid"], table="articles",
                    observed="0" * 64)  # deleter saw a DIFFERENT body
        r = apply_ops(initialized_library, [op])
        assert r.resurrected == 1 and r.tombstones == 0
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT 1 FROM articles WHERE uid = ?",
                                (row["uid"],)).fetchone() is not None
        finally:
            conn.close()


class TestS26ReviewFixes:
    """Targeted pins for the S2.6 review findings (F1, F3–F8)."""

    TS = "2026-07-11T00:00:00Z"

    def _article_uid(self, config):
        art = _ingest(config)
        conn = get_connection(config.db_path)
        try:
            return art, conn.execute(
                "SELECT uid FROM articles WHERE id = ?",
                (art["id"],)).fetchone()["uid"]
        finally:
            conn.close()

    def _mk_source(self, config, src_uid, name):
        conn = get_connection(config.db_path)
        try:
            conn.execute("INSERT INTO sources (uid, name, source_type) "
                         "VALUES (?, ?, 'web')", (src_uid, name))
            conn.commit()
        finally:
            conn.close()

    def _current_source_uid(self, config, uid):
        conn = get_connection(config.db_path)
        try:
            return conn.execute(
                "SELECT s.uid AS v, a.meta_updated_at AS ts FROM articles a "
                "LEFT JOIN sources s ON s.id = a.source_id "
                "WHERE a.uid = ?", (uid,)).fetchone()
        finally:
            conn.close()

    def _seed_source_tie(self, config):
        """Stamp meta_updated_at = TS by applying a first source_uid op.
        Note Task 8's property strategy excludes source_uid — these example
        tests are the pin for the F1 tie-break."""
        _art, uid = self._article_uid(config)
        mid = "01TIESRCMMMMMMMMMMMMMMMMMM"
        self._mk_source(config, mid, "Mid")
        r = apply_ops(config, [_meta(uid, "source_uid", mid, self.TS)])
        assert r.applied == 1
        return uid, mid

    def test_f1_equal_ts_source_uid_larger_wins(self, initialized_library):
        uid, _mid = self._seed_source_tie(initialized_library)
        larger = "01TIESRCZZZZZZZZZZZZZZZZZZ"
        self._mk_source(initialized_library, larger, "Larger")
        r = apply_ops(initialized_library,
                      [_meta(uid, "source_uid", larger, self.TS)])
        assert r.applied == 1
        cur = self._current_source_uid(initialized_library, uid)
        assert cur["v"] == larger and cur["ts"] == self.TS

    def test_f1_equal_ts_source_uid_smaller_skipped(self, initialized_library):
        uid, mid = self._seed_source_tie(initialized_library)
        smaller = "01TIESRCAAAAAAAAAAAAAAAAAA"
        self._mk_source(initialized_library, smaller, "Smaller")
        r = apply_ops(initialized_library,
                      [_meta(uid, "source_uid", smaller, self.TS)])
        assert r.skipped_stale == 1
        cur = self._current_source_uid(initialized_library, uid)
        assert cur["v"] == mid and cur["ts"] == self.TS

    def test_f1_equal_ts_source_uid_echo_is_idempotent_skip(
            self, initialized_library):
        uid, mid = self._seed_source_tie(initialized_library)
        r = apply_ops(initialized_library,
                      [_meta(uid, "source_uid", mid, self.TS)])
        assert r.skipped_stale == 1 and r.applied == 0
        assert self._current_source_uid(initialized_library, uid)["v"] == mid

    def test_f2_equal_ts_ordinary_field_larger_value_wins_both_orders(
            self, initialized_library):
        # F2 was verified SOUND — this pins the algebra: at equal ts the
        # canonical-JSON-larger value wins regardless of arrival order.
        _a1, uid1 = self._article_uid(initialized_library)
        art2 = _ingest(initialized_library, title="Second",
                       url="https://example.com/second")
        conn = get_connection(initialized_library.db_path)
        try:
            uid2 = conn.execute("SELECT uid FROM articles WHERE id = ?",
                                (art2["id"],)).fetchone()["uid"]
        finally:
            conn.close()
        # Order A: 1 then 2 — the later, larger op applies.
        apply_ops(initialized_library, [_meta(uid1, "rating", 1, self.TS)])
        ra = apply_ops(initialized_library, [_meta(uid1, "rating", 2, self.TS)])
        assert ra.applied == 1
        # Order B: 2 then 1 — the later, smaller op is skipped.
        apply_ops(initialized_library, [_meta(uid2, "rating", 2, self.TS)])
        rb = apply_ops(initialized_library, [_meta(uid2, "rating", 1, self.TS)])
        assert rb.skipped_stale == 1
        assert _row(initialized_library, "articles", uid1)["rating"] == 2
        assert _row(initialized_library, "articles", uid2)["rating"] == 2

    def test_f3_never_synced_link_survives_remove_with_observed(
            self, initialized_library):
        _art, a_uid = self._article_uid(initialized_library)
        t_uid = new_ulid()
        conn = get_connection(initialized_library.db_path)
        try:
            conn.execute("INSERT INTO tags (uid, name) VALUES (?, 'localonly')",
                         (t_uid,))
            tag_id = conn.execute("SELECT id FROM tags WHERE uid = ?",
                                  (t_uid,)).fetchone()["id"]
            art_id = conn.execute("SELECT id FROM articles WHERE uid = ?",
                                  (a_uid,)).fetchone()["id"]
            conn.execute("INSERT INTO article_tags (article_id, tag_id) "
                         "VALUES (?, ?)", (art_id, tag_id))
            conn.commit()  # local link, NO sync_shadow row
        finally:
            conn.close()
        clock = _clock(ms=5000)
        rem = RowDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=f"{a_uid}:{t_uid}", table="article_tags",
                     observed=_clock(ms=999, device="dev-x").tick().to_str())
        r = apply_ops(initialized_library, [rem])
        assert r.skipped_stale == 1 and r.applied == 0
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute(
                "SELECT 1 FROM article_tags j JOIN tags t ON t.id = j.tag_id "
                "WHERE t.uid = ?", (t_uid,)).fetchone() is not None
        finally:
            conn.close()

    def test_f4_article_tombstone_observed_none_resurrects(
            self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT uid, markdown_path FROM articles WHERE id = ?",
                (art["id"],)).fetchone()
        finally:
            conn.close()
        clock = _clock()
        op = RowDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                    uid=row["uid"], table="articles", observed=None)
        r = apply_ops(initialized_library, [op])
        assert r.resurrected == 1 and r.tombstones == 0 and r.applied == 0
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT 1 FROM articles WHERE uid = ?",
                                (row["uid"],)).fetchone() is not None
        finally:
            conn.close()
        assert (initialized_library.articles_dir / row["markdown_path"]).exists()

    def test_f5_row_del_of_referenced_tag_is_deferred(self, initialized_library):
        _art, a_uid = self._article_uid(initialized_library)
        t_uid = new_ulid()
        conn = get_connection(initialized_library.db_path)
        try:
            conn.execute("INSERT INTO tags (uid, name) VALUES (?, 'pinned')",
                         (t_uid,))
            tag_id = conn.execute("SELECT id FROM tags WHERE uid = ?",
                                  (t_uid,)).fetchone()["id"]
            art_id = conn.execute("SELECT id FROM articles WHERE uid = ?",
                                  (a_uid,)).fetchone()["id"]
            conn.execute("INSERT INTO article_tags (article_id, tag_id) "
                         "VALUES (?, ?)", (art_id, tag_id))
            conn.commit()
        finally:
            conn.close()
        clock = _clock()
        op = RowDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                    uid=t_uid, table="tags", observed=None)
        r = apply_ops(initialized_library, [op])
        assert r.deferred == 1 and r.errors == 0 and r.tombstones == 0
        assert _row(initialized_library, "tags", t_uid) is not None
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute(
                "SELECT 1 FROM sync_shadow WHERE kind = 'row:tags' AND uid = ?",
                (t_uid,)).fetchone() is None  # no shadow tombstone
        finally:
            conn.close()

    def test_f5_row_del_of_unreferenced_tag_applies(self, initialized_library):
        t_uid = new_ulid()
        conn = get_connection(initialized_library.db_path)
        try:
            conn.execute("INSERT INTO tags (uid, name) VALUES (?, 'loose')",
                         (t_uid,))
            conn.commit()
        finally:
            conn.close()
        clock = _clock()
        op = RowDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                    uid=t_uid, table="tags", observed=None)
        r = apply_ops(initialized_library, [op])
        assert r.applied == 1 and r.tombstones == 1
        assert _row(initialized_library, "tags", t_uid) is None
        conn = get_connection(initialized_library.db_path)
        try:
            srow = conn.execute(
                "SELECT deleted_at FROM sync_shadow WHERE kind = 'row:tags' "
                "AND uid = ?", (t_uid,)).fetchone()
            assert srow is not None and srow["deleted_at"] is not None
        finally:
            conn.close()

    def test_f6_digest_prefer_newer_is_order_independent(
            self, initialized_library):
        # The previously-diverging order: hlc-NEWER/created-OLDER arrives
        # first — the shadow hlc gate must not block the created-newer op.
        base = {"date": "2026-07-10", "digest_type": "ranked",
                "article_ids": "[]"}
        hi_clock = _clock(ms=9000)
        lo_clock = _clock(ms=1000)
        hlc_newer_created_older = RowPut(
            op_id=new_ulid(), hlc=hi_clock.tick(), device="dev-b",
            uid="2026-07-10:ranked", table="digests",
            row={**base, "content": "OLD",
                 "created_at": "2026-07-10 07:00:00"})
        hlc_older_created_newer = RowPut(
            op_id=new_ulid(), hlc=lo_clock.tick(), device="dev-c",
            uid="2026-07-10:ranked", table="digests",
            row={**base, "content": "NEW",
                 "created_at": "2026-07-10 09:00:00"})
        apply_ops(initialized_library, [hlc_newer_created_older])
        r = apply_ops(initialized_library, [hlc_older_created_newer])
        assert r.applied == 1
        conn = get_connection(initialized_library.db_path)
        try:
            d = conn.execute(
                "SELECT content FROM digests WHERE date='2026-07-10' "
                "AND digest_type='ranked'").fetchone()
            assert d["content"] == "NEW"
        finally:
            conn.close()

    def test_f7_ts_none_meta_op_never_regresses_the_clock(
            self, initialized_library):
        _art, uid = self._article_uid(initialized_library)
        apply_ops(initialized_library, [_meta(uid, "rating", 2, self.TS)])
        r = apply_ops(initialized_library, [_meta(uid, "rating", -1, None)])
        assert r.skipped_stale == 1 and r.applied == 0
        a = _row(initialized_library, "articles", uid)
        assert a["rating"] == 2 and a["meta_updated_at"] == self.TS

    def test_f8_digest_uid_payload_mismatch_is_error(self, initialized_library):
        clock = _clock()
        op = RowPut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                    uid="2026-07-10:ranked", table="digests",
                    row={"date": "2026-07-11", "digest_type": "ranked",
                         "content": "X", "article_ids": "[]",
                         "created_at": "2026-07-11 09:00:00"})
        r = apply_ops(initialized_library, [op])
        assert r.errors == 1 and r.applied == 0
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT 1 FROM digests").fetchone() is None
        finally:
            conn.close()


class TestMassDeleteGuard:
    def test_guard_halts_whole_batch(self, initialized_library):
        for i in range(12):
            _ingest(initialized_library, title=f"T{i}",
                    url=f"https://example.com/{i}")
        conn = get_connection(initialized_library.db_path)
        try:
            uids = [r["uid"] for r in conn.execute(
                "SELECT uid FROM articles ORDER BY id")]
        finally:
            conn.close()
        clock = _clock()
        ops = [RowDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                      uid=u, table="articles", observed=None)
               for u in uids[:11]]  # 11 of 12 > max(10, ceil(2.4))
        # Plus one innocuous meta op — the guard must halt EVERYTHING.
        ops.append(_meta(uids[11], "rating", 2, "2099-01-01T00:00:00Z"))
        r = apply_ops(initialized_library, ops)
        assert r.guard and r.applied == 0
        conn = get_connection(initialized_library.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
            rated = conn.execute(
                "SELECT rating FROM articles WHERE uid = ?", (uids[11],)
            ).fetchone()["rating"]
        finally:
            conn.close()
        assert n == 12 and rated is None

    def test_guard_false_applies_anyway(self, initialized_library):
        for i in range(12):
            _ingest(initialized_library, title=f"U{i}",
                    url=f"https://example.com/u{i}")
        conn = get_connection(initialized_library.db_path)
        try:
            rows = conn.execute(
                "SELECT uid, body_hash FROM articles ORDER BY id").fetchall()
        finally:
            conn.close()
        clock = _clock()
        ops = [RowDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                      uid=r["uid"], table="articles", observed=r["body_hash"])
               for r in rows[:11]]
        r = apply_ops(initialized_library, ops, guard=False)
        assert r.guard is None and r.tombstones == 11


class TestS28MetaClockFix:
    """S2.8 hard-review Blocker pin: the per-field metats gate must not let
    an OLDER remote meta op overwrite a NEWER un-pushed LOCAL edit (LWW
    inversion). Local edits are detected by divergence from the article
    shadow entry's stored field value (decision #8's file-rule mirror)."""

    def _synced_article(self, config):
        from tiro.sync.journal import HLCClock
        from tiro.sync.manifest import build_manifest, save_shadow

        art = _ingest(config)
        conn = get_connection(config.db_path)
        try:
            uid = conn.execute("SELECT uid FROM articles WHERE id = ?",
                               (art["id"],)).fetchone()["uid"]
        finally:
            conn.close()
        # Remote rating=1 applied at T05, then a full save_shadow so the
        # article shadow entry stores the synced field values.
        apply_ops(config, [_meta(uid, "rating", 1, "2026-07-10T00:00:05Z")])
        save_shadow(config, build_manifest(config),
                    clock=HLCClock("dev-a", now_ms=lambda: 1))
        return art, uid

    def _local_edit(self, config, article_id, rating=2,
                    mu="2026-07-10T00:00:10Z"):
        conn = get_connection(config.db_path)
        try:
            conn.execute(
                "UPDATE articles SET rating = ?, meta_updated_at = ? "
                "WHERE id = ?", (rating, mu, article_id))
            conn.commit()
        finally:
            conn.close()

    def test_older_remote_op_never_overwrites_newer_local_edit(
            self, initialized_library):
        art, uid = self._synced_article(initialized_library)
        # LOCAL route-style edit at T10 (bumps meta_updated_at only).
        self._local_edit(initialized_library, art["id"])
        # Remote op OLDER than the local edit (T09 < T10) must be skipped
        # even though it is newer than the last-SYNCED metats (T05).
        r = apply_ops(initialized_library,
                      [_meta(uid, "rating", 1, "2026-07-10T00:00:09Z")])
        assert r.skipped_stale == 1 and r.applied == 0
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT rating FROM articles WHERE uid = ?",
                                (uid,)).fetchone()["rating"] == 2
        finally:
            conn.close()

    def test_newer_remote_op_still_beats_local_edit(self, initialized_library):
        art, uid = self._synced_article(initialized_library)
        self._local_edit(initialized_library, art["id"])
        r = apply_ops(initialized_library,
                      [_meta(uid, "rating", -1, "2026-07-10T00:00:11Z")])
        assert r.applied == 1
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT rating FROM articles WHERE uid = ?",
                                (uid,)).fetchone()["rating"] == -1
        finally:
            conn.close()


def _uid_for(config, article_id):
    conn = get_connection(config.db_path)
    try:
        return conn.execute("SELECT uid FROM articles WHERE id = ?",
                            (article_id,)).fetchone()["uid"]
    finally:
        conn.close()


class TestTransientInfraErrors:
    """S5.3 review M2: sqlite3.OperationalError is transient infra (e.g.
    "database is locked" from the concurrent S1 reconcile loop) — apply_ops
    must RE-RAISE it so the engine holds the watermark and re-applies the
    whole segment next cycle, instead of folding it into report.errors and
    letting the watermark advance (permanent op loss)."""

    def test_operational_error_reraises(self, initialized_library, monkeypatch):
        import sqlite3

        art = _ingest(initialized_library)
        uid = _uid_for(initialized_library, art["id"])

        def boom(_config, _op, _report):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("tiro.sync.merge._apply_meta", boom)
        with pytest.raises(sqlite3.OperationalError):
            apply_ops(initialized_library,
                      [_meta(uid, "rating", 2, "2026-07-11T00:00:00Z")])

    def test_deterministic_bad_op_still_folds_into_errors(
            self, initialized_library):
        art = _ingest(initialized_library)
        uid = _uid_for(initialized_library, art["id"])
        # Disallowed meta field raises ValueError inside _apply_meta —
        # deterministic, folds into report.errors, never raises out.
        report = apply_ops(initialized_library,
                           [_meta(uid, "not_a_field", 1,
                                  "2026-07-11T00:00:00Z")])
        assert report.errors == 1

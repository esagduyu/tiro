"""Sync S5.9: multi-device integration suite — THE SECOND HALF OF THE
v1.0.0 GO/NO-GO GATE (sync spec §9, FROZEN scenarios).

Every scenario runs two (or three) REAL libraries against the REAL
FilesystemAdapter over a shared temp backend dir, encryption ON with
test-speed Argon2id params. NEVER weaken an assertion, NEVER xfail — a red
scenario is a real engine bug.
"""
import asyncio
from pathlib import Path

import frontmatter
import pytest

from tests.test_reconcile import _ingest
from tests.test_sync_setup_flows import (
    WEAK_KDF,
    _count,
    _second_library,
    _sync_cfg,
    _titles,
)
from tiro.anchors import content_hash
from tiro.annotations import append_highlight, read_annotations, write_note
from tiro.database import get_connection
from tiro.lifecycle import delete_article
from tiro.sync.engine import (
    adapter_for_config,
    bootstrap,
    init_backend,
    load_sync_status,
    read_sync_state,
    sync_cycle,
    verify_passphrase,
)

PASSPHRASE = "gate-passphrase"


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    """The S1 two-poll settle sleep is pure wall-clock cost here — every
    external edit in this suite is complete before the cycle runs."""
    import tiro.sync.reconcile as rec

    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)


@pytest.fixture
def rig(tmp_path, initialized_library):
    """(cfg_a, cfg_b, backend_root). A initialized the encrypted backend; B
    joined via verify_passphrase + bootstrap-on-first-cycle (auto-bootstrap
    needs a snapshot; A's first cycle makes one)."""
    backend = tmp_path / "backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="on")
    cfg_a.sync_enabled = True
    recovery = asyncio.run(
        init_backend(cfg_a, adapter_for_config(cfg_a), PASSPHRASE,
                     kdf_params=WEAK_KDF))
    assert recovery
    cfg_a.sync_identity = recovery

    cfg_b = _second_library(tmp_path, "lib-b")
    _sync_cfg(cfg_b, backend, encrypt="on")
    cfg_b.sync_enabled = True
    identity = asyncio.run(
        verify_passphrase(cfg_b, adapter_for_config(cfg_b), PASSPHRASE))
    assert identity == recovery
    cfg_b.sync_identity = identity
    return cfg_a, cfg_b, backend


# --- helpers -----------------------------------------------------------------


def _sync(cfg, **kw):
    return asyncio.run(sync_cycle(cfg, **kw))


def _q(cfg, sql, *params):
    conn = get_connection(cfg.db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _exec(cfg, sql, *params):
    conn = get_connection(cfg.db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _art(cfg, uid, cols="*"):
    rows = _q(cfg, f"SELECT {cols} FROM articles WHERE uid = ?", uid)
    return rows[0] if rows else None


def _sidecar_bytes(cfg) -> dict:
    """{relpath: bytes} for every annotations/ + notes/ file — read_bytes,
    not read_text (newline honesty)."""
    out = {}
    for sub in ("annotations", "notes"):
        d = cfg.library / sub
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file():
                out[f"{sub}/{p.name}"] = p.read_bytes()
    return out


def _converged(cfg_a, cfg_b):
    """Identical synced state across the two libraries: article rows,
    highlight rows (+ anchored note per article uid), and sidecar file
    sets with byte-identical contents."""
    arts_sql = ("SELECT uid, title, body_hash, rating, is_read "
                "FROM articles ORDER BY uid")
    assert _q(cfg_a, arts_sql) == _q(cfg_b, arts_sql)
    hl_sql = ("SELECT a.uid AS article_uid, h.uid, h.color, "
              "n.body_markdown AS note "
              "FROM highlights h JOIN articles a ON a.id = h.article_id "
              "LEFT JOIN notes n ON n.highlight_id = h.id ORDER BY h.uid")
    assert _q(cfg_a, hl_sql) == _q(cfg_b, hl_sql)
    sa, sb = _sidecar_bytes(cfg_a), _sidecar_bytes(cfg_b)
    assert set(sa) == set(sb)
    for name in sa:
        assert sa[name] == sb[name], f"sidecar bytes differ: {name}"


# --- #0 rig sanity -----------------------------------------------------------


def test_rig_sanity_ingest_sync_bootstrap_converge(rig):
    cfg_a, cfg_b, _backend = rig
    _ingest(cfg_a, title="Hello Sync", url="https://example.com/hello-sync")

    assert _sync(cfg_a).result == "ok"
    rb = _sync(cfg_b)  # auto-bootstrap (D-S5-3)

    assert rb.result == "ok"
    assert _titles(cfg_b) == {"Hello Sync"}
    _converged(cfg_a, cfg_b)


# --- #1 concurrent rating flip (LWW) -----------------------------------------


def test_concurrent_rating_flip_lww(rig):
    cfg_a, cfg_b, _backend = rig
    art = _ingest(cfg_a, title="Rated", url="https://example.com/rated")
    uid = _q(cfg_a, "SELECT uid FROM articles WHERE id = ?",
             art["id"])[0]["uid"]
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    assert _art(cfg_b, uid) is not None

    # Both rate "offline": A at T, B at T+5s (explicit LWW stamps, the same
    # ISO-Z shape the rate/read routes write).
    _exec(cfg_a, "UPDATE articles SET rating = 1, meta_updated_at = ? "
          "WHERE uid = ?", "2026-07-15T10:00:00Z", uid)
    _exec(cfg_b, "UPDATE articles SET rating = 2, meta_updated_at = ? "
          "WHERE uid = ?", "2026-07-15T10:00:05Z", uid)

    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    assert _sync(cfg_a).result == "ok"

    assert _art(cfg_a, uid)["rating"] == 2
    assert _art(cfg_b, uid)["rating"] == 2
    _converged(cfg_a, cfg_b)


# --- #2 concurrent note edit -> conflict file on BOTH ------------------------


def test_concurrent_note_edit_conflicts_on_both(rig):
    cfg_a, cfg_b, _backend = rig
    art = _ingest(cfg_a, title="Noted", url="https://example.com/noted")
    stem = Path(_q(cfg_a, "SELECT markdown_path FROM articles WHERE id = ?",
                   art["id"])[0]["markdown_path"]).stem
    write_note(cfg_a, stem, "shared base note\n")
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    assert (cfg_b.library / "notes" / f"{stem}.md").read_text() == \
        "shared base note\n"

    (cfg_a.library / "notes" / f"{stem}.md").write_text("version from A\n")
    (cfg_b.library / "notes" / f"{stem}.md").write_text("version from B\n")

    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"

    for cfg in (cfg_a, cfg_b):
        note_files = sorted((cfg.library / "notes").glob("*.md"))
        conflicts = [p for p in note_files if ".conflict-" in p.name]
        assert conflicts, f"no conflict file in {cfg.library}/notes"
        combined = "\n".join(p.read_text() for p in note_files)
        assert "version from A" in combined
        assert "version from B" in combined
    _converged(cfg_a, cfg_b)


# --- #3 offline/offline/both-online ------------------------------------------


def test_offline_offline_both_online(rig):
    cfg_a, cfg_b, _backend = rig
    base = _ingest(cfg_a, title="Base", url="https://example.com/base")
    base_uid = _q(cfg_a, "SELECT uid FROM articles WHERE id = ?",
                  base["id"])[0]["uid"]
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"

    _ingest(cfg_a, title="From A", url="https://example.com/from-a")
    _ingest(cfg_b, title="From B", url="https://example.com/from-b")
    _exec(cfg_a, "UPDATE articles SET is_read = 1, meta_updated_at = ? "
          "WHERE uid = ?", "2026-07-15T11:00:00Z", base_uid)

    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"

    assert _titles(cfg_a) == {"Base", "From A", "From B"}
    assert _titles(cfg_b) == {"Base", "From A", "From B"}
    assert _art(cfg_a, base_uid)["is_read"] == 1
    assert _art(cfg_b, base_uid)["is_read"] == 1
    _converged(cfg_a, cfg_b)


# --- #4 delete vs concurrent edit — edit wins (resurrection) -----------------


def test_delete_vs_concurrent_edit_edit_wins(rig):
    cfg_a, cfg_b, _backend = rig
    art = _ingest(cfg_a, title="Doomed", url="https://example.com/doomed")
    uid = _q(cfg_a, "SELECT uid FROM articles WHERE id = ?",
             art["id"])[0]["uid"]
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    b_row = _art(cfg_b, uid, "id, markdown_path")
    assert b_row is not None

    # A deletes; B concurrently edits the file EXTERNALLY (the only real
    # body-edit path — bodies have no route).
    delete_article(cfg_a, art["id"])
    path = cfg_b.articles_dir / b_row["markdown_path"]
    post = frontmatter.load(str(path))
    post.content = "# Doomed\n\nEdited on B — the edit must survive."
    path.write_text(frontmatter.dumps(post), encoding="utf-8")

    assert _sync(cfg_a).result == "ok"   # pushes tombstone
    assert _sync(cfg_b).result == "ok"   # pulls tombstone; edit wins; pushes
    assert _sync(cfg_a).result == "ok"   # pulls the resurrecting edit

    for cfg in (cfg_a, cfg_b):
        rows = _q(cfg, "SELECT uid, markdown_path FROM articles WHERE uid = ?",
                  uid)
        assert len(rows) == 1, f"article missing/duplicated in {cfg.library}"
        body = frontmatter.load(
            str(cfg.articles_dir / rows[0]["markdown_path"])).content
        assert "Edited on B" in body
    _converged(cfg_a, cfg_b)


# --- #5 wipe + bootstrap — full restore --------------------------------------


def test_wipe_and_bootstrap_full_restore(rig, tmp_path):
    from tiro.vectorstore import init_vectorstore, retry_pending_vectors

    cfg_a, _cfg_b, _backend = rig
    art = _ingest(cfg_a, title="Full Restore",
                  body="# Hello\n\nSome body text to highlight.\n",
                  url="https://example.com/full-restore")
    row = _q(cfg_a, "SELECT * FROM articles WHERE id = ?", art["id"])[0]
    stem = Path(row["markdown_path"]).stem
    write_note(cfg_a, stem, "Restore-me article note.\n")
    body = frontmatter.load(
        str(cfg_a.articles_dir / row["markdown_path"])).content
    start = body.index("body")
    conn = get_connection(cfg_a.db_path)
    try:
        hl_uid = append_highlight(
            conn=conn, config=cfg_a, article=row, quote="body",
            prefix=body[max(0, start - 8):start],
            suffix=body[start + 4:start + 12],
            position_start=start, position_end=start + 4,
            content_hash=content_hash(body), color="yellow",
            note_markdown="restored highlight note")
        conn.commit()
    finally:
        conn.close()
    assert _sync(cfg_a).result == "ok"  # first cycle -> snapshot exists

    # New device C, fresh library. Its OWN vectorstore is initialized before
    # bootstrap so the module-global collection points at C's chroma dir
    # (not A's) for both materialization and the retry pass.
    cfg_c = _second_library(tmp_path, "lib-c")
    _sync_cfg(cfg_c, cfg_a.sync_path, encrypt="on")
    cfg_c.sync_enabled = True
    identity = asyncio.run(
        verify_passphrase(cfg_c, adapter_for_config(cfg_c), PASSPHRASE))
    assert identity == cfg_a.sync_identity
    cfg_c.sync_identity = identity
    init_vectorstore(cfg_c.chroma_dir, cfg_c.default_embedding_model)

    report = asyncio.run(bootstrap(cfg_c, adapter_for_config(cfg_c)))

    assert report.result == "ok"
    c_row = _q(cfg_c, "SELECT * FROM articles")[0]
    assert c_row["title"] == "Full Restore"
    # Body bytes byte-identical: articles sync as FULL files.
    assert (cfg_c.articles_dir / c_row["markdown_path"]).read_bytes() == \
        (cfg_a.articles_dir / row["markdown_path"]).read_bytes()
    note_path = cfg_c.library / "notes" / f"{stem}.md"
    assert note_path.exists()
    assert note_path.read_text() == "Restore-me article note.\n"
    hls = _q(cfg_c, "SELECT uid, color FROM highlights")
    assert [(h["uid"], h["color"]) for h in hls] == [(hl_uid, "yellow")]
    lines = read_annotations(cfg_c, stem)
    assert [ln["uid"] for ln in lines] == [hl_uid]
    assert lines[0]["note_markdown"] == "restored highlight note"
    assert c_row["vector_status"] in ("pending", "indexed")

    retry_pending_vectors(cfg_c)
    assert _q(cfg_c, "SELECT vector_status FROM articles")[0][
        "vector_status"] == "indexed"


# --- #6 wrong passphrase — clean refusal -------------------------------------


def _backend_files(backend: Path) -> list[str]:
    return sorted(p.relative_to(backend).as_posix()
                  for p in backend.rglob("*") if p.is_file())


def test_wrong_passphrase_clean_refusal(rig, tmp_path):
    cfg_a, _cfg_b, backend = rig
    _ingest(cfg_a, title="Private", url="https://example.com/private")
    assert _sync(cfg_a).result == "ok"

    cfg_x = _second_library(tmp_path, "lib-x")
    _sync_cfg(cfg_x, backend, encrypt="on")
    files_before = _backend_files(backend)

    result = asyncio.run(
        verify_passphrase(cfg_x, adapter_for_config(cfg_x), "totally wrong"))

    assert result is None  # clean refusal, no exception
    assert _count(cfg_x) == 0
    assert _backend_files(backend) == files_before  # X wrote NOTHING


# --- #7 corrupted segment — quarantine, not half-apply -----------------------


def test_corrupted_segment_quarantine_then_recover(rig):
    cfg_a, cfg_b, backend = rig
    _ingest(cfg_a, title="Fine", url="https://example.com/fine")
    assert _sync(cfg_a).result == "ok"   # snapshot epoch; segment 1 GC'd
    assert _sync(cfg_b).result == "ok"   # bootstraps
    assert _titles(cfg_b) == {"Fine"}

    # A's SECOND segment survives GC (snapshot cadence returns False). B's
    # own bootstrap cycle may have pushed its documented one-cycle echo
    # segment too — corrupt specifically the segment B still has to PULL:
    # A's.
    from tiro.sync.engine import get_or_create_device

    device_a, _ = get_or_create_device(cfg_a)
    _ingest(cfg_a, title="Second", url="https://example.com/second")
    assert _sync(cfg_a).result == "ok"
    segments = list(backend.glob(f"journal/{device_a}/*.age"))
    assert len(segments) == 1
    seg = segments[0]
    original = seg.read_bytes()
    seg.write_bytes(bytes(b ^ 0xFF for b in original[:8]) + original[8:])
    wm_before = read_sync_state(cfg_b)["watermarks"]

    report = _sync(cfg_b)

    assert report.result == "needs_attention"
    assert _count(cfg_b) == 1                      # nothing half-applied
    assert read_sync_state(cfg_b)["watermarks"] == wm_before
    assert load_sync_status(cfg_b)["dot"] == "warn"

    # REPAIR the corruption honestly: restore the bytes — quarantine was
    # recoverable, not sticky.
    seg.write_bytes(original)
    assert _sync(cfg_b).result == "ok"
    assert _titles(cfg_b) == {"Fine", "Second"}
    _converged(cfg_a, cfg_b)


# --- #8 mass-delete guard trips end-to-end -----------------------------------


def test_mass_delete_guard_end_to_end(rig):
    cfg_a, cfg_b, _backend = rig
    arts = [_ingest(cfg_a, title=f"Bulk {i:02d}",
                    url=f"https://example.com/bulk-{i}") for i in range(12)]
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    assert _count(cfg_b) == 12

    for art in arts:
        delete_article(cfg_a, art["id"])
    assert _count(cfg_a) == 0
    assert _sync(cfg_a).result == "ok"

    report = _sync(cfg_b)

    assert report.result == "needs_attention"
    assert report.reason == "mass_delete_guard"
    assert report.guard  # the human-readable guard message rides along
    assert _count(cfg_b) == 12  # nothing applied

    report2 = _sync(cfg_b, accept_mass_delete=True)

    assert report2.result == "ok"
    assert report2.guard is None  # one-shot acceptance consumed cleanly
    assert _count(cfg_b) == 0
    _converged(cfg_a, cfg_b)

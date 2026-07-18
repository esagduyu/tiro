"""Sync S6.1: corrupted-snapshot recovery drills — bootstrap must fall
back newest->oldest across backend snapshots, and refuse CLEANLY (plain
result="error" pointing at `tiro sync repair`, library untouched) when
snapshots exist but none is usable.

These drills are the machine-verifiable half of the Phase 7a acceptance
story; the physical two-laptops-plus-a-phone matrix is OWNER-ONLY and is
never simulated here.
"""
import asyncio

import pytest

from tests.test_reconcile import _ingest
from tests.test_sync_setup_flows import (
    WEAK_KDF,
    _count,
    _second_library,
    _sync_cfg,
    _titles,
)
from tiro.sync import engine
from tiro.sync.engine import (
    adapter_for_config,
    bootstrap,
    codec_for_config,
    get_or_create_device,
    init_backend,
    read_sync_state,
    sync_cycle,
    verify_passphrase,
)

PASSPHRASE = "drill-passphrase"

CORRUPTION = b"\x00corrupted beyond hope"


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    """The S1 two-poll settle sleep is pure wall-clock cost here — every
    file in these drills is complete before any cycle runs."""
    import tiro.sync.reconcile as rec

    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)


def _sync(cfg, **kw):
    return asyncio.run(sync_cycle(cfg, **kw))


@pytest.fixture
def seeded_backend(tmp_path, initialized_library):
    """(cfg_a, backend, manifests): device A + an encrypted backend holding
    TWO snapshots and one tail journal segment.

    Snapshot #1 (articles Alpha+Beta) is auto-created by A's first cycle,
    which also GCs its own segment; cycle 2 pushes a tail segment carrying
    Gamma (should_snapshot thresholds not met — no second snapshot).
    Snapshot #2 (all three articles) is then uploaded DIRECTLY via
    engine._upload_local_snapshot — a forced _compact would GC snapshot #1
    (plan_gc: latest-dominates), and these drills need both on the backend.
    `manifests` is ULID-sorted: manifests[-1] is the newest.
    """
    backend = tmp_path / "backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="on")
    cfg_a.sync_enabled = True
    recovery = asyncio.run(
        init_backend(cfg_a, adapter_for_config(cfg_a), PASSPHRASE,
                     kdf_params=WEAK_KDF))
    cfg_a.sync_identity = recovery

    _ingest(cfg_a, title="Alpha", body="# Alpha\n\nBody one.",
            url="https://example.com/alpha")
    _ingest(cfg_a, title="Beta", body="# Beta\n\nBody two.",
            url="https://example.com/beta")
    report = _sync(cfg_a)  # pushes seq 1, auto-snapshots, GCs segment 1
    assert report.result == "ok"
    assert len(list(backend.glob("snapshots/*/manifest.age"))) == 1

    _ingest(cfg_a, title="Gamma", body="# Gamma\n\nBody three.",
            url="https://example.com/gamma")
    report = _sync(cfg_a)  # pushes the tail segment; no second snapshot
    assert report.result == "ok"

    device_id, _name = get_or_create_device(cfg_a)
    state = read_sync_state(cfg_a)
    covers = {device_id: (state["self"] or {}).get("last_seq") or 0,
              **state["watermarks"]}
    asyncio.run(engine._upload_local_snapshot(
        cfg_a, adapter_for_config(cfg_a), codec_for_config(cfg_a),
        device_id, covers))

    manifests = sorted(backend.glob("snapshots/*/manifest.age"))
    assert len(manifests) == 2  # ULID-sorted: [-1] is snapshot #2
    return cfg_a, backend, manifests


def _join(tmp_path, backend, name="lib-joiner"):
    """Fresh empty joining library pointed at the backend, passphrase
    verified (the multidevice-suite recipe)."""
    cfg = _second_library(tmp_path, name)
    _sync_cfg(cfg, backend, encrypt="on")
    cfg.sync_enabled = True
    identity = asyncio.run(
        verify_passphrase(cfg, adapter_for_config(cfg), PASSPHRASE))
    cfg.sync_identity = identity
    return cfg


def test_bootstrap_falls_back_to_older_snapshot(tmp_path, seeded_backend):
    """Newest manifest corrupted -> bootstrap silently (well, warned-ly)
    falls back to the older snapshot, and the pull folds the tail journal
    segment in: the joining device still converges to all three articles."""
    _cfg_a, backend, manifests = seeded_backend
    manifests[-1].write_bytes(CORRUPTION)

    cfg_c = _join(tmp_path, backend)
    report = asyncio.run(bootstrap(cfg_c, adapter_for_config(cfg_c)))

    assert report.result == "ok"
    assert _titles(cfg_c) == {"Alpha", "Beta", "Gamma"}
    assert any("unusable" in w for w in report.warnings)


def test_bootstrap_all_snapshots_corrupt_refuses_cleanly(
        tmp_path, seeded_backend):
    """Every manifest corrupted -> a plain error whose reason points at
    `tiro sync repair`, and the joining library is left EMPTY — no
    half-materialization (rows or files)."""
    _cfg_a, backend, manifests = seeded_backend
    for manifest in manifests:
        manifest.write_bytes(CORRUPTION)

    cfg_c = _join(tmp_path, backend)
    report = asyncio.run(bootstrap(cfg_c, adapter_for_config(cfg_c)))

    assert report.result == "error"
    assert "repair" in report.reason.lower()
    assert _count(cfg_c) == 0
    assert list(cfg_c.articles_dir.glob("*.md")) == []


def test_bootstrap_missing_shared_object_refuses_cleanly(
        tmp_path, seeded_backend):
    """Every object gone (the clean deterministic form of "referenced
    object missing" for every snapshot) -> both candidates fail the
    fetch-everything phase, same clean refusal, library untouched."""
    _cfg_a, backend, _manifests = seeded_backend
    objects = list(backend.glob("objects/*/*.age"))
    assert objects
    for obj in objects:
        obj.unlink()

    cfg_c = _join(tmp_path, backend)
    report = asyncio.run(bootstrap(cfg_c, adapter_for_config(cfg_c)))

    assert report.result == "error"
    assert "repair" in report.reason.lower()
    assert _count(cfg_c) == 0
    assert list(cfg_c.articles_dir.glob("*.md")) == []


def test_bootstrap_falls_back_on_parse_failure(tmp_path, seeded_backend):
    """Decision #1(a)'s PARSE-failure leg, pinned distinctly from the
    decrypt-failure leg: the newest manifest DECRYPTS fine (real codec)
    but is not a snapshot doc -> parse_snapshot raises -> bootstrap falls
    back to the older snapshot and the tail pull still converges to all
    three titles."""
    from tiro.sync.snapshot import encode_snapshot

    cfg_a, backend, manifests = seeded_backend
    codec = codec_for_config(cfg_a)
    manifests[-1].write_bytes(
        encode_snapshot("this is not a snapshot doc", codec))

    cfg_c = _join(tmp_path, backend)
    report = asyncio.run(bootstrap(cfg_c, adapter_for_config(cfg_c)))

    assert report.result == "ok"
    assert _titles(cfg_c) == {"Alpha", "Beta", "Gamma"}
    assert any("unusable" in w for w in report.warnings)


def test_fallback_bootstrap_with_truncated_journal_refuses(
        tmp_path, seeded_backend):
    """S6.1-fix review Major 1, field case (a): newest manifest corrupt AND
    every journal segment beyond the OLDER snapshot's covers fully GC'd.
    The fallback materializes the older snapshot, but the pull must then
    detect that A's device doc reports a journal head the listing cannot
    reach — needs_attention (truncated/GC'd + repair pointer), NEVER a
    silently-stale "ok"."""
    from tiro.sync.snapshot import decode_snapshot, parse_journal_key

    cfg_a, backend, manifests = seeded_backend
    codec = codec_for_config(cfg_a)
    older_covers = decode_snapshot(manifests[0].read_bytes(), codec).covers
    manifests[-1].write_bytes(CORRUPTION)
    deleted = 0
    for seg in backend.glob("journal/*/*.age"):
        dev, seq = parse_journal_key(seg.relative_to(backend).as_posix())
        if seq > older_covers.get(dev, 0):
            seg.unlink()
            deleted += 1
    assert deleted  # the Gamma tail segment was present, and is now gone

    cfg_c = _join(tmp_path, backend)
    report = asyncio.run(bootstrap(cfg_c, adapter_for_config(cfg_c)))

    assert report.result == "needs_attention"
    assert "truncated" in report.reason
    assert "GC'd" in report.reason
    assert "repair" in report.reason
    # Honest partial: the older snapshot's two articles DID materialize;
    # what must never happen is an "ok" that hides the missing tail.
    assert _titles(cfg_c) == {"Alpha", "Beta"}


def test_steady_state_truncated_segment_refuses(tmp_path, seeded_backend):
    """S6.1-fix review Major 1, field case (b): two converged devices; A
    pushes a new segment B has not pulled; that segment file is then lost
    from the backend while A's device doc still reports it. B's next cycle
    must be needs_attention (truncated), not an ok that silently strands B
    behind A's journal head forever."""
    from tiro.sync.snapshot import journal_key

    cfg_a, backend, _manifests = seeded_backend
    cfg_b = _join(tmp_path, backend, name="lib-b")
    report = asyncio.run(bootstrap(cfg_b, adapter_for_config(cfg_b)))
    assert report.result == "ok"
    assert _titles(cfg_b) == {"Alpha", "Beta", "Gamma"}

    _ingest(cfg_a, title="Delta", body="# Delta\n\nBody four.",
            url="https://example.com/delta")
    report = _sync(cfg_a)
    assert report.result == "ok"
    dev_a, _name = get_or_create_device(cfg_a)
    head = (read_sync_state(cfg_a)["self"] or {}).get("last_seq") or 0
    seg = backend / journal_key(dev_a, head)
    assert seg.exists()
    seg.unlink()

    report = _sync(cfg_b)
    assert report.result == "needs_attention"
    assert "truncated" in report.reason
    assert "repair" in report.reason
    assert "Delta" not in _titles(cfg_b)


def test_bootstrap_tail_segment_object_missing_quarantines_after_snapshot(
        tmp_path, seeded_backend):
    """Honest-partial pin: delete exactly the object(s) snapshot #2 needs
    beyond snapshot #1 — i.e. Gamma's body, which the tail journal segment
    references by the same content address. Snapshot #2 then fails its
    fetch phase (fallback), snapshot #1 materializes fully, and the PULL
    quarantines on the tail segment's missing object: needs_attention with
    exactly the older snapshot's two articles applied — the same honest
    incremental semantics as a normal cycle quarantine."""
    from tiro.sync.snapshot import decode_snapshot, object_key

    cfg_a, backend, manifests = seeded_backend
    codec = codec_for_config(cfg_a)
    older_doc = decode_snapshot(manifests[0].read_bytes(), codec)
    newest_doc = decode_snapshot(manifests[-1].read_bytes(), codec)
    tail_addrs = (set(newest_doc.objects.values())
                  - set(older_doc.objects.values()))
    assert tail_addrs  # Gamma's body object, content-addressed
    for addr in tail_addrs:
        (backend / object_key(addr)).unlink()

    cfg_c = _join(tmp_path, backend)
    report = asyncio.run(bootstrap(cfg_c, adapter_for_config(cfg_c)))

    assert report.result == "needs_attention"
    assert _titles(cfg_c) == {"Alpha", "Beta"}
    assert any("unusable" in w for w in report.warnings)

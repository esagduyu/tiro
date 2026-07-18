"""Sync S6.1: corrupted-snapshot recovery drills — bootstrap must fall
back newest->oldest across backend snapshots, and refuse CLEANLY (plain
result="error" pointing at `tiro sync repair`, library untouched) when
snapshots exist but none is usable.

These drills are the machine-verifiable half of the Phase 7a acceptance
story; the physical two-laptops-plus-a-phone matrix is OWNER-ONLY and is
never simulated here.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import frontmatter
import pytest

from tests.test_reconcile import _ingest
from tests.test_sync_cli import Args
from tests.test_sync_multidevice import _q
from tests.test_sync_setup_flows import (
    WEAK_KDF,
    _count,
    _second_library,
    _sync_cfg,
    _titles,
)
from tiro.anchors import content_hash
from tiro.annotations import append_highlight, read_annotations, sidecar_stem
from tiro.cli import cmd_sync
from tiro.database import get_connection
from tiro.doctor import fix as doctor_fix
from tiro.doctor import scan as doctor_scan
from tiro.lifecycle import delete_article
from tiro.sync import engine
from tiro.sync.adapters.base import LOCK_KEY, make_lock_payload
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


# --- S6.2: stale-lock drills + doctor's sync section -------------------------


STALE_DEVICE = "01STALEDEVICEELSEWHERE0000"


def _write_lock(backend, *, age_s, ttl_s=120, device_id=STALE_DEVICE):
    """Write a real-shape lock payload (make_lock_payload's exact field
    names, tz-aware UTC) aged `age_s` seconds into the past."""
    path = backend / LOCK_KEY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_lock_payload(
        device_id, ttl_s,
        now=datetime.now(UTC) - timedelta(seconds=age_s)))
    return path


@pytest.fixture
def plain_synced(tmp_path, initialized_library):
    """(cfg, backend): plaintext filesystem backend + one ingested article,
    sync enabled, NO cycle run yet (the first cycle auto-inits format.json)."""
    backend = tmp_path / "lock-backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    cfg.sync_enabled = True
    _ingest(cfg, title="Locked Out", body="# Locked\n\nBody.",
            url="https://example.com/locked")
    return cfg, backend


def test_stale_lock_is_stolen_by_next_cycle(plain_synced):
    """FROZEN spec §6.1 behavior pinned at the ENGINE level: a long-dead
    lock (age 3600s, ttl 120s) never wedges sync — the next cycle steals it,
    completes, and pushes."""
    cfg, backend = plain_synced
    _write_lock(backend, age_s=3600, ttl_s=120)

    report = _sync(cfg)

    assert report.result == "ok"
    assert report.pushed_ops > 0
    # The stolen lock was re-minted as ours and released on cycle exit.
    assert not (backend / LOCK_KEY).exists()


def test_live_lock_skips_cycle(plain_synced):
    """A FRESH foreign lock (age 5s < ttl 120s) is honored: the cycle skips
    and the lock file is left exactly as it was."""
    from tiro.sync.adapters.base import lock_owner

    cfg, backend = plain_synced
    lock_path = _write_lock(backend, age_s=5, ttl_s=120)

    report = _sync(cfg)

    assert report.result == "skipped_lock"
    assert lock_path.exists()
    assert lock_owner(lock_path.read_bytes()) == STALE_DEVICE


def test_doctor_reports_and_fixes_stale_lock(plain_synced):
    """Doctor's sync section reports a stale lock; --fix clears ONLY a
    stale one — a live lock survives fix untouched."""
    cfg, backend = plain_synced
    lock_path = _write_lock(backend, age_s=3600, ttl_s=120)

    report = doctor_scan(cfg)
    assert report["sync"]["configured"] is True
    assert report["sync"]["backend"] == "ok"
    assert report["sync"]["stale_lock"] is True

    fixed = doctor_fix(cfg)
    assert fixed["sync_stale_lock_cleared"] is True
    assert not lock_path.exists()

    # A LIVE lock is NEVER deleted by --fix.
    lock_path = _write_lock(backend, age_s=5, ttl_s=120)
    fixed = doctor_fix(cfg)
    assert fixed["sync_stale_lock_cleared"] is False
    assert lock_path.exists()


def test_doctor_sync_section_offline_safe(tmp_path, initialized_library):
    """The sync section can NEVER break doctor: an unconfigured library
    reports configured=False (no probe at all), and a configured-but-absent
    backend degrades gracefully — scan() never raises. (The filesystem
    adapter treats a missing dir as an empty listing, so a nonexistent
    sync_path legitimately reports "ok"; a network backend would report
    "unreachable" — both are acceptable shapes here.)"""
    report = doctor_scan(initialized_library)
    assert report["sync"]["configured"] is False
    assert report["sync"]["backend"] is None
    assert report["sync"]["stale_lock"] is False

    cfg = _sync_cfg(initialized_library, tmp_path / "nope" / "gone",
                    encrypt="off")
    cfg.sync_enabled = True
    report = doctor_scan(cfg)
    assert report["sync"]["configured"] is True
    assert report["sync"]["backend"] in ("ok", "unreachable")
    assert report["sync"]["stale_lock"] is False


def test_doctor_exit_code_neutral_on_sync_findings(plain_synced):
    """FROZEN: sync findings are operational states, not library-structural
    inconsistencies — a stale lock changes NEITHER structurally_consistent
    NOR clean (compared with and without the lock present)."""
    cfg, backend = plain_synced

    baseline = doctor_scan(cfg)
    assert baseline["structurally_consistent"] is True
    # S6.2-fix Nit 7: pin the baseline CLEAN outright — the fixture is a
    # fresh library with one normal ingest (no housekeeping residue), so
    # clean must be True; without this pin the equality below could pass
    # vacuously with clean=False on both sides.
    assert baseline["clean"] is True

    _write_lock(backend, age_s=3600, ttl_s=120)
    with_lock = doctor_scan(cfg)

    assert with_lock["sync"]["stale_lock"] is True
    assert with_lock["structurally_consistent"] is True
    assert with_lock["clean"] is True
    assert with_lock["clean"] == baseline["clean"]


def _sync_state_rows(cfg):
    from tiro.database import get_connection

    conn = get_connection(cfg.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM sync_state").fetchone()["n"]
    finally:
        conn.close()


def test_doctor_scan_never_mints_device_identity(plain_synced):
    """S6.2-fix review Major 1: scan() is a pure status probe — on a
    configured+enabled but NEVER-cycled library it must not INSERT the
    is_self=1 device-identity row into sync_state as a side effect of
    building its backend adapter."""
    cfg, _backend = plain_synced
    assert _sync_state_rows(cfg) == 0  # never cycled: no identity yet

    report = doctor_scan(cfg)

    assert report["sync"]["backend"] == "ok"  # the probe really ran
    assert _sync_state_rows(cfg) == 0  # ...and minted nothing


def test_clear_stale_lock_live_vs_stale_direct(plain_synced):
    """clear_stale_lock's own re-read/re-check, pinned directly (S6.2-fix
    Minor 4): a LIVE lock returns False and survives byte-intact; a stale
    one returns True and is deleted. The explicit device_id also pins the
    read-only adapter path (no identity mint)."""
    cfg, backend = plain_synced
    adapter = adapter_for_config(cfg, device_id="test-probe")
    try:
        lock_path = _write_lock(backend, age_s=5, ttl_s=120)
        live_bytes = lock_path.read_bytes()
        assert asyncio.run(engine.clear_stale_lock(cfg, adapter)) is False
        assert lock_path.exists()
        assert lock_path.read_bytes() == live_bytes

        lock_path = _write_lock(backend, age_s=3600, ttl_s=120)
        assert asyncio.run(engine.clear_stale_lock(cfg, adapter)) is True
        assert not lock_path.exists()
    finally:
        asyncio.run(adapter.aclose())
    assert _sync_state_rows(cfg) == 0


def test_doctor_probe_network_error_reports_unreachable(
        plain_synced, monkeypatch):
    """S6.2-fix Minor 3: a backend whose read path raises (network down)
    degrades the section to backend="unreachable" — scan() never raises
    and never claims a stale lock it could not read."""
    cfg, _backend = plain_synced

    async def _down(self, key):
        raise ConnectionError("network down")

    monkeypatch.setattr(engine.AuditedAdapter, "get", _down)

    report = doctor_scan(cfg)

    assert report["sync"]["configured"] is True
    assert report["sync"]["backend"] == "unreachable"
    assert report["sync"]["stale_lock"] is False


def test_doctor_sync_section_degrades_on_internal_error(
        plain_synced, monkeypatch, capsys):
    """S6.2-fix Minor 3, belt branch: a bug anywhere in the section builder
    (here: load_sync_status raising) degrades report["sync"] to the
    {"configured", "error"} shape while scan() still returns the FULL
    report — and cmd_doctor's text output prints that shape instead of
    crashing on missing keys."""
    from types import SimpleNamespace

    from tiro import cli

    cfg, _backend = plain_synced

    def _boom(config):
        raise RuntimeError("sync status exploded")

    monkeypatch.setattr(engine, "load_sync_status", _boom)

    report = doctor_scan(cfg)
    assert set(report["sync"]) == {"configured", "error"}
    assert report["sync"]["configured"] is True
    assert "sync status exploded" in report["sync"]["error"]
    # The rest of the report survived the sync-section failure.
    assert report["structurally_consistent"] is True
    assert report["clean"] is True

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(SimpleNamespace(config="unused", fix=False, json=False,
                                       _config_override=cfg))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "sync: status unavailable" in out
    assert "sync status exploded" in out


def test_doctor_probe_misconfigured_distinct_from_unreachable(
        plain_synced, capsys):
    """S6.2-fix Nit 6: SyncConfigError means the local config failed
    validation BEFORE any backend contact — reported as "misconfigured",
    never mislabeled "unreachable" — and cmd_doctor prints a matching
    pointer at the sync settings."""
    from types import SimpleNamespace

    from tiro import cli

    cfg, _backend = plain_synced
    cfg.sync_encrypt = "bogus"  # resolve_encryption refuses unknown values

    report = doctor_scan(cfg)
    assert report["sync"]["configured"] is True
    assert report["sync"]["backend"] == "misconfigured"
    assert report["sync"]["stale_lock"] is False

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(SimpleNamespace(config="unused", fix=False, json=False,
                                       _config_override=cfg))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "sync: backend misconfigured" in out


def test_cli_doctor_fix_json_carries_sync_lock_flag(plain_synced, capsys):
    """S6.2-fix Nit 5: `tiro doctor --fix --json` merges the fix-only
    sync_stale_lock_cleared flag into the printed post-fix report (scan()
    alone never produces that key)."""
    import json as _json
    from types import SimpleNamespace

    from tiro import cli

    cfg, backend = plain_synced
    lock_path = _write_lock(backend, age_s=3600, ttl_s=120)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(SimpleNamespace(config="unused", fix=True, json=True,
                                       _config_override=cfg))
    assert exc.value.code == 0
    parsed = _json.loads(capsys.readouterr().out)
    assert parsed["sync_stale_lock_cleared"] is True
    assert not lock_path.exists()


def test_cli_doctor_prints_cycle_warnings(plain_synced, monkeypatch, capsys):
    """S6.2-fix Nit 7: last-cycle warnings surfaced by the sync section are
    PRINTED by cmd_doctor (report-only, muted "sync:" prefix) — S6's later
    engine warnings will ride the same lines."""
    from types import SimpleNamespace

    from tiro import cli

    cfg, _backend = plain_synced

    def _status(config):
        return {"configured": True, "enabled": True, "dot": "ok",
                "last_cycle": {"result": "ok",
                               "warnings": ["snapshot 01FAKE unusable"]},
                "last_synced_at": None, "device_name": None, "devices": []}

    monkeypatch.setattr(engine, "load_sync_status", _status)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(SimpleNamespace(config="unused", fix=False, json=False,
                                       _config_override=cfg))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "sync: last-cycle warning — snapshot 01FAKE unusable" in out


# --- S6.3: clock-skew warnings (>24h, spec §10 / D-S6-1) ---------------------


def _skewed_hlc_factory(offset_ms):
    """engine.HLCClock stand-in whose wall clock runs offset_ms off the real
    one — the honest seam: HLCClock already accepts a now_ms time source, so
    no journal-side patching is needed."""
    import time

    from tiro.sync.journal import HLCClock

    def _factory(device):
        return HLCClock(
            device, now_ms=lambda: time.time_ns() // 1_000_000 + offset_ms)

    return _factory


def _skews(warnings):
    return [w for w in warnings if w.startswith("clock skew:")]


def test_clock_skew_constant_frozen():
    """FROZEN: the >24h threshold is spec §10 — never tune it casually."""
    assert engine.CLOCK_SKEW_WARN_HOURS == 24


def test_status_live_ahead_check(initialized_library):
    """Prong B unit: a registry row whose last_wall_ms sits ~30h in the
    future warns (naming the device); ~1h ahead stays silent."""
    import time

    from tiro.sync.engine import load_sync_status, upsert_remote_device

    cfg = initialized_library
    get_or_create_device(cfg)
    now_ms = time.time_ns() // 1_000_000

    upsert_remote_device(cfg, "01SKEWFASTLAPTOP0000000000",
                         name="fast-laptop",
                         last_wall_ms=now_ms + 30 * 3600 * 1000)
    warnings = load_sync_status(cfg)["warnings"]
    assert len(warnings) == 1
    assert "fast-laptop" in warnings[0]
    assert "clock" in warnings[0]
    assert "ahead" in warnings[0]

    # ~1h of skew is normal life (timezones don't matter — HLC wall stamps
    # are UTC epoch ms — but drift/suspend jitter must never warn).
    upsert_remote_device(cfg, "01SKEWFASTLAPTOP0000000000",
                         last_wall_ms=now_ms + 1 * 3600 * 1000)
    assert load_sync_status(cfg)["warnings"] == []


@pytest.fixture
def skew_rig(tmp_path, initialized_library):
    """(cfg_a, cfg_b, backend): plaintext two-device rig. A pushed one
    normal-clock cycle (auto-snapshot on first cycle covers it); B joined
    via the empty-library auto-bootstrap and completed one SUCCESSFUL
    cycle — the prev-success baseline the behind-check needs."""
    backend = tmp_path / "skew-backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="off")
    cfg_a.sync_enabled = True
    _ingest(cfg_a, title="Alpha", body="# Alpha\n\nBody one.",
            url="https://example.com/alpha")
    assert _sync(cfg_a).result == "ok"

    cfg_b = _second_library(tmp_path, "lib-skew-b")
    _sync_cfg(cfg_b, backend, encrypt="off")
    cfg_b.sync_enabled = True
    report = _sync(cfg_b)
    assert report.result == "ok"
    assert _titles(cfg_b) == {"Alpha"}
    return cfg_a, cfg_b, backend


def test_pull_warns_on_remote_clock_ahead(skew_rig, monkeypatch):
    """Prong A ahead e2e: A pushes ops stamped ~30h in the future; B's next
    pull warns (one line, naming A), the warning persists into
    load_sync_status via last_cycle AND the live registry check — deduped
    to a single line — and doctor's sync section carries it report-only."""
    from tiro.sync.engine import load_sync_status

    cfg_a, cfg_b, _backend = skew_rig
    _, name_a = get_or_create_device(cfg_a)

    _ingest(cfg_a, title="Beta", body="# Beta\n\nBody two.",
            url="https://example.com/beta")
    with monkeypatch.context() as m:
        m.setattr(engine, "HLCClock",
                  _skewed_hlc_factory(30 * 3600 * 1000))
        assert _sync(cfg_a).result == "ok"

    report = _sync(cfg_b)
    assert report.result == "ok"
    skews = _skews(report.warnings)
    assert len(skews) == 1
    assert name_a in skews[0]
    assert "ahead" in skews[0]

    status = load_sync_status(cfg_b)
    skews = _skews(status["warnings"])
    assert len(skews) == 1  # prong A + prong B deduped by device label
    assert name_a in skews[0]
    assert "ahead" in skews[0]

    # Doctor's sync section carries it (report-only, like the rest of the
    # section — exit-code neutrality is pinned by the S6.2 drills; the
    # structural keys aren't asserted here because the joined library
    # shares the process-global test vectorstore with A's).
    scan = doctor_scan(cfg_b)
    skews = _skews(scan["sync"]["clock_skew"])
    assert len(skews) == 1
    assert name_a in skews[0]


def test_pull_warns_on_remote_clock_behind(skew_rig, tmp_path, monkeypatch):
    """Prong A behind e2e: B has a previous SUCCESSFUL cycle, so a segment
    new to this pull whose newest stamp predates that cycle by >24h proves
    the remote clock is behind. A fresh device C joining afterwards
    (bootstrap/catch-up, no previous success) pulls the SAME old-stamped
    ops with NO behind warning — offline-all-weekend can never false-fire."""
    from tiro.sync.engine import load_sync_status

    cfg_a, cfg_b, backend = skew_rig
    _, name_a = get_or_create_device(cfg_a)

    # Drain B's journal into A first (a normal-clock cycle): each cycle's
    # fresh HLC clock observes every pulled stamp and tick() never
    # regresses, so a backdated wall clock only actually mints old stamps
    # when the pull observed nothing newer — which is exactly the dangerous
    # LWW case this warning exists for (a behind device writing without
    # having seen others' recent ops).
    assert _sync(cfg_a).result == "ok"

    _ingest(cfg_a, title="Gamma", body="# Gamma\n\nBody three.",
            url="https://example.com/gamma")
    with monkeypatch.context() as m:
        m.setattr(engine, "HLCClock",
                  _skewed_hlc_factory(-30 * 3600 * 1000))
        assert _sync(cfg_a).result == "ok"

    report = _sync(cfg_b)
    assert report.result == "ok"
    skews = _skews(report.warnings)
    assert len(skews) == 1
    assert name_a in skews[0]
    assert "behind" in skews[0]
    # A behind clock leaves no future last_wall_ms, so status carries the
    # persisted cycle warning (folded, not the live prong-B check).
    assert _skews(load_sync_status(cfg_b)["warnings"]) == skews
    assert doctor_scan(cfg_b)["sync"]["clock_skew"] == skews

    cfg_c = _second_library(tmp_path, "lib-skew-c")
    _sync_cfg(cfg_c, backend, encrypt="off")
    cfg_c.sync_enabled = True
    report_c = _sync(cfg_c)
    assert report_c.result == "ok"
    assert _titles(cfg_c) >= {"Alpha", "Gamma"}
    assert _skews(report_c.warnings) == []


def test_skew_no_double_report_across_surfaces(initialized_library):
    """S6.3 review #1: a persisted prong-A line must appear ONCE per
    surface — doctor carries it under clock_skew and EXCLUDES it from
    cycle_warnings; non-skew cycle warnings stay in cycle_warnings only."""
    from tiro.doctor import scan
    from tiro.sync.engine import CycleReport, _record_cycle, load_sync_status

    cfg = initialized_library
    cfg.sync_path = str(cfg.library / "unused-backend")  # configured=True
    get_or_create_device(cfg)
    report = CycleReport()
    report.result = "ok"
    report.finished_at = "2026-07-17T00:00:00Z"
    skew_line = ("clock skew: device 'fast-laptop' is ~30h ahead of this "
                 "device's clock — last-write-wins merges may misorder "
                 "until fixed")
    report.warnings = [skew_line, "compaction skipped: transient"]
    _record_cycle(cfg, report)

    status = load_sync_status(cfg)
    assert status["warnings"].count(skew_line) == 1

    section = scan(cfg)["sync"]
    assert skew_line in section["clock_skew"]
    assert skew_line not in section["cycle_warnings"]
    assert "compaction skipped: transient" in section["cycle_warnings"]


def test_skew_shared_name_never_suppresses(initialized_library):
    """S6.3 review #3: two devices sharing a NAME must not dedupe each
    other's warnings — a live-ahead line for one 'MacBook' plus a persisted
    line for the other 'MacBook' both survive."""
    import time

    from tiro.sync.engine import (
        CycleReport,
        _record_cycle,
        load_sync_status,
        upsert_remote_device,
    )

    cfg = initialized_library
    get_or_create_device(cfg)
    now_ms = time.time_ns() // 1_000_000
    upsert_remote_device(cfg, "01SKEWTWINAAAAAAAAAAAAAAAA", name="MacBook",
                         last_wall_ms=now_ms + 30 * 3600 * 1000)
    upsert_remote_device(cfg, "01SKEWTWINBBBBBBBBBBBBBBBB", name="MacBook",
                         last_wall_ms=now_ms)
    report = CycleReport()
    report.result = "ok"
    report.finished_at = "2026-07-17T00:00:00Z"
    persisted = ("clock skew: device 'MacBook' is ~30h behind this "
                 "device's clock — last-write-wins merges may misorder "
                 "until fixed")
    report.warnings = [persisted]
    _record_cycle(cfg, report)

    warnings = load_sync_status(cfg)["warnings"]
    # One live ahead line + the persisted behind line: both present.
    assert len(warnings) == 2
    assert any("ahead" in w for w in warnings)
    assert persisted in warnings


def test_status_survives_garbage_last_wall_ms(initialized_library):
    """S6.3 review #4: one hand-edited garbage last_wall_ms row degrades to
    a skipped row, not a lost prong-B pass for every other device."""
    import time

    from tiro.database import get_connection
    from tiro.sync.engine import load_sync_status, upsert_remote_device

    cfg = initialized_library
    get_or_create_device(cfg)
    now_ms = time.time_ns() // 1_000_000
    upsert_remote_device(cfg, "01SKEWGARBAGEROW0000000000", name="broken")
    conn = get_connection(cfg.db_path)
    try:
        conn.execute(
            "UPDATE sync_state SET last_wall_ms = 'not-a-number' "
            "WHERE device_id = ?", ("01SKEWGARBAGEROW0000000000",))
        conn.commit()
    finally:
        conn.close()
    upsert_remote_device(cfg, "01SKEWHEALTHYROW0000000000", name="healthy",
                         last_wall_ms=now_ms + 30 * 3600 * 1000)

    warnings = load_sync_status(cfg)["warnings"]
    assert len(warnings) == 1
    assert "healthy" in warnings[0]


# --- S6.4: mass-delete guard e2e extras --------------------------------------
#
# S5's multidevice scenario 8 pins the basic article-guard trip + acceptance;
# these drills add what it did NOT: trip persistence across cycles, the real
# CLI acceptance path, and the annotations-guard equivalent (spec §4). The
# one-shot-across-segments semantic is already pinned at the pull-unit level
# (test_sync_engine.py::test_pull_mass_delete_acceptance_is_one_shot with
# hand-seeded segments); drill 4 re-pins it END-TO-END across the two guard
# KINDS (article RowDels then highlight LineDels, both from real device
# cycles' diffs).


GUARD_WORDS = ["alpha", "bravo", "charlie", "delta", "echo"]

MARKED_BODY = "# Marked\n\nalpha bravo charlie delta echo close the loop.\n"


@pytest.fixture
def guard_rig(tmp_path, initialized_library):
    """(cfg_a, cfg_b): encrypted two-device rig, no data yet — A initialized
    the backend, B passphrase-joined but has not cycled (its first _sync
    auto-bootstraps once A's first cycle has made a snapshot)."""
    backend = tmp_path / "guard-backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="on")
    cfg_a.sync_enabled = True
    recovery = asyncio.run(
        init_backend(cfg_a, adapter_for_config(cfg_a), PASSPHRASE,
                     kdf_params=WEAK_KDF))
    cfg_a.sync_identity = recovery
    cfg_b = _join(tmp_path, backend, name="lib-guard-b")
    return cfg_a, cfg_b


def _hl_count(cfg):
    return _q(cfg, "SELECT COUNT(*) AS n FROM highlights")[0]["n"]


def _highlight_words(cfg, article_id, words):
    """Create one highlight per word via the real M2.1 sidecar-first path,
    positions computed against the WRITTEN file body (multidevice #5's
    recipe). No note_markdown — a synced delete preserves non-empty notes as
    conflict files, which would blur these drills' sidecar assertions."""
    row = _q(cfg, "SELECT * FROM articles WHERE id = ?", article_id)[0]
    body = frontmatter.load(
        str(cfg.articles_dir / row["markdown_path"])).content
    conn = get_connection(cfg.db_path)
    try:
        for word in words:
            start = body.index(word)
            append_highlight(
                conn=conn, config=cfg, article=row, quote=word,
                prefix=body[max(0, start - 8):start],
                suffix=body[start + len(word):start + len(word) + 8],
                position_start=start, position_end=start + len(word),
                content_hash=content_hash(body), color="yellow")
        conn.commit()
    finally:
        conn.close()


def _wipe_annotation_sidecars(cfg, expected: int):
    """Empty every annotations sidecar IN PLACE. Files stay PRESENT, so the
    S1 annotations mass-delete guard (which fires on a MISSING dir / missing
    files for >1 stem) does not trip the wiping device's own reconcile:
    files-win empties its rows and the manifest diff pushes one LineDel per
    vanished line. (Deleting the files instead would wedge the wiping device
    behind its own S1 guard — this is the least-contrived honest route to a
    mass highlight-delete diff.)"""
    sidecars = sorted((cfg.library / "annotations").glob("*.jsonl"))
    assert len(sidecars) == expected
    for path in sidecars:
        path.write_text("")


def _trip_article_guard(cfg_a, cfg_b, n=12):
    """A ingests n articles, both devices converge, A deletes ALL n and
    pushes — B's next pull now faces a guarded segment (n RowDels > the
    max(10, 20%) threshold)."""
    arts = [_ingest(cfg_a, title=f"Guard {i:02d}",
                    url=f"https://example.com/guard-{i}") for i in range(n)]
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"  # auto-bootstrap (D-S5-3)
    assert _count(cfg_b) == n
    for art in arts:
        delete_article(cfg_a, art["id"])
    assert _count(cfg_a) == 0
    assert _sync(cfg_a).result == "ok"


def test_guard_state_persists_across_cycles(guard_rig):
    """A tripped guard NEVER self-clears: every un-accepted cycle re-trips
    (watermark held, nothing applied) until an explicit acceptance — the trip
    is re-derived from the un-advanced watermark each cycle, not a sticky
    flag someone could lose."""
    cfg_a, cfg_b = guard_rig
    _trip_article_guard(cfg_a, cfg_b)

    first = _sync(cfg_b)
    assert first.result == "needs_attention"
    assert first.reason == "mass_delete_guard"
    assert first.guard
    assert _count(cfg_b) == 12  # nothing applied

    second = _sync(cfg_b)  # NO acceptance — must re-trip, not self-clear
    assert second.result == "needs_attention"
    assert second.reason == "mass_delete_guard"
    assert second.guard
    assert _count(cfg_b) == 12  # still nothing applied

    accepted = _sync(cfg_b, accept_mass_delete=True)
    assert accepted.result == "ok"
    assert accepted.guard is None  # acceptance consumed cleanly
    assert _count(cfg_b) == 0


def test_cli_accept_mass_delete_clears_trip(guard_rig, capsys):
    """The FROZEN CLI shape end-to-end: `tiro sync --now` surfaces the trip
    (GUARDED + reason), `tiro sync --now --accept-mass-delete` clears it —
    through the real cmd_sync, not the engine kwarg directly."""
    cfg_a, cfg_b = guard_rig
    _trip_article_guard(cfg_a, cfg_b)

    cmd_sync(Args(cfg_b, now=True))
    out = capsys.readouterr().out
    assert "needs_attention" in out
    assert "GUARDED" in out
    assert "mass_delete_guard" in out
    assert _count(cfg_b) == 12

    cmd_sync(Args(cfg_b, now=True, accept_mass_delete=True))
    out = capsys.readouterr().out
    assert "Sync: ok" in out
    assert "GUARDED" not in out
    assert _count(cfg_b) == 0


def test_annotations_mass_delete_guard_equivalent(guard_rig):
    """Spec §4's "annotations-guard equivalent": a pulled diff deleting >
    max(10, 20%) of local HIGHLIGHTS halts the merge exactly like the
    article guard — rows AND sidecar lines survive the trip; acceptance
    applies the wipe (rows gone, sidecars emptied)."""
    cfg_a, cfg_b = guard_rig
    arts = [_ingest(cfg_a, title=f"Marked {i}", body=MARKED_BODY,
                    url=f"https://example.com/marked-{i}") for i in range(3)]
    for art in arts:
        _highlight_words(cfg_a, art["id"], GUARD_WORDS)
    assert _hl_count(cfg_a) == 15
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    assert _hl_count(cfg_b) == 15

    _wipe_annotation_sidecars(cfg_a, expected=3)
    report_a = _sync(cfg_a)
    # A's own wipe is A's own edit: the pull-side guard does not apply, the
    # S1 reconcile guard stays quiet (files present), rows empty files-win.
    assert report_a.result == "ok"
    assert _hl_count(cfg_a) == 0

    tripped = _sync(cfg_b)
    assert tripped.result == "needs_attention"
    assert tripped.reason == "mass_delete_guard"
    # Exact-count message (S6.4 review nit): pins that ALL 15 LineDels
    # crossed the wire and were counted against B's 15 local highlights.
    assert "15 highlight deletions vs 15 local highlights" in tripped.guard
    assert _hl_count(cfg_b) == 15  # rows survive the trip
    b_sidecars = sorted((cfg_b.library / "annotations").glob("*.jsonl"))
    assert len(b_sidecars) == 3
    assert all(p.read_text() for p in b_sidecars)  # lines survive too
    for art in _q(cfg_b, "SELECT markdown_path FROM articles"):
        assert len(read_annotations(cfg_b, sidecar_stem(art))) == 5

    accepted = _sync(cfg_b, accept_mass_delete=True)
    assert accepted.result == "ok"
    assert accepted.guard is None
    assert _hl_count(cfg_b) == 0
    for path in b_sidecars:
        assert path.read_text() == ""  # emptied, not orphaned


def test_guard_acceptance_is_one_shot_across_segments(guard_rig):
    """One-shot acceptance END-TO-END across the two guard KINDS: A's two
    cycles push one article-mass-delete segment then one highlight-mass-
    delete segment; B's accepted run consumes the acceptance on the FIRST
    trip (articles applied) and re-trips on the second (highlights held); a
    second accepted run clears it."""
    cfg_a, cfg_b = guard_rig
    bulk = [_ingest(cfg_a, title=f"Bulk {i:02d}",
                    url=f"https://example.com/bulk-{i}") for i in range(12)]
    marked = [_ingest(cfg_a, title=f"Marked {i}", body=MARKED_BODY,
                      url=f"https://example.com/marked-{i}") for i in range(3)]
    for art in marked:
        _highlight_words(cfg_a, art["id"], GUARD_WORDS)
    assert _sync(cfg_a).result == "ok"
    assert _sync(cfg_b).result == "ok"
    assert _count(cfg_b) == 15
    assert _hl_count(cfg_b) == 15

    # Guarded segment #1: 12 article deletions (> max(10, ceil(0.2*15))).
    for art in bulk:
        delete_article(cfg_a, art["id"])
    assert _sync(cfg_a).result == "ok"
    # Guarded segment #2: 15 highlight deletions (> max(10, ceil(0.2*15))).
    _wipe_annotation_sidecars(cfg_a, expected=3)
    assert _sync(cfg_a).result == "ok"

    report = _sync(cfg_b, accept_mass_delete=True)
    assert report.result == "needs_attention"
    assert report.reason == "mass_delete_guard"
    assert "highlight" in report.guard  # the SECOND, un-accepted trip
    assert _count(cfg_b) == 3     # first trip consumed the acceptance, applied
    assert _hl_count(cfg_b) == 15  # second trip held its segment

    report2 = _sync(cfg_b, accept_mass_delete=True)
    assert report2.result == "ok"
    assert report2.guard is None
    assert _hl_count(cfg_b) == 0
    assert _count(cfg_b) == 3


def test_cli_argv_shape_parses_to_expected_dests(monkeypatch):
    """S6.4 review #1: pin the REAL argv shape `tiro sync --now
    --accept-mass-delete` through the REAL parser (main() builds it inline
    with a bare parse_args()) — the hand-rolled Args stand-ins elsewhere
    verify dispatch behavior but would stay green through a flag rename
    that breaks the shipped CLI. cmd_sync is intercepted so nothing runs."""
    import tiro.cli as cli

    captured: dict = {}

    def _capture(args):
        captured["args"] = args

    monkeypatch.setattr(cli, "cmd_sync", _capture)
    monkeypatch.setattr(
        "sys.argv", ["tiro", "sync", "--now", "--accept-mass-delete"])
    cli.main()
    args = captured["args"]
    assert args.command == "sync"
    assert args.now is True
    assert args.accept_mass_delete is True
    assert args.status is False
    assert args.sync_cmd is None

    # The subcommand forms parse too (setup/repair ride sync_cmd).
    monkeypatch.setattr("sys.argv", ["tiro", "sync", "repair"])
    cli.main()
    assert captured["args"].sync_cmd == "repair"

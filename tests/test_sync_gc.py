"""Sync S3: compaction/GC pure logic (spec para 6.5).

Cadence 500 ops / 7 days; segments deletable below every live device's ack
AND the latest snapshot's covers; devices unseen >90d stop blocking GC."""
from datetime import UTC, datetime

from tiro.sync.snapshot import (
    DEAD_DEVICE_DAYS,
    SNAPSHOT_MAX_AGE_DAYS,
    SNAPSHOT_OPS_THRESHOLD,
    DeviceInfo,
    journal_key,
    object_key,
    plan_gc,
    plan_object_gc,
    should_snapshot,
    snapshot_key,
)

NOW = datetime(2026, 7, 11, tzinfo=UTC)


def _dev(device_id, last_seen, last_seq, acked):
    return DeviceInfo(device_id=device_id, name=device_id, last_seen=last_seen,
                      last_seq=last_seq, app_version="0.7.0", acked=acked)


class TestShouldSnapshot:
    def test_frozen_constants(self):
        assert SNAPSHOT_OPS_THRESHOLD == 500
        assert SNAPSHOT_MAX_AGE_DAYS == 7
        assert DEAD_DEVICE_DAYS == 90

    def test_nothing_new_never_snapshots(self):
        assert not should_snapshot(0, None, now=NOW)
        assert not should_snapshot(0, "2020-01-01T00:00:00Z", now=NOW)

    def test_first_snapshot_on_first_ops(self):
        assert should_snapshot(1, None, now=NOW)

    def test_ops_threshold(self):
        recent = "2026-07-10T00:00:00Z"
        assert not should_snapshot(500, recent, now=NOW)  # spec: STRICTLY > 500
        assert should_snapshot(501, recent, now=NOW)

    def test_age_threshold(self):
        assert not should_snapshot(1, "2026-07-05T00:00:00Z", now=NOW)  # 6d
        assert should_snapshot(1, "2026-07-03T00:00:00Z", now=NOW)      # 8d

    def test_unparseable_timestamp_snapshots(self):
        assert should_snapshot(1, "garbage", now=NOW)  # fail-safe: take one


class TestPlanGc:
    def _base(self):
        devices = {
            "dev-a": _dev("dev-a", "2026-07-10T00:00:00Z", 5, {"dev-b": 2}),
            "dev-b": _dev("dev-b", "2026-07-09T00:00:00Z", 3, {"dev-a": 4}),
        }
        segments = [journal_key("dev-a", s) for s in (1, 2, 3, 4, 5)] + \
                   [journal_key("dev-b", s) for s in (1, 2, 3)]
        # ULIDs sort lexicographically by creation time — "...B" is NEWER.
        covers = {"01SNAP0000000000000000000A": {"dev-a": 2, "dev-b": 1},
                  "01SNAP0000000000000000000B": {"dev-a": 4, "dev-b": 2}}
        return devices, segments, covers

    def test_watermark_rule(self):
        devices, segments, covers = self._base()
        plan = plan_gc(devices=devices, segment_keys=segments,
                       snapshot_covers=covers, now=NOW)
        # dev-a segments: min(covers 4, dev-b acked 4, own last_seq 5) => 1..4
        # dev-b segments: min(covers 2, dev-a acked 2, own last_seq 3) => 1..2
        assert plan.delete_segments == sorted(
            [journal_key("dev-a", s) for s in (1, 2, 3, 4)]
            + [journal_key("dev-b", s) for s in (1, 2)])
        # Only the newest snapshot (max ULID) survives.
        assert plan.delete_snapshots == [snapshot_key("01SNAP0000000000000000000A")]
        assert plan.dropped_devices == []

    def test_dead_device_stops_blocking(self):
        devices, segments, covers = self._base()
        devices["dev-c"] = _dev("dev-c", "2026-01-01T00:00:00Z", 0, {})  # >90d
        plan = plan_gc(devices=devices, segment_keys=segments,
                       snapshot_covers=covers, now=NOW)
        assert plan.dropped_devices == ["dev-c"]
        assert any("dev-c" in w for w in plan.warnings)
        # Same deletions as without dev-c: it no longer pins the journal.
        assert journal_key("dev-a", 4) in plan.delete_segments

    def test_live_laggard_blocks(self):
        devices, segments, covers = self._base()
        devices["dev-c"] = _dev("dev-c", "2026-07-11T00:00:00Z", 0, {})  # live, acked nothing
        plan = plan_gc(devices=devices, segment_keys=segments,
                       snapshot_covers=covers, now=NOW)
        assert plan.delete_segments == []

    def test_no_snapshot_means_no_gc(self):
        devices, segments, _ = self._base()
        plan = plan_gc(devices=devices, segment_keys=segments,
                       snapshot_covers={}, now=NOW)
        assert plan.delete_segments == [] and plan.delete_snapshots == []

    def test_stray_keys_warn_not_crash(self):
        devices, segments, covers = self._base()
        plan = plan_gc(devices=devices, segment_keys=segments + ["journal/x/bad.age"],
                       snapshot_covers=covers, now=NOW)
        assert any("bad" in w for w in plan.warnings)


class TestPlanObjectGc:
    def test_unreferenced_objects_deleted(self):
        live, dead = "a" * 64, "b" * 64
        keys = [object_key(live), object_key(dead)]
        assert plan_object_gc({live}, keys) == [object_key(dead)]

    def test_stray_keys_ignored(self):
        assert plan_object_gc(set(), ["objects/zz/nothex.age"]) == []

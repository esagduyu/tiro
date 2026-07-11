"""Sync S2: journal primitives — HLC, op dataclasses, wire serialization.

Pure module: no config, no SQLite, no sockets (zero-I/O gate enforced in
tests/test_sync_properties.py; this file covers behavior)."""
from pathlib import Path

import pytest

from tiro.sync.journal import (
    HLC,
    OP_KINDS,
    SYNC_FORMAT,
    TOMBSTONE_TTL_DAYS,
    Alias,
    FileDel,
    FilePut,
    HLCClock,
    JournalError,
    LineDel,
    LinePut,
    Meta,
    RowDel,
    RowPut,
    ops_from_jsonl,
    ops_to_jsonl,
)

GOLDEN = Path(__file__).parent / "fixtures" / "sync-journal-golden.jsonl"


class TestHLC:
    def test_frozen_constants(self):
        assert TOMBSTONE_TTL_DAYS == 90
        assert SYNC_FORMAT == 1
        assert OP_KINDS == (
            "file_put", "file_del", "line_put", "line_del",
            "meta", "row_put", "row_del", "alias",
        )

    def test_str_roundtrip_and_lexicographic_order(self):
        a = HLC(1720000000000, 0, "dev-a")
        b = HLC(1720000000000, 1, "dev-a")
        c = HLC(1720000000001, 0, "dev-b")
        assert HLC.parse(a.to_str()) == a
        assert a < b < c
        # Lexicographic string order == logical order (LWW compares strings).
        assert a.to_str() < b.to_str() < c.to_str()

    def test_parse_tolerates_dashes_in_device(self):
        h = HLC(5, 7, "my-laptop-2")
        assert HLC.parse(h.to_str()) == h

    def test_device_breaks_ties_totally(self):
        a = HLC(5, 0, "dev-a")
        b = HLC(5, 0, "dev-b")
        assert a < b and not (b < a) and a != b

    def test_clock_monotonic_against_stalled_wall(self):
        times = iter([100, 100, 100, 50])  # wall clock stalls, then REGRESSES
        clock = HLCClock("dev-a", now_ms=lambda: next(times))
        ticks = [clock.tick() for _ in range(4)]
        for earlier, later in zip(ticks, ticks[1:], strict=False):
            assert earlier < later
        assert ticks[3].wall_ms == 100  # regression bounded, never goes back

    def test_clock_observe_folds_remote_in(self):
        clock = HLCClock("dev-a", now_ms=lambda: 100)
        clock.observe(HLC(9999, 3, "dev-b"))
        t = clock.tick()
        assert t > HLC(9999, 3, "dev-b")
        assert t.device == "dev-a"


def _sample_ops():
    """One op of every kind, fixed values — the golden-fixture corpus."""
    hlc = HLC(1720000000000, 0, "dev-a")
    return [
        FilePut(op_id="01SAMPLE00000000000000FP1", hlc=hlc, device="dev-a",
                uid="01ART00000000000000000001",
                path_hint="articles/2026-07-10_hello.md",
                object_hash="a" * 64, base_hash="b" * 64, body="# Hello\n"),
        FileDel(op_id="01SAMPLE00000000000000FD1", hlc=hlc, device="dev-a",
                uid="01ART00000000000000000001",
                path_hint="notes/2026-07-10_hello.md", base_hash="c" * 64),
        LinePut(op_id="01SAMPLE00000000000000LP1", hlc=hlc, device="dev-a",
                uid="01HLT00000000000000000001",
                article_uid="01ART00000000000000000001",
                line={"uid": "01HLT00000000000000000001",
                      "article_uid": "01ART00000000000000000001",
                      "quote": "bravo", "prefix": "alpha ", "suffix": " charlie",
                      "position_start": 6, "position_end": 11,
                      "content_hash": "d" * 64, "color": "yellow",
                      "note_markdown": None,
                      "created_at": "2026-07-10T00:00:00Z",
                      "updated_at": "2026-07-10T00:00:00Z"}),
        LineDel(op_id="01SAMPLE00000000000000LD1", hlc=hlc, device="dev-a",
                uid="01HLT00000000000000000001",
                article_uid="01ART00000000000000000001",
                observed_updated_at="2026-07-10T00:00:00Z"),
        Meta(op_id="01SAMPLE00000000000000MT1", hlc=hlc, device="dev-a",
             uid="01ART00000000000000000001",
             field="rating", value=2, ts="2026-07-10T00:00:01Z"),
        RowPut(op_id="01SAMPLE00000000000000RP1", hlc=hlc, device="dev-a",
               uid="01SRC00000000000000000001", table="sources",
               row={"uid": "01SRC00000000000000000001", "name": "Example",
                    "domain": "example.com", "email_sender": None,
                    "source_type": "web", "is_vip": 1,
                    "created_at": "2026-07-01 00:00:00"}),
        RowDel(op_id="01SAMPLE00000000000000RD1", hlc=hlc, device="dev-a",
               uid="01ART00000000000000000002", table="articles",
               observed="e" * 64),
        Alias(op_id="01SAMPLE00000000000000AL1", hlc=hlc, device="dev-a",
              uid="01ART00000000000000000003",
              new_uid="01ART00000000000000000001"),
    ]


class TestWire:
    def test_kind_classvars_match_spec_verbatim(self):
        kinds = [type(op).kind for op in _sample_ops()]
        assert kinds == list(OP_KINDS)

    def test_roundtrip_all_kinds(self):
        ops = _sample_ops()
        text, objects = ops_to_jsonl(ops)
        back = ops_from_jsonl(text, objects)
        assert back == ops

    def test_file_body_never_on_the_wire(self):
        ops = [_sample_ops()[0]]
        text, objects = ops_to_jsonl(ops)
        assert "# Hello" not in text
        assert objects == {"a" * 64: "# Hello\n"}

    def test_missing_object_raises_journal_error(self):
        text, _objects = ops_to_jsonl([_sample_ops()[0]])
        with pytest.raises(JournalError):
            ops_from_jsonl(text, {})

    def test_unknown_kind_raises_journal_error(self):
        with pytest.raises(JournalError):
            ops_from_jsonl(
                '{"op":"x","hlc":"0000000000005-000000-d","device":"d",'
                '"kind":"nonsense","uid":"u","payload":{}}\n', {})

    def test_garbage_line_raises_journal_error(self):
        with pytest.raises(JournalError):
            ops_from_jsonl("not json at all\n", {})

    def test_golden_fixture_frozen(self):
        """sync_format 1 is frozen by this fixture (spec §5): serialization
        must reproduce it byte-for-byte. A change here is a FORMAT change —
        it requires bumping SYNC_FORMAT, not editing the fixture."""
        text, _objects = ops_to_jsonl(_sample_ops())
        assert text == GOLDEN.read_text()

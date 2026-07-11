"""Sync journal primitives (S2 — pure merge core).

HLC (hybrid logical clock), the eight journal op dataclasses (spec §5 kinds
verbatim), and canonical wire (de)serialization. PURE: no config, no SQLite,
no network — enforced by the zero-I/O gate in tests/test_sync_properties.py.

Wire envelope (spec §5): one JSON object per line —
    {"op": ulid, "hlc": str, "device": str, "kind": str, "uid": str,
     "base_hash"?: str, "payload": {...}}
Canonical JSON (sort_keys, compact separators, ensure_ascii=False) so the
bytes are deterministic; sync_format 1 is frozen by the golden fixture
tests/fixtures/sync-journal-golden.jsonl — changing these bytes means
bumping SYNC_FORMAT (older devices must refuse with an upgrade prompt).

File bodies are NEVER on the wire: FilePut.body is in-memory only;
ops_to_jsonl externalizes bodies into a content-addressed {hash: body} dict
(the shape S3's objects/ store consumes) and ops_from_jsonl re-hydrates.
"""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

SYNC_FORMAT = 1
TOMBSTONE_TTL_DAYS = 90  # FROZEN (spec §4: tombstone GC after ack + 90d TTL)

OP_KINDS = (
    "file_put", "file_del", "line_put", "line_del",
    "meta", "row_put", "row_del", "alias",
)


class JournalError(ValueError):
    """A journal segment that cannot be (de)serialized faithfully.

    Callers (S3/S5) treat this as quarantine-the-segment, never
    half-apply (spec §6.7)."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@functools.total_ordering
@dataclass(frozen=True)
class HLC:
    """Hybrid logical clock stamp: max(wall_ms, last+1) + device tiebreak
    (spec §3). Total order; the string form sorts identically to the
    logical order so LWW can compare stored strings directly."""

    wall_ms: int
    counter: int
    device: str

    def to_str(self) -> str:
        return f"{self.wall_ms:013d}-{self.counter:06d}-{self.device}"

    @classmethod
    def parse(cls, s: str) -> HLC:
        try:
            wall, counter, device = s.split("-", 2)
            return cls(int(wall), int(counter), device)
        except (ValueError, AttributeError) as e:
            raise JournalError(f"bad HLC string: {s!r}") from e

    def _key(self) -> tuple:
        return (self.wall_ms, self.counter, self.device)

    def __lt__(self, other: HLC) -> bool:
        return self._key() < other._key()


class HLCClock:
    """Per-device HLC generator. tick() never regresses even when the wall
    clock does; observe() folds a remote stamp in so subsequent local ticks
    sort after everything this device has seen (the engine observes every
    pulled op's hlc — that is what makes local edits 'newest')."""

    def __init__(self, device: str, *, now_ms: Callable[[], int] | None = None):
        self.device = device
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._last = HLC(0, 0, device)

    def tick(self) -> HLC:
        wall = self._now_ms()
        if wall > self._last.wall_ms:
            self._last = HLC(wall, 0, self.device)
        else:
            self._last = HLC(self._last.wall_ms, self._last.counter + 1, self.device)
        return self._last

    def observe(self, other: HLC) -> None:
        if (other.wall_ms, other.counter) > (self._last.wall_ms, self._last.counter):
            self._last = HLC(other.wall_ms, other.counter, self.device)


@dataclass(frozen=True)
class _OpBase:
    op_id: str
    hlc: HLC
    device: str
    uid: str


@dataclass(frozen=True)
class FilePut(_OpBase):
    """file_put(path_hint, object_hash) — spec §5. body is IN-MEMORY ONLY."""
    path_hint: str = ""
    object_hash: str = ""
    base_hash: str | None = None
    body: str | None = None
    kind: ClassVar[str] = "file_put"


@dataclass(frozen=True)
class FileDel(_OpBase):
    path_hint: str = ""
    base_hash: str | None = None
    kind: ClassVar[str] = "file_del"


@dataclass(frozen=True)
class LinePut(_OpBase):
    """line_put(article_uid, line) — uid is the HIGHLIGHT uid; line is the
    full self-contained JSONL annotation line (CRDT headroom, spec §10)."""
    article_uid: str = ""
    line: dict = field(default_factory=dict)
    kind: ClassVar[str] = "line_put"


@dataclass(frozen=True)
class LineDel(_OpBase):
    """line_del(highlight_uid) — observed_updated_at drives the
    note-resurrect rule (spec §4: a concurrent note_markdown edit
    resurrects as an article-level conflict note)."""
    article_uid: str = ""
    observed_updated_at: str | None = None
    kind: ClassVar[str] = "line_del"


@dataclass(frozen=True)
class Meta(_OpBase):
    """meta(field, value, ts) — uid is the ARTICLE uid; ts is the
    meta_updated_at-format LWW clock (NOT the HLC)."""
    field: str = ""
    value: Any = None
    ts: str = ""
    kind: ClassVar[str] = "meta"


@dataclass(frozen=True)
class RowPut(_OpBase):
    table: str = ""
    row: dict = field(default_factory=dict)
    kind: ClassVar[str] = "row_put"


@dataclass(frozen=True)
class RowDel(_OpBase):
    """row_del — observed carries edit-wins/add-wins context: for
    table='articles' the deleting device's last-seen body_hash; for link
    tables the HLC string of the link add it removed; else None."""
    table: str = ""
    observed: str | None = None
    kind: ClassVar[str] = "row_del"


@dataclass(frozen=True)
class Alias(_OpBase):
    """alias(old_uid, new_uid) — uid is the OLD (losing) uid."""
    new_uid: str = ""
    kind: ClassVar[str] = "alias"


Op = FilePut | FileDel | LinePut | LineDel | Meta | RowPut | RowDel | Alias

_KIND_TO_CLS: dict[str, type] = {
    "file_put": FilePut, "file_del": FileDel, "line_put": LinePut,
    "line_del": LineDel, "meta": Meta, "row_put": RowPut,
    "row_del": RowDel, "alias": Alias,
}


def op_to_wire(op: Op) -> tuple[dict, dict[str, str]]:
    """(wire line dict, objects contributed by this op)."""
    envelope: dict[str, Any] = {
        "op": op.op_id, "hlc": op.hlc.to_str(), "device": op.device,
        "kind": type(op).kind, "uid": op.uid,
    }
    objects: dict[str, str] = {}
    if isinstance(op, FilePut):
        envelope["base_hash"] = op.base_hash
        envelope["payload"] = {"path_hint": op.path_hint, "object_hash": op.object_hash}
        if op.body is not None:
            objects[op.object_hash] = op.body
    elif isinstance(op, FileDel):
        envelope["base_hash"] = op.base_hash
        envelope["payload"] = {"path_hint": op.path_hint}
    elif isinstance(op, LinePut):
        envelope["payload"] = {"article_uid": op.article_uid, "line": op.line}
    elif isinstance(op, LineDel):
        envelope["payload"] = {
            "article_uid": op.article_uid,
            "observed_updated_at": op.observed_updated_at,
        }
    elif isinstance(op, Meta):
        envelope["payload"] = {"field": op.field, "value": op.value, "ts": op.ts}
    elif isinstance(op, RowPut):
        envelope["payload"] = {"table": op.table, "row": op.row}
    elif isinstance(op, RowDel):
        envelope["payload"] = {"table": op.table, "observed": op.observed}
    elif isinstance(op, Alias):
        envelope["payload"] = {"new_uid": op.new_uid}
    else:  # pragma: no cover — the union above is closed
        raise JournalError(f"unknown op type: {type(op)!r}")
    return envelope, objects


def op_from_wire(d: dict, objects: dict[str, str]) -> Op:
    try:
        kind = d["kind"]
        cls = _KIND_TO_CLS.get(kind)
        if cls is None:
            raise JournalError(f"unknown op kind: {kind!r}")
        base = {
            "op_id": d["op"], "hlc": HLC.parse(d["hlc"]),
            "device": d["device"], "uid": d["uid"],
        }
        p = d.get("payload") or {}
        if cls is FilePut:
            object_hash = p["object_hash"]
            if object_hash not in objects:
                raise JournalError(f"missing object for hash {object_hash!r}")
            return FilePut(**base, path_hint=p["path_hint"],
                           object_hash=object_hash,
                           base_hash=d.get("base_hash"),
                           body=objects[object_hash])
        if cls is FileDel:
            return FileDel(**base, path_hint=p["path_hint"],
                           base_hash=d.get("base_hash"))
        if cls is LinePut:
            return LinePut(**base, article_uid=p["article_uid"], line=p["line"])
        if cls is LineDel:
            return LineDel(**base, article_uid=p["article_uid"],
                           observed_updated_at=p.get("observed_updated_at"))
        if cls is Meta:
            return Meta(**base, field=p["field"], value=p.get("value"), ts=p["ts"])
        if cls is RowPut:
            return RowPut(**base, table=p["table"], row=p["row"])
        if cls is RowDel:
            return RowDel(**base, table=p["table"], observed=p.get("observed"))
        return Alias(**base, new_uid=p["new_uid"])
    except JournalError:
        raise
    except Exception as e:
        raise JournalError(f"malformed op line: {e}") from e


def ops_to_jsonl(ops: list[Op]) -> tuple[str, dict[str, str]]:
    """Serialize ops to canonical JSONL + the content-addressed bodies they
    reference. The JSONL bytes are what S3 encrypts into a journal segment;
    the objects dict is what S3 uploads to objects/ FIRST (spec §6.4)."""
    lines: list[str] = []
    objects: dict[str, str] = {}
    for op in ops:
        envelope, objs = op_to_wire(op)
        objects.update(objs)
        lines.append(canonical_json(envelope))
    return "".join(line + "\n" for line in lines), objects


def ops_from_jsonl(text: str, objects: dict[str, str]) -> list[Op]:
    ops: list[Op] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
            if not isinstance(d, dict):
                raise JournalError("op line is not a JSON object")
        except JournalError:
            raise
        except Exception as e:
            raise JournalError(f"invalid JSON at line {lineno}: {e}") from e
        ops.append(op_from_wire(d, objects))
    return ops

"""Sync S2: THE 1.0 HARD GATE — hypothesis property suite (spec §9) +
zero-I/O enforcement over the pure modules.

Run repeatedly with randomized seeds before trusting it:
    uv run pytest tests/test_sync_properties.py --hypothesis-seed=random
(3 consecutive green runs minimum, per the plan header.)

NEVER weaken a property to make it pass — a failing property is a real
merge bug. Pin shrunk counterexamples as @example regressions instead.
"""
import re
import socket
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from tiro.sync.journal import (
    HLC,
    HLCClock,
    LineDel,
    LinePut,
    Meta,
    ops_from_jsonl,
    ops_to_jsonl,
)
from tiro.sync.merge import merge_jsonl

# --- profiles -------------------------------------------------------------------

settings.register_profile(
    "sync_pure", max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.register_profile(
    "sync_apply", max_examples=25, deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
# function_scoped_fixture suppression: conftest's AUTOUSE fixtures
# (_isolate_cwd, _no_external_apis) are function-scoped and would trip the
# health check on every @given test. All per-example state in this module is
# created INSIDE the test bodies (fresh tempdir libraries), so examples never
# share mutated state — the suppression is sound, not a dodge.


# --- zero-I/O gate ---------------------------------------------------------------

_PURE_MODULES = ("journal.py", "manifest.py", "merge.py")


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("sync pure core attempted network I/O")
    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


def test_pure_modules_import_no_network_libs():
    # `from urllib.parse import ...` is PERMITTED (pure string parsing —
    # merge.py's _source_for uses urlparse for domain extraction, zero
    # sockets); every network-capable urllib form stays banned alongside
    # the client libraries. The socket monkeypatch fixture above remains
    # the behavioral gate either way.
    root = Path(__file__).parent.parent / "tiro" / "sync"
    banned = re.compile(
        r"^\s*(?:import\s+(?:httpx|requests|socket|aiohttp|anthropic|openai|"
        r"urllib3|urllib)\b"
        r"|from\s+(?:httpx|requests|socket|aiohttp|anthropic|openai|urllib3|"
        r"urllib\.request|urllib\.error)\b"
        r"|from\s+urllib\s+import)", re.MULTILINE)
    for name in _PURE_MODULES:
        src = (root / name).read_text()
        assert not banned.search(src), f"{name} imports a network library"


# --- strategies -------------------------------------------------------------------

DEVICES = ("deva", "devb", "devc")
HL_UIDS = tuple(f"01HLUID000000000000000000{i}" for i in range(5))
ART_UIDS = tuple(f"01ARTUID00000000000000000{i}" for i in range(4))
COLORS = ("yellow", "green", "blue", "pink")

st_ts = st.integers(min_value=0, max_value=10 * 24 * 3600).map(
    lambda s: f"2026-07-{1 + s // 86400:02d}T{(s % 86400) // 3600:02d}:"
              f"{(s % 3600) // 60:02d}:{s % 60:02d}Z")
st_note = st.one_of(
    st.none(),
    st.text(min_size=1, max_size=40),
    st.sampled_from(["   ", "línea única", "line1\nline2", "> already quoted"]),
)


@st.composite
def st_line(draw, uid=None):
    return {
        "uid": uid or draw(st.sampled_from(HL_UIDS)),
        "article_uid": draw(st.sampled_from(ART_UIDS)),
        "quote": draw(st.text(min_size=1, max_size=20)),
        "prefix": draw(st.text(max_size=8)),
        "suffix": draw(st.text(max_size=8)),
        "position_start": draw(st.integers(0, 500)),
        "position_end": draw(st.integers(0, 500)),
        "content_hash": draw(st.sampled_from(["a" * 64, "b" * 64])),
        "color": draw(st.sampled_from(COLORS)),
        "note_markdown": draw(st_note),
        "created_at": draw(st_ts),
        "updated_at": draw(st.one_of(st.none(), st_ts)),
    }


def st_lines():
    return st.lists(st_line(), max_size=8).map(
        lambda lines: list({ln["uid"]: ln for ln in lines}.values()))


st_hlc = st.builds(HLC,
                   wall_ms=st.integers(0, 10**13 - 1),
                   counter=st.integers(0, 999999),
                   device=st.sampled_from(DEVICES))


# --- HLC properties ----------------------------------------------------------------

@settings(settings.get_profile("sync_pure"))
@given(st_hlc)
def test_hlc_str_roundtrip_identity(h):
    assert HLC.parse(h.to_str()) == h


@settings(settings.get_profile("sync_pure"))
@given(st_hlc, st_hlc)
def test_hlc_string_order_equals_logical_order(a, b):
    assert (a < b) == (a.to_str() < b.to_str())
    assert (a == b) == (a.to_str() == b.to_str())


@settings(settings.get_profile("sync_pure"))
@given(st.lists(st.integers(0, 10**12), min_size=1, max_size=30),
       st.lists(st_hlc, max_size=5))
def test_hlc_clock_monotone_under_any_wall_clock_and_observations(walls, seen):
    it = iter(walls + [walls[-1]] * (len(seen) + len(walls)))
    clock = HLCClock("deva", now_ms=lambda: next(it))
    last = clock.tick()
    for h in seen:
        clock.observe(h)
        nxt = clock.tick()
        assert nxt > last and nxt > h
        last = nxt


# --- wire round-trip ------------------------------------------------------------------

@settings(settings.get_profile("sync_pure"))
@given(st_lines(), st_hlc)
@example(
    # Pinned S2.8 counterexample: a note containing U+0085 (NEL). JSON with
    # ensure_ascii=False emits it RAW; the reader used str.splitlines(),
    # which treats NEL/LS/PS as line breaks and sheared the wire line
    # mid-string. ops_from_jsonl now splits on "\n" only.
    lines=[{"uid": HL_UIDS[0], "article_uid": ART_UIDS[0], "quote": "0",
            "prefix": "", "suffix": "", "position_start": 0,
            "position_end": 0, "content_hash": "a" * 64, "color": "yellow",
            "note_markdown": "\x85", "created_at": "2026-07-01T00:00:00Z",
            "updated_at": None}],
    hlc=HLC(wall_ms=0, counter=0, device="deva"),
)
def test_wire_roundtrip_line_ops(lines, hlc):
    ops = [LinePut(op_id=f"01OP{i:022d}", hlc=hlc, device=hlc.device,
                   uid=ln["uid"], article_uid=ln["article_uid"], line=ln)
           for i, ln in enumerate(lines)]
    text, objects = ops_to_jsonl(ops)
    assert ops_from_jsonl(text, objects) == ops


# --- merge_jsonl properties (spec §9: commutative/assoc/idempotent, no-note-loss) ---

@settings(settings.get_profile("sync_pure"))
@given(st_lines(), st_lines())
def test_merge_jsonl_commutative(a, b):
    assert merge_jsonl(a, b, label_a="x", label_b="y") == \
           merge_jsonl(b, a, label_a="y", label_b="x")


@settings(settings.get_profile("sync_pure"))
@given(st_lines())
def test_merge_jsonl_idempotent(a):
    once = merge_jsonl(a, a)
    assert merge_jsonl(once, once) == once
    assert merge_jsonl(once, a) == once


@settings(settings.get_profile("sync_pure"))
@given(st_lines(), st_lines(), st_lines())
def test_merge_jsonl_associative_on_line_sets(a, b, c):
    """Associativity holds for the RESULTING LINE SET (uid -> winning core
    fields); note bodies may nest conflict blockquotes differently by
    grouping, so compare everything EXCEPT note_markdown, then assert
    no-note-loss separately (the spec's actual trust property)."""
    def core(lines):
        return {ln["uid"]: {k: v for k, v in ln.items()
                            if k != "note_markdown"} for ln in lines}
    ab_c = merge_jsonl(merge_jsonl(a, b), c)
    a_bc = merge_jsonl(a, merge_jsonl(b, c))
    assert core(ab_c) == core(a_bc)


@settings(settings.get_profile("sync_pure"))
@given(st_lines(), st_lines())
@example(
    a=[{"uid": HL_UIDS[0], "article_uid": ART_UIDS[0], "quote": "q",
        "prefix": "", "suffix": "", "position_start": 0, "position_end": 1,
        "content_hash": "a" * 64, "color": "yellow",
        "note_markdown": "precious", "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z"}],
    b=[{"uid": HL_UIDS[0], "article_uid": ART_UIDS[0], "quote": "q",
        "prefix": "", "suffix": "", "position_start": 0, "position_end": 1,
        "content_hash": "a" * 64, "color": "pink", "note_markdown": None,
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-02T00:00:00Z"}],
)
@example(
    # Pinned S2.8 counterexample: a MULTI-LINE loser note. The pre-S2.8
    # blockquote quoted every line with "> ", so the raw note was no longer
    # a verbatim substring of the merged note — the trust property text
    # ("appears verbatim") was violated for any note containing a newline.
    a=[{"uid": HL_UIDS[0], "article_uid": ART_UIDS[0], "quote": "q",
        "prefix": "", "suffix": "", "position_start": 0, "position_end": 1,
        "content_hash": "a" * 64, "color": "yellow",
        "note_markdown": "line1\nline2", "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z"}],
    b=[{"uid": HL_UIDS[0], "article_uid": ART_UIDS[0], "quote": "q",
        "prefix": "", "suffix": "", "position_start": 0, "position_end": 1,
        "content_hash": "a" * 64, "color": "pink", "note_markdown": None,
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-02T00:00:00Z"}],
)
def test_merge_jsonl_never_loses_a_note(a, b):
    """THE trust property: every non-empty note body present in either
    input appears verbatim inside some output line's note_markdown."""
    merged = merge_jsonl(a, b)
    haystack = "\n".join((ln.get("note_markdown") or "") for ln in merged)
    for src in (a, b):
        for ln in src:
            note = ln.get("note_markdown")
            if note and note.strip():
                assert note in haystack, f"lost note {note!r}"


# --- apply-level properties (SQLite-backed mini libraries) ---------------------------

def _mini_lib(root: Path, name: str):
    """Throwaway library: init_db only — NO ChromaDB (delete_article's
    vector delete is best-effort try/except, verified at lifecycle.py:28)."""
    from tiro.config import TiroConfig
    from tiro.database import init_db

    lib = root / name
    (lib / "articles").mkdir(parents=True)
    config = TiroConfig(library_path=str(lib))
    init_db(config.db_path)
    return config


def _seed_article(config, uid, url, source_id=None):
    """Minimal article row + file, bypassing process_article (no vectors,
    no enrichment — pure-core scale)."""
    import frontmatter as fm

    from tiro.anchors import content_hash
    from tiro.database import get_connection

    body = f"# Seeded\n\nBody for {uid}."
    post = fm.Post(body)
    post.metadata = {"title": f"Seeded {uid[-2:]}", "url": url}
    name = f"seed-{uid[-4:]}.md"
    path = config.articles_dir / name
    path.write_text(fm.dumps(post))
    conn = get_connection(config.db_path)
    try:
        # body_hash stamped by RE-LOADING the written file, mirroring
        # migration 015's / ingest's stamping semantics.
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, "
            "url, body_hash, ingestion_method, vector_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'external', 'pending')",
            (uid, source_id, f"Seeded {uid[-2:]}", name[:-3], name, url,
             content_hash(fm.load(str(path)).content)))
        conn.commit()
    finally:
        conn.close()


def _seed_source(config, uid, name, domain):
    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sources (uid, name, domain, source_type, is_vip, "
            "created_at) VALUES (?, ?, ?, 'web', 0, ?)",
            (uid, name, domain, "2026-07-01 00:00:00"))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@st.composite
def st_state_ops(draw):
    """Random op batch over the closed uid universe: line puts/dels + meta
    ops, HLC-stamped from per-device clocks (per-device order preserved)."""
    from tiro.migrations import new_ulid

    ops = []
    clocks = {d: HLCClock(d, now_ms=lambda: draw(
        st.integers(1, 10**9))) for d in DEVICES}
    n = draw(st.integers(0, 10))
    for _ in range(n):
        device = draw(st.sampled_from(DEVICES))
        hlc = clocks[device].tick()
        which = draw(st.integers(0, 2))
        if which == 0:
            ln = draw(st_line())
            ln["article_uid"] = draw(st.sampled_from(ART_UIDS[:2]))
            ops.append(LinePut(op_id=new_ulid(), hlc=hlc, device=device,
                               uid=ln["uid"], article_uid=ln["article_uid"],
                               line=ln))
        elif which == 1:
            ops.append(LineDel(op_id=new_ulid(), hlc=hlc, device=device,
                               uid=draw(st.sampled_from(HL_UIDS)),
                               article_uid=draw(st.sampled_from(ART_UIDS[:2])),
                               observed_updated_at=draw(
                                   st.one_of(st.none(), st_ts))))
        else:
            ops.append(Meta(op_id=new_ulid(), hlc=hlc, device=device,
                            uid=draw(st.sampled_from(ART_UIDS[:2])),
                            field=draw(st.sampled_from(
                                ("rating", "is_read", "snoozed_until",
                                 "opened_count"))),
                            value=draw(st.sampled_from(
                                (None, 0, 1, 2, "2026-07-12T00:00:00Z"))),
                            ts=draw(st_ts)))
    return ops


def _observable_state(config):
    """Everything convergence is judged on: article meta + sidecar lines
    (with note text) + highlight rows."""
    from tiro.annotations import read_annotations
    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    try:
        arts = {r["uid"]: (r["rating"], r["is_read"], r["snoozed_until"],
                           r["opened_count"])
                for r in conn.execute(
                    "SELECT uid, rating, is_read, snoozed_until, opened_count "
                    "FROM articles")}
        hls = {r["uid"]: (r["color"], r["quote_text"])
               for r in conn.execute("SELECT uid, color, quote_text "
                                     "FROM highlights")}
    finally:
        conn.close()
    sidecars = {}
    an_dir = config.library / "annotations"
    if an_dir.exists():
        for p in sorted(an_dir.glob("*.jsonl")):
            sidecars[p.name] = read_annotations(config, p.stem)
    return (arts, hls, sidecars)


@settings(settings.get_profile("sync_apply"))
@given(st_state_ops())
def test_apply_idempotent(ops):
    from tiro.sync.merge import apply_ops

    with tempfile.TemporaryDirectory() as td:
        config = _mini_lib(Path(td), "lib")
        for i, uid in enumerate(ART_UIDS[:2]):
            _seed_article(config, uid, f"https://seed.example.com/{i}")
        apply_ops(config, ops, guard=False)
        state1 = _observable_state(config)
        apply_ops(config, ops, guard=False)
        assert _observable_state(config) == state1


def _ex_line(note, updated, color="yellow"):
    """Concrete line for pinned @example regressions."""
    return {"uid": HL_UIDS[0], "article_uid": ART_UIDS[0], "quote": "q",
            "prefix": "", "suffix": "", "position_start": 0,
            "position_end": 1, "content_hash": "a" * 64, "color": color,
            "note_markdown": note, "created_at": "2026-07-01T00:00:00Z",
            "updated_at": updated}


@settings(settings.get_profile("sync_apply"))
@given(st_state_ops(), st_state_ops())
@example(
    # Pinned S2.8 counterexample (cross-field meta divergence): articles
    # carry ONE meta_updated_at, and gating every meta field on it coupled
    # the fields — on the device that saw is_read(ts=9) first, rating(ts=5)
    # was skipped as "stale"; on the other it applied. Fixed by per-field
    # LWW clocks (sync_shadow kind='metats').
    ops_a=[Meta(op_id="01OPMETA00000000000000000A", hlc=HLC(5, 0, "deva"),
                device="deva", uid=ART_UIDS[0], field="rating", value=2,
                ts="2026-07-05T00:00:00Z")],
    ops_b=[Meta(op_id="01OPMETA00000000000000000B", hlc=HLC(9, 0, "devb"),
                device="devb", uid=ART_UIDS[0], field="is_read", value=1,
                ts="2026-07-09T00:00:00Z")],
)
@example(
    # Pinned S2.8 counterexample (stale-put content drop): a line_put whose
    # HLC is older than the watermark but whose updated_at is NEWER was
    # skipped outright on the device that saw the hlc-newer put first, and
    # LWW-merged on the device that saw it last — diverging on color and
    # note. A stale put now still FOLDS (content) without advancing the
    # watermark (liveness).
    ops_a=[LinePut(op_id="01OPLINE00000000000000000A", hlc=HLC(9, 0, "deva"),
                   device="deva", uid=HL_UIDS[0], article_uid=ART_UIDS[0],
                   line=_ex_line(None, "2026-07-05T00:00:00Z", color="pink"))],
    ops_b=[LinePut(op_id="01OPLINE00000000000000000B", hlc=HLC(5, 0, "devb"),
                   device="devb", uid=HL_UIDS[0], article_uid=ART_UIDS[0],
                   line=_ex_line("precious", "2026-07-09T00:00:00Z"))],
)
def test_apply_order_independent_across_devices(ops_a, ops_b):
    """Two devices' batches applied in either order converge (spec §9's
    commutativity across op reorderings per device pair). Libraries start
    clean-shadow (no un-diffed local edits), so resolution is fully
    deterministic — the pseudo-HLC concurrency path has its own example
    tests in test_sync_apply_files.py."""
    from tiro.sync.merge import apply_ops

    with tempfile.TemporaryDirectory() as td:
        c1 = _mini_lib(Path(td), "lib1")
        c2 = _mini_lib(Path(td), "lib2")
        for c in (c1, c2):
            for i, uid in enumerate(ART_UIDS[:2]):
                _seed_article(c, uid, f"https://seed.example.com/{i}")
        apply_ops(c1, ops_a, guard=False)
        apply_ops(c1, ops_b, guard=False)
        apply_ops(c2, ops_b, guard=False)
        apply_ops(c2, ops_a, guard=False)
        assert _observable_state(c1) == _observable_state(c2)


@settings(settings.get_profile("sync_apply"))
@given(st_state_ops())
@example(
    # Pinned S2.8 counterexample (observed delete destroyed the note): a
    # line_del whose observed_updated_at covered the line's updated_at
    # deleted the note SILENTLY — no conflict note file. A synced delete
    # now always preserves a non-empty note (retention over tidiness).
    ops=[LinePut(op_id="01OPLINE00000000000000000C", hlc=HLC(5, 0, "deva"),
                 device="deva", uid=HL_UIDS[0], article_uid=ART_UIDS[0],
                 line=_ex_line("precious", "2026-07-05T00:00:00Z")),
         LineDel(op_id="01OPLINE00000000000000000D", hlc=HLC(9, 0, "devb"),
                 device="devb", uid=HL_UIDS[0], article_uid=ART_UIDS[0],
                 observed_updated_at="2026-07-05T00:00:00Z")],
)
def test_apply_never_loses_note_text(ops):
    """No-note-loss at APPLY level (tombstone+resurrect matrix included):
    every note in a line_put that is not superseded by a NEWER line_put of
    the same uid appears in the library afterwards — in a sidecar line, a
    notes row, or a conflict note file."""
    from tiro.sync.merge import apply_ops

    with tempfile.TemporaryDirectory() as td:
        config = _mini_lib(Path(td), "lib")
        for i, uid in enumerate(ART_UIDS[:2]):
            _seed_article(config, uid, f"https://seed.example.com/{i}")
        apply_ops(config, ops, guard=False)

        arts, hls, sidecars = _observable_state(config)
        pool = ["\n".join((ln.get("note_markdown") or "")
                          for lines in sidecars.values() for ln in lines)]
        notes_root = config.library / "notes"
        if notes_root.exists():
            pool += [p.read_text() for p in notes_root.glob("*.md")]
        haystack = "\n".join(pool)

        latest: dict[str, LinePut] = {}
        for op in ops:
            if isinstance(op, LinePut):
                cur = latest.get(op.uid)
                if cur is None or (op.line.get("updated_at") or "") >= \
                        (cur.line.get("updated_at") or ""):
                    latest[op.uid] = op
        for op in latest.values():
            note = op.line.get("note_markdown")
            if note and note.strip():
                assert note in haystack, f"lost note {note!r} (uid {op.uid})"


def test_diff_apply_roundtrip(tmp_path):
    """diff∘apply round-trip (spec §9): ops diffed from library A against an
    empty shadow, applied to empty library B, reproduce A's manifest
    (modulo shadow hlc).

    Drift adaptation vs the plan's listing: A's articles are seeded WITH a
    source row (uid + domain matching the seed URLs) — apply-side
    materialization on B resolves a source from the url domain
    (merge._source_for), so a source-less A could never round-trip: B would
    grow a row:sources entry A never had. Seeding the source makes the diff
    emit the row:sources RowPut and the source_uid Meta, exercising the
    round-trip end-to-end."""
    from tiro.sync.journal import HLCClock
    from tiro.sync.manifest import Shadow, build_manifest, diff, hydrate_bodies
    from tiro.sync.merge import apply_ops

    a = _mini_lib(tmp_path, "a")
    b = _mini_lib(tmp_path, "b")
    src_uid = "01SRCUID00000000000000000A"
    src_id = _seed_source(a, src_uid, "seed.example.com", "seed.example.com")
    for i, uid in enumerate(ART_UIDS[:3]):
        _seed_article(a, uid, f"https://seed.example.com/{i}", source_id=src_id)
    ops = diff(build_manifest(a), Shadow(),
               clock=HLCClock("deva", now_ms=lambda: 1720000000000))
    ops = hydrate_bodies(a, ops)
    report = apply_ops(b, ops, guard=False)
    assert report.errors == 0
    ma = {k: (e.hash, e.fields) for k, e in build_manifest(a).entries.items()}
    mb = {k: (e.hash, e.fields) for k, e in build_manifest(b).entries.items()}
    # meta_updated_at may be stamped on B by meta application; normalize it.
    def norm(m):
        out = {}
        for k, (h, f) in m.items():
            f = dict(f)
            f.pop("meta_updated_at", None)
            out[k] = (h, f)
        return out
    assert norm(mb) == norm(ma)

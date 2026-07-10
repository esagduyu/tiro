"""Tests for tiro.model_cache.seed_embedding_model_cache (M5.0 spike, spec D1).

The seed function copies a bundled all-MiniLM-L6-v2 snapshot into the standard
HF hub cache iff absent, so a frozen desktop binary reaches a working, offline
embedding model on first launch through the completely normal
init_vectorstore code path. Tested purely with tmp dirs + env monkeypatch.
"""

from pathlib import Path

from tiro.model_cache import (
    MODEL_CACHE_DIRNAME,
    hf_hub_cache_dir,
    seed_embedding_model_cache,
)


def _make_fake_bundle(bundle_root: Path) -> Path:
    """Build a minimal but structurally-correct bundled model tree:
    bundle_root/models--sentence-transformers--all-MiniLM-L6-v2/{refs/main, snapshots/<rev>/config.json}
    """
    model = bundle_root / MODEL_CACHE_DIRNAME
    rev = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    snap = model / "snapshots" / rev
    snap.mkdir(parents=True)
    (snap / "config.json").write_text('{"model_type": "bert"}')
    (snap / "model.safetensors").write_bytes(b"\x00" * 1024)
    refs = model / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text(rev)
    return bundle_root


def test_hf_hub_cache_dir_honors_hf_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert hf_hub_cache_dir() == tmp_path / "hf" / "hub"


def test_hf_hub_cache_dir_honors_hf_hub_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "custom"))
    assert hf_hub_cache_dir() == tmp_path / "custom"


def test_copies_when_absent(monkeypatch, tmp_path):
    bundle = _make_fake_bundle(tmp_path / "bundle")
    cache = tmp_path / "hf"
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(cache))

    copied = seed_embedding_model_cache(bundle)

    assert copied is True
    dest = cache / "hub" / MODEL_CACHE_DIRNAME
    assert dest.is_dir()
    assert (dest / "refs" / "main").read_text() == "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    assert (dest / "snapshots").exists()
    # real content came across
    rev = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    assert (dest / "snapshots" / rev / "config.json").read_text() == '{"model_type": "bert"}'


def test_noop_when_present(monkeypatch, tmp_path):
    bundle = _make_fake_bundle(tmp_path / "bundle")
    cache = tmp_path / "hf"
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(cache))

    # Pre-create the cached model dir with a sentinel we can detect wasn't clobbered.
    dest = cache / "hub" / MODEL_CACHE_DIRNAME
    dest.mkdir(parents=True)
    (dest / "sentinel.txt").write_text("preexisting")

    copied = seed_embedding_model_cache(bundle)

    assert copied is False
    # existing cache untouched — no overwrite, sentinel survives, no bundle files copied in
    assert (dest / "sentinel.txt").read_text() == "preexisting"
    assert not (dest / "refs").exists()


def test_missing_bundle_dir_returns_false(monkeypatch, tmp_path):
    cache = tmp_path / "hf"
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(cache))

    # bundle path does not exist at all
    assert seed_embedding_model_cache(tmp_path / "does-not-exist") is False
    # bundle exists but lacks the model subdir
    empty = tmp_path / "empty-bundle"
    empty.mkdir()
    assert seed_embedding_model_cache(empty) is False
    # nothing was created in the cache
    assert not (cache / "hub" / MODEL_CACHE_DIRNAME).exists()


def test_never_raises_on_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    # None bundle dir must be swallowed, not raised
    assert seed_embedding_model_cache(None) is False

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the Tiro server binary (Phase 5 / M5.0, spec D1).

Builds `dist/tiro-server/` — a full, offline-capable FastAPI server (real
ChromaDB, real sentence-transformers embedding) that a Tauri sidecar launches
with env only. onedir (not onefile): no self-extraction latency per launch, and
Tauri's sidecar mechanism handles a directory fine via a wrapper.

Every non-obvious addition below is annotated WHY (the spike's whack-a-mole log
lives in the task report; the load-bearing ones are pinned here so a future
rebuild doesn't silently drop them).

Build:  uv run --group packaging pyinstaller desktop/pyinstaller/tiro-server.spec
"""

import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPEC_DIR = Path(SPECPATH)
REPO_ROOT = SPEC_DIR.parent.parent

# --- Application data files (templates, static assets, packaged prompt/schema
# markdown). These are non-.py files inside the `tiro` package that PyInstaller
# would NOT pick up from the module graph, so they're staged explicitly at the
# same `tiro/...` relative paths the code resolves via Path(__file__).parent. ---
tiro_data = []
_data_globs = (
    "**/*.md", "**/*.html", "**/*.css", "**/*.js", "**/*.json",
    "**/*.txt", "**/*.webmanifest", "**/*.png", "**/*.svg", "**/*.ico",
)
_tiro_src = REPO_ROOT / "tiro"
for pattern in _data_globs:
    for f in _tiro_src.rglob(pattern):
        if "__pycache__" in f.parts:
            continue
        rel_parent = f.parent.relative_to(REPO_ROOT)
        tiro_data.append((str(f), str(rel_parent)))

# --- Bundled embedding model (spec D1: BUNDLE + seed-the-cache). Stage the
# refs/main snapshot of all-MiniLM-L6-v2 from THIS machine's HF cache into
# `hf_model/models--sentence-transformers--all-MiniLM-L6-v2/...`. entry.py
# copies this into the user's HF hub cache on first launch, and it MUST be a
# valid HF hub layout that loads with `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`
# and an empty HF_HOME — a genuinely offline first boot is the whole point of
# bundling (spec D1). See smoke.sh's offline phase, which enforces exactly this.
#
# LOAD-BEARING SUBTLETY (a real regression this replaces): a PyInstaller datas
# tuple is `(source, dest_DIR)` — the file is staged at `dest_dir/basename(source)`.
# The snapshot files (`snapshots/<rev>/config.json`, ...) are symlinks into
# `blobs/<hash>`. If we hand PyInstaller `(f.resolve(), dest)` the source path is
# the BLOB, so `basename` is the blob HASH — every snapshot file lands under a
# hash name and the snapshot dir no longer contains `config.json`/`tokenizer.json`.
# HF then can't resolve the snapshot offline and tries to reach huggingface.co,
# which crashes an offline first boot. Fix: copy each refs/main snapshot file to
# a build-staging tree under its TRUE name with the symlink's REAL bytes
# (shutil.copyfile dereferences content but we control the destination name),
# then hand PyInstaller the staged files (correct basenames). We ship ONLY the
# snapshot tree (as real files) + refs/main — NOT the `blobs/` dir (redundant
# once snapshots are real files) and NOT the `.no_exist/` network markers (a
# complete snapshot needs none of them offline). ---
MODEL_DIRNAME = "models--sentence-transformers--all-MiniLM-L6-v2"
_hf_hub = Path.home() / ".cache" / "huggingface" / "hub" / MODEL_DIRNAME
model_data = []
if not _hf_hub.is_dir():
    raise SystemExit(
        f"Embedding model not found in HF cache at {_hf_hub}. "
        "Prime it first: `uv run python -c \"from tiro.vectorstore import init_vectorstore; "
        "import tempfile,pathlib; init_vectorstore(pathlib.Path(tempfile.mkdtemp()))\"`"
    )
_ref_main = _hf_hub / "refs" / "main"
if not _ref_main.exists():
    raise SystemExit(f"HF cache at {_hf_hub} has no refs/main — reprime the model.")
_keep_rev = _ref_main.read_text().strip()
_snapshot_dir = _hf_hub / "snapshots" / _keep_rev
if not _snapshot_dir.is_dir():
    raise SystemExit(f"HF cache snapshot {_snapshot_dir} missing — reprime the model.")

# Build-staging tree lives under the (git-ignored) workpath; recreated each run.
# Files are copied under their true names so PyInstaller's basename == real name.
_stage = SPEC_DIR / "build" / "hf_stage" / MODEL_DIRNAME
if _stage.exists():
    shutil.rmtree(_stage)
(_stage / "refs").mkdir(parents=True, exist_ok=True)
shutil.copyfile(_ref_main, _stage / "refs" / "main")
for f in _snapshot_dir.rglob("*"):
    if not f.is_file():  # follows symlink; True for the linked-to blob files
        continue
    _rel = f.relative_to(_snapshot_dir)  # e.g. config.json OR 1_Pooling/config.json
    _target = _stage / "snapshots" / _keep_rev / _rel
    _target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(f, _target)  # real bytes, TRUE name preserved
for f in _stage.rglob("*"):
    if not f.is_file():
        continue
    rel_parent = Path("hf_model") / MODEL_DIRNAME / f.relative_to(_stage).parent
    model_data.append((str(f), str(rel_parent)))

# --- Heavy third-party packages with native libs / dynamic imports / data
# files. collect_all pulls binaries + datas + hiddenimports in one shot; this
# is the well-trodden path for exactly these packages (spec D1 names them). ---
datas = list(tiro_data) + list(model_data)
binaries = []
hiddenimports = []

for pkg in (
    "chromadb",            # native chroma_hnswlib, dynamic provider imports, sqlite vec
    "sentence_transformers",
    "transformers",        # ST pulls model/config classes dynamically
    "tokenizers",          # rust-backed, data files
    "huggingface_hub",     # snapshot resolution
    "torch",               # enormous; native libs
    "safetensors",
    "tqdm",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# ChromaDB reaches for these dynamically (telemetry, query grammar, config
# validation); enumerate as hidden imports so the module graph doesn't miss
# them. onnxruntime is chromadb's DEFAULT embedding function — we use the
# SentenceTransformer EF instead, but chromadb still imports paths that touch
# these, so include the light ones.
hiddenimports += collect_submodules("chromadb")
hiddenimports += [
    "hnswlib",
    "pypika",
    "posthog",
    "importlib_resources",
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
]

# uvicorn/anthropic/tiro plumbing that may be reached via strings/dynamic import.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("tiro")

block_cipher = None

a = Analysis(
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Intentionally empty: nothing is excluded. (An earlier note here claimed
    # onnxruntime — chromadb's default EF, which we don't use — was excluded to
    # trim size; it never was, `excludes=[]` applies nothing. chromadb import
    # paths still touch onnxruntime, so a blanket exclude risks breaking import;
    # size is reported, not fought, per spec D1. Trimming is a later pass.)
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tiro-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="tiro-server",
)

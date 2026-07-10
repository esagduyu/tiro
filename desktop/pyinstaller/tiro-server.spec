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
# `hf_model/models--sentence-transformers--all-MiniLM-L6-v2/...`, dereferencing
# the HF blob symlinks into real files so the frozen bundle is self-contained.
# entry.py copies this into the user's HF hub cache on first launch. ---
MODEL_DIRNAME = "models--sentence-transformers--all-MiniLM-L6-v2"
_hf_hub = Path.home() / ".cache" / "huggingface" / "hub" / MODEL_DIRNAME
model_data = []
if _hf_hub.is_dir():
    ref_main = (_hf_hub / "refs" / "main")
    keep_rev = ref_main.read_text().strip() if ref_main.exists() else None
    for f in _hf_hub.rglob("*"):
        if not f.is_file():
            continue
        # Only ship the refs/main snapshot (skip other revisions) to avoid
        # dereferencing shared blobs into multiple full-size copies.
        parts = f.relative_to(_hf_hub).parts
        if keep_rev and parts and parts[0] == "snapshots" and len(parts) > 1 and parts[1] != keep_rev:
            continue
        rel_parent = ("hf_model" / f.relative_to(_hf_hub.parent).parent)
        model_data.append((str(f.resolve()), str(rel_parent)))  # resolve() follows symlink -> real file
else:
    raise SystemExit(
        f"Embedding model not found in HF cache at {_hf_hub}. "
        "Prime it first: `uv run python -c \"from tiro.vectorstore import init_vectorstore; "
        "import tempfile,pathlib; init_vectorstore(pathlib.Path(tempfile.mkdtemp()))\"`"
    )

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
    # onnxruntime is chromadb's default EF that we never use; excluding it
    # trims a large native dep. If chromadb import breaks, drop this line.
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

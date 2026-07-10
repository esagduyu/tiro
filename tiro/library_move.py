"""`tiro migrate-library` — copy-then-confirm-NEVER-remove library migration
(spec D3, the data-loss-risk surface).

The tool NEVER deletes, renames, or moves the source library. Ever. It copies
the entire library to a new location, verifies the copy byte-for-byte, and only
then re-points config.yaml at the new path. The old copy stays on disk; the user
removes it themselves after verifying. Any failure mid-protocol leaves BOTH
libraries valid: the source is untouched, and a partial destination is either
cleaned (on a fresh re-run) or marker-flagged so the next run restarts cleanly.

**CLI-only, server stopped** — same posture as `tiro restore`: copying a live
ChromaDB/SQLite mid-write corrupts it. Because the server is stopped, ChromaDB
is copied *verbatim* (a bit-identical directory copy under the no-live-writer
guarantee) rather than rebuilt from a portable dump the way `restore` must —
restore's dump/rebuild exists only because a portable snapshot never contains
ChromaDB internals; here we have the real directory and no concurrent writer.

Protocol (spec D3), in order:
1. `auto_backup(config, "library-migrate")` FIRST — a failed backup ABORTS
   (the one caller where "best-effort" isn't good enough).
2. Destination must be empty or absent, EXCEPT an interrupted prior run
   (contains our `.tiro-migrate-incomplete` marker) → cleared and restarted.
   A non-empty dest without the marker aborts (never merge into unknown data).
3. Write the marker FIRST, then copy the whole library. A `config.yaml` living
   inside the library dir is excluded (it would be a stale-path doppelgänger;
   the live config at `config.config_path` is updated in place at step 5).
4. Verify: relative-path set equality + per-file size equality. Mismatch →
   abort, keep the marker (clean re-run), config unchanged. Success → drop marker.
5. `persist_config(config, {"library_path": str(dest)})` — the ONLY config write.
6. Report the source is preserved; the user removes it manually.
"""

import logging
import shutil
from pathlib import Path

from tiro.backup import auto_backup
from tiro.config import TiroConfig, persist_config

logger = logging.getLogger(__name__)

MARKER_NAME = ".tiro-migrate-incomplete"
_EXCLUDED_TOP = "config.yaml"


class MigrationError(Exception):
    """A migration was refused or aborted. The source is always intact."""


def _relwalk(root: Path, exclude_top: set[str]) -> dict[str, int]:
    """{relative-posix-path: size} for every file under root, skipping any
    top-level entry named in exclude_top."""
    out: dict[str, int] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in exclude_top:
            continue
        out[rel.as_posix()] = p.stat().st_size
    return out


def migrate_library(
    config: TiroConfig,
    dest: str | Path,
    *,
    assume_yes: bool = False,
    _pre_verify_hook=None,
) -> dict:
    """Copy `config.library` to `dest`, verify, then re-point config at it.

    Returns a report dict: {status, source, dest, files_copied, bytes_copied,
    snapshot}. `status` is "migrated" on success or "already_at_destination"
    for a no-op (dest == source / re-run after success). Raises MigrationError
    on any refusal or verification failure — the source is intact in every case.

    `_pre_verify_hook(dest_path)` is a test seam invoked after the copy and
    before verification (used to inject a verify mismatch); production callers
    never pass it.
    """
    source = config.library  # resolves symlinks via TiroConfig.library
    dest = Path(dest).resolve()

    report = {
        "status": None,
        "source": str(source),
        "dest": str(dest),
        "files_copied": 0,
        "bytes_copied": 0,
        "snapshot": None,
    }

    # --- Refusals (cheap, no side effects) ---------------------------------
    if dest == source:
        # Both the dest==source mistake AND the re-run-after-success case: the
        # config already points here. A clean no-op, never a destructive action.
        report["status"] = "already_at_destination"
        return report

    if dest.is_relative_to(source) or source.is_relative_to(dest):
        raise MigrationError(
            f"Destination {dest} and source {source} overlap — the copy would "
            "recurse into itself. Choose a location outside the library."
        )

    marker = dest / MARKER_NAME

    # --- Destination handling (spec step 2) --------------------------------
    interrupted = False
    if dest.exists():
        if not dest.is_dir():
            raise MigrationError(f"Destination {dest} exists and is not a directory.")
        entries = [p for p in dest.iterdir()]
        if entries:
            if marker.exists():
                interrupted = True  # our own partial run — safe to clear
            else:
                raise MigrationError(
                    f"Destination {dest} is not empty and is not a detected "
                    "interrupted migration — refusing to merge into unknown data."
                )

    # --- Step 1: snapshot FIRST (abort on failure) -------------------------
    snapshot = auto_backup(config, "library-migrate")
    if snapshot is None:
        raise MigrationError(
            "Pre-migration backup failed — aborting before any copy "
            "(source left untouched)."
        )
    report["snapshot"] = str(snapshot)

    # Clear a detected interrupted run now (source was never touched, so
    # clearing the partial destination is always safe).
    if interrupted:
        shutil.rmtree(dest)

    # --- Step 3: marker FIRST, then copy the whole library -----------------
    dest.mkdir(parents=True, exist_ok=True)
    marker.write_text("Tiro library migration in progress. Safe to delete.\n")

    def _ignore(dirpath, names):
        # Exclude a top-level config.yaml (Docker layout doppelgänger) only.
        if Path(dirpath).resolve() == source and _EXCLUDED_TOP in names:
            return {_EXCLUDED_TOP}
        return set()

    shutil.copytree(source, dest, ignore=_ignore, dirs_exist_ok=True, symlinks=False)

    # --- Step 4: verify (path-set + per-file size equality) ----------------
    if _pre_verify_hook is not None:
        _pre_verify_hook(dest)

    src_files = _relwalk(source, exclude_top={_EXCLUDED_TOP})
    dst_files = _relwalk(dest, exclude_top={MARKER_NAME})
    if src_files != dst_files:
        missing = set(src_files) - set(dst_files)
        extra = set(dst_files) - set(src_files)
        size_diff = {
            k for k in src_files.keys() & dst_files.keys()
            if src_files[k] != dst_files[k]
        }
        # Keep the marker so a re-run restarts cleanly; config stays unchanged.
        raise MigrationError(
            "Verification failed — the copy does not match the source. "
            f"missing={sorted(missing)} extra={sorted(extra)} "
            f"size_mismatch={sorted(size_diff)}. Source untouched; destination "
            f"marker retained at {marker} for a clean re-run."
        )

    marker.unlink()
    report["files_copied"] = len(dst_files)
    report["bytes_copied"] = sum(dst_files.values())

    # --- Step 5: the ONLY config write -------------------------------------
    persist_config(config, {"library_path": str(dest)})
    report["status"] = "migrated"

    logger.info(
        "Migrated library %s -> %s (%d files, %d bytes; snapshot: %s). "
        "Source preserved.",
        source, dest, report["files_copied"], report["bytes_copied"], snapshot,
    )
    return report

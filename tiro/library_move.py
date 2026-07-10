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

The in-migration `auto_backup(config, "library-migrate")` runs from the CLI with
no live server, so its portable `embeddings.jsonl` is written empty (the derived
ChromaDB dump has no in-process collection to read); this is fine — that snapshot
is a rollback safety net for the SQLite/markdown/sidecar side, and `tiro restore`
re-embeds from markdown anyway. The verbatim `chroma/` directory copy carries the
real vectors to the destination.

Protocol (spec D3), in order:
1. `auto_backup(config, "library-migrate")` FIRST — a failed backup ABORTS
   (the one caller where "best-effort" isn't good enough); a backup DISABLED by
   config (`backup_auto_keep <= 0`) aborts with a distinct message so the user
   knows to enable auto-backups rather than reading it as a failure.
2. Destination must be empty or absent, EXCEPT an interrupted prior run
   (contains our `.tiro-migrate-incomplete` marker) → cleared and restarted.
   A non-empty dest without the marker aborts (never merge into unknown data).
3. Write the marker FIRST, then copy the whole library. A `config.yaml` living
   inside the library dir is excluded from the bulk copy; if it is the LIVE
   config (`config.config_path` resolves inside the library — the platform
   default layout, where config lives at `<library>/config.yaml`, spec D7), it
   is relocated into the destination at step 5 so deleting the old library
   later can't take the live config with it. A stray inside-library config that
   is NOT the live one stays a stale-path doppelgänger and is simply dropped.
4. Verify: relative-path set equality + per-file size equality. Mismatch →
   abort, keep the marker (clean re-run), config unchanged. Success → drop
   marker. Internal DIRECTORY symlinks are refused up front (step 0): copytree
   follows them but the verify walk cannot see through them, which would fail
   verification permanently on every re-run.
5. `persist_config(config, {"library_path": str(dest)})` — the only config
   write — then, when the live config lived inside the library, copy it into the
   destination and re-point `config.config_path`. Crash window (documented, not
   fixed): a crash AFTER `marker.unlink()` but BEFORE `persist_config` leaves a
   complete, marker-less destination while config still points at the source; a
   later re-run then sees a non-empty dest without a marker and refuses as a
   "foreign dest" — recover by removing the (verified-complete) destination and
   re-running, or by hand-editing `library_path`. The source is intact either way.
6. Report the source is preserved; the user removes it manually.
"""

import logging
import os
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


def _find_internal_dir_symlinks(root: Path) -> list[str]:
    """Directory symlinks anywhere under `root`.

    `copytree(symlinks=False)` FOLLOWS a directory symlink (copying the target's
    contents as real files at the destination), but `Path.rglob` deliberately
    does NOT descend into symlinked directories — so `_relwalk` under-counts the
    source while the destination over-counts, producing a permanent, misdiagnosed
    "extra files" verify mismatch on every re-run. Detect and refuse up front.
    (File symlinks are safe: copytree and rglob both resolve them to the target,
    so both sides agree.)"""
    out: list[str] = []
    for p in root.rglob("*"):
        if p.is_symlink() and p.is_dir():
            out.append(p.relative_to(root).as_posix())
    return out


def migrate_library(
    config: TiroConfig,
    dest: str | Path,
    *,
    _pre_verify_hook=None,
) -> dict:
    """Copy `config.library` to `dest`, verify, then re-point config at it.

    Returns a report dict: {status, source, dest, files_copied, bytes_copied,
    snapshot, config_relocated_to}. `status` is "migrated" on success or
    "already_at_destination" for a no-op (dest == source / re-run after
    success). `config_relocated_to` is the destination path of the live config
    when it lived inside the library and was relocated (else None). Raises
    MigrationError on any refusal or verification failure — the source is intact
    in every case.

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
        "config_relocated_to": None,
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

    # Step 0: internal directory symlinks would fail verification forever.
    internal_links = _find_internal_dir_symlinks(source)
    if internal_links:
        raise MigrationError(
            "The library contains internal directory symlink(s) that a copy "
            "would follow but verification cannot see through, which would fail "
            f"verification permanently: {sorted(internal_links)}. Resolve or "
            "remove these symlinks (replace with real directories) before "
            "migrating."
        )

    # Detect whether the LIVE config file lives inside the library (the platform
    # default layout, spec D7: config at <library>/config.yaml). If so it must be
    # relocated into the destination at step 5 — otherwise deleting the old
    # library later would delete the live config with it.
    config_inside = False
    cfg_path: Path | None = None
    if config.config_path:
        cfg_path = Path(config.config_path).resolve()
        config_inside = cfg_path.is_relative_to(source)

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
        if config.backup_auto_keep <= 0:
            raise MigrationError(
                "Pre-migration backup is DISABLED by config (backup_auto_keep=0). "
                "Enable auto-backups (set backup_auto_keep > 0) so the migration "
                "can take its safety snapshot — aborting before any copy "
                "(source left untouched)."
            )
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
        # Exclude a top-level config.yaml (Docker doppelgänger, or the live
        # config we relocate explicitly at step 5) from the bulk copy only.
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

    # --- Step 5: the config write(s) ---------------------------------------
    # Persist the new library_path into the CURRENT config file (still at the old
    # path). See the crash-window note in the module docstring: a crash between
    # marker.unlink() and here leaves a complete, marker-less dest with config
    # still pointing at source — recoverable, never data-losing.
    persist_config(config, {"library_path": str(dest)})

    # If the live config lived inside the library, relocate it into the
    # destination (D7 layout) and re-point config.config_path so removing the old
    # library can't take the live config with it.
    if config_inside and cfg_path is not None:
        rel = cfg_path.relative_to(source)
        new_cfg = dest / rel
        new_cfg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg_path, new_cfg)
        try:
            os.chmod(new_cfg, 0o600)
        except OSError:
            pass
        config.config_path = str(new_cfg)
        report["config_relocated_to"] = str(new_cfg)
        logger.info("Relocated live config %s -> %s", cfg_path, new_cfg)

    report["status"] = "migrated"

    logger.info(
        "Migrated library %s -> %s (%d files, %d bytes; snapshot: %s). "
        "Source preserved.",
        source, dest, report["files_copied"], report["bytes_copied"], snapshot,
    )
    return report

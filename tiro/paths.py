"""Platform-default library/config paths (Phase 5 D2).

Pure, side-effect-free path computation. These are the *standard* locations a
first-run `tiro init` (D2) and the Tauri launcher (D7) write into; they are NOT
the dataclass default — `TiroConfig.DEFAULTS["library_path"]` deliberately stays
`./tiro-library` (changing it would silently re-point every existing
defaults-only install at an empty directory). Platform defaults enter ONLY
through explicit writers.

Platform layout:
- macOS:   ~/Library/Application Support/Tiro
- Linux:   $XDG_DATA_HOME/tiro  (fallback ~/.local/share/tiro)
- Windows: %APPDATA%\\Tiro       (fallback ~/AppData/Roaming/Tiro)

`platform_config_path()` is `<that dir>/config.yaml` — i.e. the config lives
*inside* the library dir (the same "Docker layout" the migrate-library copy
step deliberately excludes from the copy).
"""

import os
import sys
from pathlib import Path


def platform_default_library() -> Path:
    """The OS-standard directory for a Tiro library."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Tiro"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Tiro"
    # Linux / other POSIX: XDG Base Directory spec.
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "tiro"


def platform_config_path() -> Path:
    """The OS-standard config.yaml path (lives inside the library dir)."""
    return platform_default_library() / "config.yaml"

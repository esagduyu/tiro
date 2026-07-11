"""Background service management — `tiro service install|uninstall|status|logs`.

Spec D8. Targets the **CLI/uv install** of Tiro (the Tauri desktop app owns its
own sidecar lifecycle — mixing the two is documented as unsupported; install one
or the other). Platform dispatch by `sys.platform`:

- macOS: a launchd user agent at ``~/Library/LaunchAgents/com.tiro.app.plist``
  (``launchctl load/unload -w``), logging to ``~/Library/Logs/Tiro/tiro.log``.
- Linux: a systemd **user** unit at ``~/.config/systemd/user/tiro.service``
  (``systemctl --user enable --now``), logging to the journal.
- Windows: documented-not-built (ON-9 Q2) — ``install`` prints the nssm recipe
  and exits 1; every subcommand is a clear "not supported" + exit 1.

**Design for testability (the fake-manager seam):** everything that touches the
OS is injectable on :class:`ServiceController` — ``platform`` (dispatch),
``home`` (where files land), ``runner`` (the ``subprocess.run`` stand-in — tests
capture launchctl/systemctl invocations and NOTHING is ever executed), and
``probe`` (the /healthz check). The generated plist/unit CONTENT is produced by
pure builder functions and verified by parsing it back. A real install is never
performed by the test suite; the live launchd round-trip is an owner-runbook
step (D9).

ON-8: the ``--config`` path baked into the service file is resolved to an
ABSOLUTE path so a launchd/systemd process (which runs with an arbitrary CWD)
never falls back to a repo-relative ``./config.yaml``.
"""

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.tiro.app"
LINUX_UNIT = "tiro.service"


# --- pure content builders ---------------------------------------------------

def build_launchd_plist(
    executable: str, config_path: str, log_path: str, working_dir: str
) -> bytes:
    """Standard launchd user-agent plist. RunAtLoad + KeepAlive make it a
    survives-login, restart-on-crash service; stdout/stderr both go to the log
    file so `tiro service logs` has something to tail."""
    data = {
        "Label": LABEL,
        "ProgramArguments": [
            executable,
            "--config",
            config_path,
            "run",
            "--no-browser",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "WorkingDirectory": working_dir,
    }
    return plistlib.dumps(data)


def build_systemd_unit(executable: str, config_path: str) -> str:
    """A systemd **user** service unit. `Restart=on-failure` mirrors launchd's
    KeepAlive; `WantedBy=default.target` makes `enable` start it at user login."""
    return (
        "[Unit]\n"
        "Description=Tiro — reading OS for the AI age\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={executable} --config {config_path} run --no-browser\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _healthz_probe(config) -> tuple[bool, str | None]:
    """Best-effort local /healthz check for `status`. Never raises."""
    import json
    import urllib.request

    url = f"http://127.0.0.1:{config.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 (loopback)
            body = json.loads(resp.read().decode("utf-8"))
        return True, body.get("version")
    except Exception:
        return False, None


class ServiceController:
    """Platform-dispatching service manager with fully injectable OS seams."""

    def __init__(self, *, platform=None, home=None, runner=None, executable=None, probe=None):
        self.platform = platform or sys.platform
        self.home = Path(home) if home else Path.home()
        self.runner = runner or subprocess.run
        self._executable = executable
        self.probe = probe or _healthz_probe

    # -- resolved locations --------------------------------------------------

    @property
    def executable(self) -> str:
        """Absolute path to the `tiro` entry point, recorded into the service
        file so PATH quirks can't break launchd/systemd (ON-8 spirit)."""
        if self._executable:
            return self._executable
        import shutil

        found = shutil.which("tiro") or sys.argv[0]
        return str(Path(found).resolve())

    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def macos_log_path(self) -> Path:
        return self.home / "Library" / "Logs" / "Tiro" / "tiro.log"

    def systemd_unit_path(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / LINUX_UNIT

    def _abs_config(self, config) -> str:
        return str(Path(config.config_path).resolve())

    # -- verbs ---------------------------------------------------------------

    def install(self, config) -> int:
        if self.platform == "darwin":
            return self._macos_install(config)
        if self.platform.startswith("linux"):
            return self._linux_install(config)
        return self._windows_install(config)

    def uninstall(self, config) -> int:
        if self.platform == "darwin":
            return self._macos_uninstall(config)
        if self.platform.startswith("linux"):
            return self._linux_uninstall(config)
        return self._windows_unsupported()

    def status(self, config) -> int:
        if self.platform == "darwin":
            return self._macos_status(config)
        if self.platform.startswith("linux"):
            return self._linux_status(config)
        return self._windows_unsupported()

    def logs(self, config, *, follow=False) -> int:
        if self.platform == "darwin":
            return self._macos_logs(config, follow=follow)
        if self.platform.startswith("linux"):
            return self._linux_logs(config, follow=follow)
        return self._windows_unsupported()

    # -- macOS ----------------------------------------------------------------

    def _macos_install(self, config) -> int:
        plist = self.plist_path()
        log = self.macos_log_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)

        content = build_launchd_plist(
            self.executable, self._abs_config(config), str(log), str(self.home)
        )
        plist.write_bytes(content)

        # Idempotent reinstall: unload any prior copy (ignore failure) before
        # loading, so launchctl doesn't reject a double-load of the label.
        self.runner(["launchctl", "unload", "-w", str(plist)], check=False,
                    capture_output=True, text=True)
        result = self.runner(["launchctl", "load", "-w", str(plist)], check=False,
                             capture_output=True, text=True)
        if getattr(result, "returncode", 0) != 0:
            print(f"launchctl load failed: {getattr(result, 'stderr', '').strip()}")
            return 1
        print(f"Installed launchd agent {LABEL}.")
        print(f"  plist: {plist}")
        print(f"  logs:  {log}")
        print("Tiro will start at login and restart if it crashes.")
        return 0

    def _macos_uninstall(self, config) -> int:
        plist = self.plist_path()
        if not plist.exists():
            print(f"Not installed (no {plist}).")
            return 0
        self.runner(["launchctl", "unload", "-w", str(plist)], check=False,
                    capture_output=True, text=True)
        plist.unlink()
        print(f"Uninstalled launchd agent {LABEL}.")
        return 0

    def _macos_status(self, config) -> int:
        result = self.runner(["launchctl", "list"], check=False,
                             capture_output=True, text=True)
        loaded = LABEL in (getattr(result, "stdout", "") or "")
        up, version = self.probe(config)
        print(f"launchd agent {LABEL}: {'loaded' if loaded else 'not loaded'}")
        if up:
            print(f"Server: responding on :{config.port} (Tiro {version})")
        else:
            print(f"Server: not responding on :{config.port}")
        return 0

    def _macos_logs(self, config, *, follow=False) -> int:
        log = self.macos_log_path()
        if not log.exists():
            print(f"No log file yet at {log} (has the service run?).")
            return 0
        cmd = ["tail", "-n", "200"]
        if follow:
            cmd.append("-f")
        cmd.append(str(log))
        self.runner(cmd, check=False)
        return 0

    # -- Linux ----------------------------------------------------------------

    def _linux_install(self, config) -> int:
        unit = self.systemd_unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(build_systemd_unit(self.executable, self._abs_config(config)))

        self.runner(["systemctl", "--user", "daemon-reload"], check=False,
                    capture_output=True, text=True)
        result = self.runner(["systemctl", "--user", "enable", "--now", LINUX_UNIT],
                             check=False, capture_output=True, text=True)
        if getattr(result, "returncode", 0) != 0:
            print(f"systemctl enable failed: {getattr(result, 'stderr', '').strip()}")
            print("(A user systemd session is required; on a headless box see "
                  "`loginctl enable-linger`.)")
            return 1
        print(f"Installed systemd user unit {LINUX_UNIT}.")
        print(f"  unit: {unit}")
        print("Tiro will start at login and restart on failure.")
        print("View logs with: tiro service logs")
        return 0

    def _linux_uninstall(self, config) -> int:
        unit = self.systemd_unit_path()
        if not unit.exists():
            print(f"Not installed (no {unit}).")
            return 0
        self.runner(["systemctl", "--user", "disable", "--now", LINUX_UNIT],
                    check=False, capture_output=True, text=True)
        unit.unlink()
        self.runner(["systemctl", "--user", "daemon-reload"], check=False,
                    capture_output=True, text=True)
        print(f"Uninstalled systemd user unit {LINUX_UNIT}.")
        return 0

    def _linux_status(self, config) -> int:
        result = self.runner(["systemctl", "--user", "status", LINUX_UNIT],
                             check=False, capture_output=True, text=True)
        out = (getattr(result, "stdout", "") or "").strip()
        active = "active (running)" in out or "Active: active" in out
        up, version = self.probe(config)
        print(f"systemd user unit {LINUX_UNIT}: {'active' if active else 'not active'}")
        if out:
            print(out)
        if up:
            print(f"Server: responding on :{config.port} (Tiro {version})")
        else:
            print(f"Server: not responding on :{config.port}")
        return 0

    def _linux_logs(self, config, *, follow=False) -> int:
        cmd = ["journalctl", "--user", "-u", LINUX_UNIT]
        if follow:
            cmd.append("-f")
        else:
            cmd.extend(["-n", "200", "--no-pager"])
        self.runner(cmd, check=False)
        return 0

    # -- Windows (documented, not built) -------------------------------------

    def _windows_install(self, config) -> int:
        exe = self.executable
        abs_cfg = self._abs_config(config)
        print("Windows service management is documented but not built into Tiro.")
        print("Use nssm (the Non-Sucking Service Manager) — https://nssm.cc :")
        print()
        print("  nssm install Tiro \"" + exe + "\" --config \"" + abs_cfg + "\" run --no-browser")
        print("  nssm set Tiro AppExit Default Restart")
        print("  nssm start Tiro")
        print()
        print("To remove:  nssm remove Tiro confirm")
        return 1

    def _windows_unsupported(self) -> int:
        print("`tiro service` is not supported on Windows (documented via nssm — "
              "run `tiro service install` for the recipe).")
        return 1


def dispatch(command, config, *, follow=False, controller=None) -> int:
    """CLI entry: route a subcommand to a (default or injected) controller."""
    controller = controller or ServiceController()
    if command == "install":
        return controller.install(config)
    if command == "uninstall":
        return controller.uninstall(config)
    if command == "status":
        return controller.status(config)
    if command == "logs":
        return controller.logs(config, follow=follow)
    raise ValueError(f"unknown service command: {command!r}")

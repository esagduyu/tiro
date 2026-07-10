"""`tiro service` CLI tests (Phase 5 M5.2, spec D8).

The whole suite runs against a FAKE service-manager seam: the launchctl/systemctl
subprocess runner is injected, so NOTHING here ever touches the real
~/Library/LaunchAgents or the user's systemd session, and no service is ever
installed on the machine running the tests. We verify the *generated file
contents* (parsed back, not string-matched) and the *dispatch logic* (which
manager command runs, in what order), with all state confined to tmp_path.

A live launchd round-trip against a scratch config is an owner-runbook
verification step (D9), never executed by CI.
"""

import plistlib
from types import SimpleNamespace

import pytest

from tiro import service


def make_config(tmp_path, port=8000):
    """Minimal config-shaped object: service only reads config_path + port."""
    cfg = tmp_path / "config.yaml"
    if not cfg.exists():
        cfg.write_text("library_path: ./lib\n")
    return SimpleNamespace(config_path=str(cfg), port=port)


class FakeRunner:
    """Stand-in for subprocess.run. Records calls, never executes anything."""

    def __init__(self):
        self.calls = []
        # keyed by a substring of the command; matched to shape stdout/returncode
        self.stdout_for = {}
        self.returncode_for = {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        stdout = ""
        returncode = 0
        for needle, out in self.stdout_for.items():
            if needle in cmd:
                stdout = out
        for needle, rc in self.returncode_for.items():
            if needle in cmd:
                returncode = rc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def cmds(self):
        return [" ".join(c) for c in self.calls]


def mac_controller(tmp_path, runner=None, probe=None):
    return service.ServiceController(
        platform="darwin",
        home=tmp_path / "home",
        runner=runner or FakeRunner(),
        executable="/usr/local/bin/tiro",
        probe=probe or (lambda config: (False, None)),
    )


def linux_controller(tmp_path, runner=None, probe=None):
    return service.ServiceController(
        platform="linux",
        home=tmp_path / "home",
        runner=runner or FakeRunner(),
        executable="/usr/local/bin/tiro",
        probe=probe or (lambda config: (False, None)),
    )


# --- plist / unit content ----------------------------------------------------

def test_launchd_plist_content(tmp_path):
    ctrl = mac_controller(tmp_path)
    config = make_config(tmp_path)
    rc = ctrl.install(config)
    assert rc == 0

    plist_path = ctrl.plist_path()
    assert plist_path.exists()
    data = plistlib.loads(plist_path.read_bytes())

    assert data["Label"] == "com.tiro.app"
    # Absolute program path baked in (PATH quirks can't break launchd).
    args = data["ProgramArguments"]
    assert args[0] == "/usr/local/bin/tiro"
    # --config <abs path> comes BEFORE the `run` subcommand (top-level arg).
    assert "--config" in args
    cfg_idx = args.index("--config")
    assert args[cfg_idx + 1] == str((tmp_path / "config.yaml").resolve())
    assert args.index("--config") < args.index("run")
    assert "--no-browser" in args
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    # Logs to a real, absolute path.
    assert data["StandardOutPath"] == str(ctrl.macos_log_path())
    assert data["StandardErrorPath"] == str(ctrl.macos_log_path())


def test_systemd_unit_content(tmp_path):
    ctrl = linux_controller(tmp_path)
    config = make_config(tmp_path)
    rc = ctrl.install(config)
    assert rc == 0

    unit_path = ctrl.systemd_unit_path()
    assert unit_path.exists()
    text = unit_path.read_text()

    abs_cfg = str((tmp_path / "config.yaml").resolve())
    assert f"ExecStart=/usr/local/bin/tiro --config {abs_cfg} run --no-browser" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text


# --- install: manager invocation + idempotence -------------------------------

def test_macos_install_loads_service(tmp_path):
    runner = FakeRunner()
    ctrl = mac_controller(tmp_path, runner=runner)
    ctrl.install(make_config(tmp_path))
    cmds = runner.cmds()
    # launchctl load -w <plist> is issued.
    assert any("launchctl" in c and "load" in c and "-w" in c for c in cmds)
    # The log directory is created too.
    assert ctrl.macos_log_path().parent.exists()


def test_macos_reinstall_is_idempotent(tmp_path):
    runner = FakeRunner()
    ctrl = mac_controller(tmp_path, runner=runner)
    ctrl.install(make_config(tmp_path))
    # Second install overwrites cleanly and does not raise.
    rc = ctrl.install(make_config(tmp_path))
    assert rc == 0
    assert ctrl.plist_path().exists()
    # An unload precedes the second load so launchctl doesn't reject a
    # double-load of an already-loaded label.
    assert any("unload" in c for c in runner.cmds())


def test_linux_install_enables_service(tmp_path):
    runner = FakeRunner()
    ctrl = linux_controller(tmp_path, runner=runner)
    ctrl.install(make_config(tmp_path))
    cmds = runner.cmds()
    assert any("daemon-reload" in c for c in cmds)
    assert any("enable" in c and "--now" in c for c in cmds)


# --- uninstall: stop + remove, safe when absent ------------------------------

def test_macos_uninstall_removes_and_stops(tmp_path):
    runner = FakeRunner()
    ctrl = mac_controller(tmp_path, runner=runner)
    ctrl.install(make_config(tmp_path))
    assert ctrl.plist_path().exists()

    runner.calls.clear()
    rc = ctrl.uninstall(make_config(tmp_path))
    assert rc == 0
    assert not ctrl.plist_path().exists()
    assert any("unload" in c for c in runner.cmds())


def test_macos_uninstall_when_absent_is_safe(tmp_path):
    runner = FakeRunner()
    ctrl = mac_controller(tmp_path, runner=runner)
    # Never installed: uninstall must not raise, must not call the manager.
    rc = ctrl.uninstall(make_config(tmp_path))
    assert rc == 0
    assert runner.calls == []


def test_linux_uninstall_when_absent_is_safe(tmp_path):
    runner = FakeRunner()
    ctrl = linux_controller(tmp_path, runner=runner)
    rc = ctrl.uninstall(make_config(tmp_path))
    assert rc == 0
    assert runner.calls == []


# --- status: manager query + healthz probe -----------------------------------

def test_macos_status_combines_manager_and_healthz(tmp_path, capsys):
    runner = FakeRunner()
    runner.stdout_for["list"] = "1234\t0\tcom.tiro.app\n"
    probe = lambda config: (True, "0.7.0")  # noqa: E731
    ctrl = mac_controller(tmp_path, runner=runner, probe=probe)
    rc = ctrl.status(make_config(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert any("launchctl" in c and "list" in c for c in runner.cmds())
    # Both signals surfaced: the manager sees the label AND healthz answered.
    assert "com.tiro.app" in out or "loaded" in out.lower()
    assert "0.7.0" in out


def test_status_reports_down_when_healthz_fails(tmp_path, capsys):
    runner = FakeRunner()
    runner.stdout_for["list"] = ""  # not loaded
    ctrl = mac_controller(tmp_path, runner=runner, probe=lambda c: (False, None))
    ctrl.status(make_config(tmp_path))
    out = capsys.readouterr().out.lower()
    assert "not" in out  # "not loaded" / "not responding"


# --- Windows: documented, not built ------------------------------------------

def test_windows_install_prints_nssm_and_exits_1(tmp_path, capsys):
    ctrl = service.ServiceController(
        platform="win32", home=tmp_path / "home", runner=FakeRunner(),
        executable="C:\\tiro.exe",
    )
    rc = ctrl.install(make_config(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out.lower()
    assert "nssm" in out


def test_windows_status_not_supported_exits_1(tmp_path, capsys):
    ctrl = service.ServiceController(
        platform="win32", home=tmp_path / "home", runner=FakeRunner(),
        executable="C:\\tiro.exe",
    )
    assert ctrl.status(make_config(tmp_path)) == 1


# --- logs --------------------------------------------------------------------

def test_macos_logs_tails_file(tmp_path):
    runner = FakeRunner()
    ctrl = mac_controller(tmp_path, runner=runner)
    log = ctrl.macos_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("hello\n")
    rc = ctrl.logs(make_config(tmp_path), follow=False)
    assert rc == 0
    assert any("tail" in c and str(log) in c for c in runner.cmds())


def test_macos_logs_follow_adds_flag(tmp_path):
    runner = FakeRunner()
    ctrl = mac_controller(tmp_path, runner=runner)
    log = ctrl.macos_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("hello\n")
    ctrl.logs(make_config(tmp_path), follow=True)
    assert any("tail" in c and "-f" in c for c in runner.cmds())


def test_macos_logs_missing_file_is_graceful(tmp_path, capsys):
    runner = FakeRunner()
    ctrl = mac_controller(tmp_path, runner=runner)
    rc = ctrl.logs(make_config(tmp_path), follow=False)
    assert rc == 0
    assert runner.calls == []  # no tail on a nonexistent log
    assert "no log" in capsys.readouterr().out.lower()


def test_linux_logs_uses_journalctl(tmp_path):
    runner = FakeRunner()
    ctrl = linux_controller(tmp_path, runner=runner)
    ctrl.logs(make_config(tmp_path), follow=False)
    cmds = runner.cmds()
    assert any("journalctl" in c and "--user" in c and "tiro" in c for c in cmds)


# --- dispatch (CLI entry) ----------------------------------------------------

def test_dispatch_routes_to_controller(tmp_path):
    runner = FakeRunner()
    ctrl = mac_controller(tmp_path, runner=runner)
    rc = service.dispatch("install", make_config(tmp_path), controller=ctrl)
    assert rc == 0
    assert ctrl.plist_path().exists()


def test_dispatch_unknown_command_raises(tmp_path):
    ctrl = mac_controller(tmp_path)
    with pytest.raises(ValueError):
        service.dispatch("bogus", make_config(tmp_path), controller=ctrl)

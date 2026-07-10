// Tauri desktop shell for Tiro (Phase 5 / M5.1, spec D7).
//
// A thin native window that manages the PyInstaller-frozen `tiro-server` as a
// sidecar. Launch sequence (spec D7, verbatim):
//   1. Resolve the config path (TIRO_CONFIG override, else the platform default
//      ~/Library/Application Support/Tiro/config.yaml); bootstrap a minimal
//      config (0600) if absent — mirroring cmd_init's minimal-write fallback.
//   2. Pick a port: prefer the config's `port` (default 8000); if occupied,
//      bind 127.0.0.1:0 for a free ephemeral port and use that.
//   3. Spawn the sidecar with TIRO_CONFIG / TIRO_PORT / TIRO_HOST=127.0.0.1.
//   4. Poll GET /healthz until ready (up to POLL_TIMEOUT_SECS), then navigate
//      the window to http://127.0.0.1:<port>/.
//   5. On window-close / app-quit: kill the sidecar (and its process group) —
//      no orphans.
//
// The sidecar binary is a PyInstaller *onedir* bundle, so it is shipped as a
// bundle *resource* (not externalBin, which wants a single file) and spawned
// via std::process. In dev, set TIRO_SERVER_BIN to the repo's built binary.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::menu::{AboutMetadata, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{Manager, RunEvent, WindowEvent};

/// How long to wait for the sidecar's /healthz to answer before giving up.
/// T1 measured ~4 s warm boot and ~37 s on a genuinely cold first boot (OS
/// cache cold + torch/ChromaDB paging + model-cache seed). Spec D7 says "~30 s";
/// we use 60 s so a real first launch never flakes — the window still opens the
/// instant /healthz answers (typically ~4 s), this is only the upper bound.
const POLL_TIMEOUT_SECS: u64 = 60;
const POLL_INTERVAL: Duration = Duration::from_millis(300);
const DEFAULT_PORT: u16 = 8000;

/// Shared runtime state: the live sidecar child and the resolved base URL.
struct Runtime {
    sidecar: Mutex<Option<Child>>,
    base_url: Mutex<String>,
}

/// The config file the shell reads/bootstraps. TIRO_CONFIG wins (used by the
/// acceptance test to point at a scratch dir); otherwise the macOS platform
/// default, mirroring tiro/paths.py::platform_config_path().
fn config_path() -> PathBuf {
    if let Ok(p) = std::env::var("TIRO_CONFIG") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    platform_config_path()
}

fn platform_config_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("Tiro")
        .join("config.yaml")
}

/// Create a minimal bootstrap config if none exists. This is INITIAL FILE
/// CREATION (not a config *update*), so it does not go through the Python
/// persist_config chokepoint — the Python side still only ever writes config via
/// persist_config. Library defaults to the config's own directory (the platform
/// app-support dir), matching tiro/paths.py::platform_default_library().
fn ensure_bootstrap_config(path: &Path) -> std::io::Result<()> {
    if path.exists() {
        return Ok(());
    }
    let dir = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(dir)?;
    let library = dir.to_string_lossy();
    let contents = format!("library_path: \"{library}\"\nhost: \"127.0.0.1\"\n");
    fs::write(path, contents)?;
    set_owner_only(path)?;
    Ok(())
}

#[cfg(unix)]
fn set_owner_only(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn set_owner_only(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

/// Best-effort read of the `port:` key from a YAML config. Single-user local
/// config; a full YAML parser is overkill, so scan top-level `port:` lines.
fn read_config_port(path: &Path) -> u16 {
    let Ok(text) = fs::read_to_string(path) else {
        return DEFAULT_PORT;
    };
    for line in text.lines() {
        let trimmed = line.trim_start();
        // top-level key only (no indentation) so we don't match smtp_port etc.
        if line.len() == trimmed.len() {
            if let Some(rest) = trimmed.strip_prefix("port:") {
                if let Ok(p) = rest.trim().trim_matches(['"', '\'']).parse::<u16>() {
                    return p;
                }
            }
        }
    }
    DEFAULT_PORT
}

fn port_is_free(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

fn pick_ephemeral_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok(listener.local_addr()?.port())
}

/// Prefer the configured/default port; fall back to an ephemeral free port only
/// when it is occupied (spec D7 — keeps the extension's hardcoded :8000 working
/// in the common case while solving the port-conflict gotcha).
fn choose_port(preferred: u16) -> u16 {
    if port_is_free(preferred) {
        preferred
    } else {
        pick_ephemeral_port().unwrap_or(preferred)
    }
}

/// Locate the frozen `tiro-server` executable. TIRO_SERVER_BIN wins (dev loop:
/// point it at desktop/pyinstaller/dist/tiro-server/tiro-server). In a bundled
/// .app it lives under the resource dir; we probe the expected layout then fall
/// back to a shallow search so `_up_`-prefixed resource paths still resolve.
fn resolve_sidecar_bin(resource_dir: &Path) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("TIRO_SERVER_BIN") {
        if !p.is_empty() {
            let pb = PathBuf::from(p);
            if pb.is_file() {
                return Some(pb);
            }
        }
    }
    let direct = resource_dir.join("tiro-server").join("tiro-server");
    if direct.is_file() {
        return Some(direct);
    }
    // Fallback: find a file literally named "tiro-server" up to 3 levels deep.
    find_named(resource_dir, "tiro-server", 3)
}

fn find_named(dir: &Path, name: &str, depth: usize) -> Option<PathBuf> {
    let entries = fs::read_dir(dir).ok()?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_file() && path.file_name().map(|n| n == name).unwrap_or(false) {
            return Some(path);
        }
        if depth > 0 && path.is_dir() {
            if let Some(found) = find_named(&path, name, depth - 1) {
                return Some(found);
            }
        }
    }
    None
}

/// Spawn the sidecar with the Tiro env contract. On unix it gets its own process
/// group so kill_sidecar can take down uvicorn + any torch/ChromaDB workers as a
/// unit (freeze_support in entry.py already prevents orphans; the group kill is
/// belt-and-suspenders).
fn spawn_sidecar(bin: &Path, config: &Path, port: u16, log_path: &Path) -> std::io::Result<Child> {
    let log = fs::File::create(log_path)?;
    let log_err = log.try_clone()?;
    let mut cmd = Command::new(bin);
    cmd.env("TIRO_CONFIG", config)
        .env("TIRO_PORT", port.to_string())
        .env("TIRO_HOST", "127.0.0.1")
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err));
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    cmd.spawn()
}

/// Raw HTTP GET /healthz over a fresh TCP connection; true iff the status line
/// reports 200. Dependency-free — this is loopback traffic to our own sidecar.
fn healthz_ok(port: u16) -> bool {
    let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let req = "GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 128];
    let Ok(n) = stream.read(&mut buf) else {
        return false;
    };
    let head = String::from_utf8_lossy(&buf[..n]);
    head.starts_with("HTTP/1.") && head.split_whitespace().nth(1) == Some("200")
}

/// Kill the sidecar and its process group. Idempotent (`take()`), so the two
/// callers (window CloseRequested + app Exit) can both fire safely.
fn kill_sidecar(runtime: &Runtime) {
    let Ok(mut guard) = runtime.sidecar.lock() else {
        return;
    };
    if let Some(mut child) = guard.take() {
        let pid = child.id();
        #[cfg(unix)]
        {
            // Negative pid => signal the whole process group.
            let _ = Command::new("kill")
                .arg("-TERM")
                .arg(format!("-{pid}"))
                .status();
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn build_menu(app: &tauri::AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let about = AboutMetadata {
        version: Some(env!("CARGO_PKG_VERSION").to_string()),
        name: Some("Tiro".to_string()),
        ..Default::default()
    };

    let about_item = PredefinedMenuItem::about(app, Some("About Tiro"), Some(about))?;
    let prefs = MenuItem::with_id(app, "preferences", "Preferences…", true, Some("Cmd+,"))?;
    let quit = PredefinedMenuItem::quit(app, Some("Quit Tiro"))?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;

    // On macOS the first submenu becomes the application menu regardless of name.
    let app_menu = Submenu::with_items(
        app,
        "Tiro",
        true,
        &[&about_item, &sep1, &prefs, &sep2, &quit],
    )?;
    // A standard Edit submenu keeps copy/paste/select-all working in the webview.
    let edit_menu = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;
    Menu::with_items(app, &[&app_menu, &edit_menu])
}

fn main() {
    tauri::Builder::default()
        .manage(Runtime {
            sidecar: Mutex::new(None),
            base_url: Mutex::new(String::new()),
        })
        .setup(|app| {
            let handle = app.handle().clone();

            // Menu + Preferences handler (navigates the window to /settings).
            let menu = build_menu(&handle)?;
            app.set_menu(menu)?;
            app.on_menu_event(move |app, event| {
                if event.id() == "preferences" {
                    let base = app.state::<Runtime>().base_url.lock().unwrap().clone();
                    if base.is_empty() {
                        return;
                    }
                    if let Some(win) = app.get_webview_window("main") {
                        if let Ok(url) = format!("{base}/settings").parse() {
                            let _ = win.navigate(url);
                        }
                    }
                }
            });

            // 1. Config path + bootstrap.
            let cfg = config_path();
            if let Err(e) = ensure_bootstrap_config(&cfg) {
                eprintln!("tiro-desktop: could not bootstrap config at {cfg:?}: {e}");
            }
            let cfg_dir = cfg
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| PathBuf::from("."));
            let log_path = cfg_dir.join("sidecar.log");

            // 2. Port.
            let port = choose_port(read_config_port(&cfg));
            let base_url = format!("http://127.0.0.1:{port}");
            *app.state::<Runtime>().base_url.lock().unwrap() = base_url.clone();

            // 3. Spawn sidecar.
            let resource_dir = app.path().resource_dir().unwrap_or_else(|_| cfg_dir.clone());
            let bin = resolve_sidecar_bin(&resource_dir);
            match bin {
                Some(bin) => match spawn_sidecar(&bin, &cfg, port, &log_path) {
                    Ok(child) => {
                        *app.state::<Runtime>().sidecar.lock().unwrap() = Some(child);
                    }
                    Err(e) => {
                        eprintln!("tiro-desktop: failed to spawn sidecar {bin:?}: {e}");
                        show_error(&handle, &format!("Could not start the Tiro engine.<br>{e}"));
                        return Ok(());
                    }
                },
                None => {
                    let msg = "Could not find the bundled Tiro engine.<br>\
                        (In dev, set <code>TIRO_SERVER_BIN</code> to the built \
                        <code>tiro-server</code> binary.)";
                    eprintln!("tiro-desktop: sidecar binary not found under {resource_dir:?}");
                    show_error(&handle, msg);
                    return Ok(());
                }
            }

            // 4. Poll /healthz off the main thread, then navigate the window.
            let poll_handle = handle.clone();
            let log_display = log_path.display().to_string();
            std::thread::spawn(move || {
                let deadline = Instant::now() + Duration::from_secs(POLL_TIMEOUT_SECS);
                loop {
                    // Early-exit detection: if the sidecar already died, stop waiting.
                    let mut sidecar_died = false;
                    {
                        let rt = poll_handle.state::<Runtime>();
                        let mut guard = rt.sidecar.lock().unwrap();
                        if let Some(child) = guard.as_mut() {
                            if let Ok(Some(_status)) = child.try_wait() {
                                *guard = None;
                                sidecar_died = true;
                            }
                        }
                    }
                    if sidecar_died {
                        show_error(
                            &poll_handle,
                            &format!(
                                "The Tiro engine exited during startup.<br>\
                                 See the log at <code>{log_display}</code>."
                            ),
                        );
                        return;
                    }
                    if healthz_ok(port) {
                        let url = base_url.clone();
                        let nav_handle = poll_handle.clone();
                        let _ = poll_handle.run_on_main_thread(move || {
                            if let Some(win) = nav_handle.get_webview_window("main") {
                                if let Ok(u) = url.parse() {
                                    let _ = win.navigate(u);
                                }
                                let _ = win.set_focus();
                            }
                        });
                        return;
                    }
                    if Instant::now() >= deadline {
                        show_error(
                            &poll_handle,
                            &format!(
                                "Tiro did not respond within {POLL_TIMEOUT_SECS}s.<br>\
                                 See the log at <code>{log_display}</code>."
                            ),
                        );
                        return;
                    }
                    std::thread::sleep(POLL_INTERVAL);
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { .. } = event {
                kill_sidecar(&window.app_handle().state::<Runtime>());
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the Tiro desktop app")
        .run(|app, event| match event {
            RunEvent::ExitRequested { .. } | RunEvent::Exit => {
                kill_sidecar(&app.state::<Runtime>());
            }
            _ => {}
        });
}

/// Surface a startup failure in the (still-placeholder) window via the JS hook.
fn show_error(handle: &tauri::AppHandle, html: &str) {
    let handle = handle.clone();
    let payload = serde_json::to_string(html).unwrap_or_else(|_| "\"Startup error\"".into());
    let _ = handle.clone().run_on_main_thread(move || {
        if let Some(win) = handle.get_webview_window("main") {
            let _ = win.eval(format!("window.__tiroError && window.__tiroError({payload})"));
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ephemeral_port_is_free_and_nonzero() {
        let p = pick_ephemeral_port().unwrap();
        assert!(p > 0);
    }

    #[test]
    fn choose_port_prefers_free_preferred() {
        // An ephemeral port we just released is (almost surely) free again.
        let p = pick_ephemeral_port().unwrap();
        assert_eq!(choose_port(p), p);
    }

    #[test]
    fn choose_port_falls_back_when_busy() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let busy = listener.local_addr().unwrap().port();
        let chosen = choose_port(busy);
        assert_ne!(chosen, busy, "must not return an occupied port");
        assert!(chosen > 0);
    }

    #[test]
    fn read_config_port_default_and_explicit() {
        let dir = std::env::temp_dir().join(format!("tiro-cfg-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();

        let no_port = dir.join("a.yaml");
        std::fs::write(&no_port, "library_path: \"/x\"\nhost: \"127.0.0.1\"\n").unwrap();
        assert_eq!(read_config_port(&no_port), DEFAULT_PORT);

        let with_port = dir.join("b.yaml");
        let mut f = std::fs::File::create(&with_port).unwrap();
        // smtp_port is indented-style noise the scan must ignore; top-level wins.
        writeln!(f, "smtp_port: 1025").unwrap();
        writeln!(f, "port: 8123").unwrap();
        drop(f);
        assert_eq!(read_config_port(&with_port), 8123);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bootstrap_config_is_created_once_with_owner_perms() {
        let dir = std::env::temp_dir().join(format!("tiro-boot-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let cfg = dir.join("config.yaml");
        ensure_bootstrap_config(&cfg).unwrap();
        assert!(cfg.exists());
        let body = std::fs::read_to_string(&cfg).unwrap();
        assert!(body.contains("library_path:"));
        assert!(body.contains("host: \"127.0.0.1\""));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&cfg).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
        }
        // Idempotent: a second call must not error or overwrite.
        std::fs::write(&cfg, "library_path: \"/kept\"\n").unwrap();
        ensure_bootstrap_config(&cfg).unwrap();
        assert!(std::fs::read_to_string(&cfg).unwrap().contains("/kept"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}

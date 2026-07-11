"""CLI entry points for Tiro."""

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def cmd_init(args):
    """Initialize a new Tiro library."""
    import shutil

    from tiro.config import load_config
    from tiro.database import init_db
    from tiro.vectorstore import init_vectorstore

    # Generate config.yaml from example template if it doesn't exist
    root_config = Path(args.config)
    created_new = not root_config.exists()
    if created_new:
        example = Path(__file__).resolve().parent.parent / "config.example.yaml"
        if example.exists():
            shutil.copy(example, root_config)
            print(f"Created {root_config} from template")
        else:
            # Fallback: write minimal config. No secrets are written here yet
            # (those come later via persist_config), but the file may soon
            # hold them, so create it with 0600 from the start.
            root_config.write_text("library_path: ./tiro-library\n")
            os.chmod(root_config, 0o600)
            print(f"Created {root_config}")

    config = load_config(args.config)

    # D2: a NEWLY created config gets the platform-standard library location
    # written into it (both the template-copy and minimal-fallback paths above).
    # Existing config files are NEVER touched — their library_path is
    # authoritative. DEFAULTS["library_path"] deliberately stays ./tiro-library
    # (see tiro/paths.py); the platform default enters only through this writer.
    if created_new:
        from tiro.config import persist_config
        from tiro.paths import platform_default_library

        persist_config(config, {"library_path": str(platform_default_library())})
        config = load_config(args.config)  # reload so config.library reflects it
        print(f"Library location set to {config.library}")

    config.library.mkdir(parents=True, exist_ok=True)
    config.articles_dir.mkdir(parents=True, exist_ok=True)

    init_db(config.db_path)
    init_vectorstore(config.chroma_dir)

    # Prompt for API key
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    config_key = (yaml.safe_load(root_config.read_text()) or {}).get("anthropic_api_key", "")

    print()
    print("Tiro uses the Anthropic API for AI features (digests, analysis, preferences).")
    print("Get your API key at https://console.anthropic.com/")
    print()

    api_key = ""
    existing_key = env_key or config_key
    if existing_key:
        masked = existing_key[:7] + "..." + existing_key[-4:]
        print(f"Found existing API key: {masked}")
        choice = input("Use this key? [Y/n] or paste a different one: ").strip()
        if choice == "" or choice.lower() in ("y", "yes"):
            api_key = existing_key
        elif choice.lower() in ("n", "no"):
            api_key = input("Anthropic API key (or press Enter to skip): ").strip()
        else:
            # They pasted a key directly
            api_key = choice
    else:
        api_key = input("Anthropic API key (or press Enter to skip): ").strip()

    if api_key:
        from tiro.config import persist_config

        persist_config(config, {"anthropic_api_key": api_key})
        print(f"API key saved to {root_config}")
    else:
        print("Skipped — set ANTHROPIC_API_KEY env var or add it to config.yaml later.")

    # Offer email setup
    print()
    setup_email = input("Set up Gmail email integration? (send digests / receive newsletters) [y/N] ").strip()
    if setup_email.lower() in ("y", "yes"):
        _interactive_email_setup(root_config)

    # Offer OpenAI TTS setup
    print()
    print("Tiro can read articles aloud using OpenAI's text-to-speech.")
    print("Get your API key at https://platform.openai.com/api-keys")
    print()

    openai_key = ""
    existing_openai = os.environ.get("OPENAI_API_KEY", "") or (yaml.safe_load(root_config.read_text()) or {}).get("openai_api_key", "")
    if existing_openai:
        masked = existing_openai[:7] + "..." + existing_openai[-4:]
        print(f"Found existing OpenAI key: {masked}")
        choice = input("Use this key? [Y/n] or paste a different one: ").strip()
        if choice == "" or choice.lower() in ("y", "yes"):
            openai_key = existing_openai
        elif choice.lower() in ("n", "no"):
            openai_key = input("OpenAI API key (or press Enter to skip): ").strip()
        else:
            openai_key = choice
    else:
        openai_key = input("OpenAI API key (or press Enter to skip): ").strip()

    if openai_key:
        from tiro.config import persist_config

        persist_config(config, {"openai_api_key": openai_key})
        print(f"OpenAI key saved to {root_config}")
    else:
        print("Skipped — articles will use browser voice (free, lower quality).")

    print(f"\nTiro library initialized at {config.library}")
    print("Start the server with: uv run tiro run")


def cmd_run(args):
    """Start the Tiro server."""
    import threading
    import time
    import webbrowser

    import uvicorn

    from tiro.config import load_config

    config = load_config(args.config)

    # --cert/--key (M3.0 Task 4): both-or-neither is already enforced at the
    # argparse layer in main() (a real usage error, before we even get
    # here). What's left is the runtime file-exists check, which must
    # produce a clear error and exit BEFORE uvicorn ever starts -- uvicorn's
    # own failure mode for a missing cert/key is a far less legible
    # traceback buried inside its TLS setup. getattr() with a None default:
    # existing tests construct argparse.Namespace(...) by hand without
    # cert/key attributes at all (see test_auth.py), and those callers must
    # keep working unchanged.
    cert = getattr(args, "cert", None)
    key = getattr(args, "key", None)
    tls_enabled = bool(cert and key)
    if tls_enabled:
        from tiro.tls import check_tls_files_exist

        try:
            check_tls_files_exist(cert, key)
        except FileNotFoundError as e:
            print(str(e))
            sys.exit(1)

    # Compute the effective host FIRST — a bare `host: "0.0.0.0"` in
    # config.yaml (without --lan) is just as exposed as --lan, and must be
    # refused the same way. Checking args.lan alone let config.yaml bypass
    # the refusal entirely.
    effective_host = "0.0.0.0" if args.lan else config.host
    if effective_host not in ("127.0.0.1", "localhost") and not config.auth_password_hash:
        if getattr(args, "insecure_no_auth", False):
            print("=" * 60)
            print("WARNING: --lan with NO AUTHENTICATION (--insecure-no-auth).")
            print("Anyone on your network can read and modify your library.")
            print("=" * 60)
        else:
            print(f"Binding to {effective_host} requires a password so other devices can't read your library.")
            print("Set one with:  uv run tiro set-password")
            print("Or (NOT recommended):  tiro run --lan --insecure-no-auth")
            sys.exit(1)

    # Import app AFTER config is loaded — load_config sets env vars
    # (ANTHROPIC_API_KEY, etc.) that router imports may depend on
    from tiro.app import create_app

    # Set the effective host on the in-memory config BEFORE create_app (never
    # written to config.yaml) — create_app derives app.state.lan_ips (and the
    # Host-validation allowlist) from config.host, so --lan must be visible
    # there too, not just in the local `effective_host` variable used for
    # uvicorn.run below.
    config.host = effective_host

    app = create_app(config, tls_enabled=tls_enabled)

    host = effective_host
    scheme = "https" if tls_enabled else "http"
    url = f"{scheme}://localhost:{config.port}"

    if args.lan:
        # create_app() already ran _detect_lan_ips() and populated
        # app.state.lan_ips (same helper) — reuse it here purely to print
        # the reachable URLs, avoiding a second round of socket calls.
        candidate_ips = sorted(app.state.lan_ips)
        if candidate_ips:
            for ip in candidate_ips:
                print(f"LAN mode: accessible at {scheme}://{ip}:{config.port}")
        else:
            print("LAN mode: binding to 0.0.0.0 (could not detect LAN IP)")

    # Startup warning (M3.0 Task 4): extends the LAN-IP print above with a
    # logged WARNING when serving plain HTTP to a non-loopback bind --
    # app.state.insecure_lan_http (set by create_app from the same
    # effective-host + tls_enabled inputs) is the single source of truth,
    # so this can't drift from what the browser banner shows.
    if app.state.insecure_lan_http:
        auth_url = f"http://{app.state.lan_ip}:{config.port}" if app.state.lan_ip else url
        logger.warning(
            "Serving unencrypted HTTP on your local network (%s) — "
            "use Tailscale Serve or `tiro run --cert/--key` for HTTPS.",
            auth_url,
        )

    if not args.no_browser:
        def open_browser():
            import ssl
            import urllib.request
            # Poll until the server is actually responding (up to 30s).
            # Self-signed certs (the expected --cert/--key case) fail normal
            # verification, but this is a same-machine readiness ping, not a
            # security-sensitive request -- verification is deliberately
            # skipped here so TLS mode doesn't just retry-and-timeout for
            # the full 30s on every launch before opening the browser late.
            ctx = ssl._create_unverified_context() if tls_enabled else None
            for _ in range(60):
                time.sleep(0.5)
                try:
                    urllib.request.urlopen(
                        f"{scheme}://127.0.0.1:{config.port}/healthz", timeout=1, context=ctx
                    )
                    break
                except Exception:
                    continue
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    print(f"Starting Tiro at {url}")
    uvicorn.run(app, host=host, port=config.port, ssl_certfile=cert, ssl_keyfile=key)


def cmd_export(args):
    """Export the library as a zip bundle."""
    import shutil

    from tiro.config import load_config
    from tiro.export import export_library

    config = load_config(args.config)
    output = Path(args.output)

    zip_path = export_library(
        config,
        tag=args.tag,
        source_id=args.source_id,
        rating_min=args.rating_min,
        date_from=args.date_from,
    )

    shutil.move(str(zip_path), str(output))
    print(f"Library exported to {output}")


def cmd_backup(args):
    """Write a full library snapshot (tar.zst)."""
    from tiro.backup import create_snapshot
    from tiro.config import load_config

    config = getattr(args, "_config_override", None) or load_config(args.config)
    output = Path(args.output) if args.output else None
    snap = create_snapshot(config, output, include_audio=args.include_audio)
    print(f"Snapshot written: {snap}")


def cmd_restore(args):
    """Replace the library from a snapshot."""
    import socket

    from tiro.backup import restore_snapshot
    from tiro.config import load_config

    config = getattr(args, "_config_override", None) or load_config(args.config)
    snapshot = Path(args.snapshot)
    if not snapshot.exists():
        print(f"Snapshot not found: {snapshot}")
        sys.exit(1)

    if not getattr(args, "force", False):
        try:
            with socket.create_connection((config.host, config.port), timeout=1):
                server_running = True
        except OSError:
            server_running = False
        if server_running:
            print(
                f"A server appears to be running on {config.host}:{config.port} — "
                "stop it first (or pass --force)."
            )
            sys.exit(1)

    if not args.yes:
        answer = input(
            f"Replace the library at {config.library} with {snapshot.name}?\n"
            "The current library is moved aside (not deleted). "
            "Stop the server first. [y/N] "
        )
        if answer.strip().lower() != "y":
            print("Aborted.")
            sys.exit(1)
    result = restore_snapshot(config, snapshot)
    if result.get("schema_newer_than_app"):
        print(
            "WARNING: the restored snapshot's database schema is newer than this "
            "Tiro version supports — upgrade Tiro before relying on this library."
        )
    print(
        f"Restored {result['articles']} articles "
        f"({result['vectors_restored']} vectors restored, "
        f"{result['vectors_pending']} pending re-embed). "
        f"Previous library: {result['displaced_library']}"
    )


def _interactive_email_setup(config_path: Path):
    """Interactive email setup flow — shared by cmd_init and cmd_setup_email."""
    print()
    print("Gmail Email Integration")
    print("=" * 40)
    print()
    print("Tiro can send digest emails and receive newsletters via Gmail.")
    print("You'll need a Gmail App Password (not your regular password).")
    print("Create one at: https://myaccount.google.com/apppasswords")
    print()

    # What features?
    print("What would you like to set up?")
    print("  1. Send digest emails only")
    print("  2. Receive newsletters via IMAP only")
    print("  3. Both send and receive")
    choice = input("Choice [3]: ").strip() or "3"

    want_send = choice in ("1", "3")
    want_receive = choice in ("2", "3")

    # Gmail address
    gmail = input("Gmail address: ").strip()
    if not gmail:
        print("Skipped — no email address provided.")
        return

    # App password
    app_password = input("Gmail App Password (16 chars, no spaces): ").strip()
    if not app_password:
        print("Skipped — no app password provided.")
        return

    updates: dict = {}
    label = "tiro"

    if want_send:
        updates["smtp_host"] = "smtp.gmail.com"
        updates["smtp_port"] = 587
        updates["smtp_user"] = gmail
        updates["smtp_password"] = app_password
        updates["smtp_use_tls"] = True
        updates["digest_email"] = gmail
        print("  SMTP configured: smtp.gmail.com:587 (TLS)")

    if want_receive:
        label = input("Gmail label to monitor [tiro]: ").strip() or "tiro"
        updates["imap_host"] = "imap.gmail.com"
        updates["imap_port"] = 993
        updates["imap_user"] = gmail
        updates["imap_password"] = app_password
        updates["imap_label"] = label
        updates["imap_enabled"] = True
        updates["imap_sync_interval"] = 15
        print(f"  IMAP configured: imap.gmail.com:993, label='{label}', sync every 15 min")

    from tiro.config import load_config, persist_config

    cfg = load_config(config_path)
    persist_config(cfg, updates)
    print(f"\nEmail settings saved to {config_path}")

    if want_receive:
        print("\nTo receive newsletters:")
        print(f"  1. Create a Gmail label called '{label}'")
        print("  2. Set up a Gmail filter to auto-label forwarded newsletters")
        print("  3. Run: uv run tiro check-email")


def cmd_setup_email(args):
    """Interactive Gmail email setup."""
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"No config file found at {config_path}. Run 'tiro init' first.")
        sys.exit(1)
    _interactive_email_setup(config_path)


def cmd_check_email(args):
    """Check IMAP inbox for new newsletters and ingest them."""
    from tiro.config import load_config
    from tiro.ingestion.imap import check_imap_inbox

    config = load_config(args.config)

    if not config.imap_user or not config.imap_password:
        print("IMAP not configured. Run: uv run tiro setup-email")
        sys.exit(1)

    print(f"Checking {config.imap_user} / label '{config.imap_label}'...")

    try:
        result = check_imap_inbox(config)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)

    if result["fetched"] == 0:
        print("No new messages.")
        return

    print(f"\nFetched: {result['fetched']}")
    print(f"Processed: {result['processed']}")
    print(f"Skipped (duplicates): {result['skipped']}")
    print(f"Failed: {result['failed']}")

    if result["articles"]:
        print("\nIngested articles:")
        for a in result["articles"]:
            print(f"  [{a['id']}] {a['title']}")

    if result["errors"]:
        print("\nErrors:")
        for err in result["errors"]:
            print(f"  - {err}")


def cmd_import_emails(args):
    """Bulk import .eml files from a directory."""
    from tiro.config import load_config
    from tiro.ingestion.email import parse_eml
    from tiro.ingestion.processor import process_article

    config = load_config(args.config)
    directory = args.directory

    if not directory.is_dir():
        print(f"Error: Not a directory: {directory}")
        sys.exit(1)

    eml_files = sorted(directory.glob("*.eml"))
    if not eml_files:
        print(f"No .eml files found in {directory}")
        sys.exit(1)

    print(f"Found {len(eml_files)} .eml files in {directory}")
    print()

    processed = 0
    skipped = 0
    failed = 0

    for i, eml_path in enumerate(eml_files, 1):
        filename = eml_path.name
        prefix = f"[{i}/{len(eml_files)}]"

        try:
            extracted = parse_eml(eml_path)
        except (ValueError, Exception) as e:
            print(f"{prefix} FAIL  {filename}: {e}")
            failed += 1
            continue

        try:
            article = process_article(
                title=extracted["title"],
                author=extracted["author"],
                content_md=extracted["content_md"],
                url=extracted["url"],
                config=config,
                published_at=extracted["published_at"],
                email_sender=extracted["email_sender"],
            )
            print(f"{prefix} OK    [{article['id']}] {article['title']}")
            processed += 1
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                print(f"{prefix} SKIP  {filename}: duplicate")
                skipped += 1
            else:
                print(f"{prefix} FAIL  {filename}: {e}")
                failed += 1

    print(f"\nDone! {processed} imported, {skipped} skipped, {failed} failed")


def cmd_import_bundle(args):
    """Import a Tiro export bundle (zip) into the current library."""
    from tiro.config import load_config
    from tiro.importer import import_bundle

    config = getattr(args, "_config_override", None) or load_config(args.config)
    result = import_bundle(config, Path(args.bundle), conflicts=args.conflicts)
    print(
        f"Imported: {result['imported']} | skipped: {result['skipped']} | "
        f"overwritten: {result['overwritten']} | kept-both: {result['kept_both']} | "
        f"sources created: {result['sources_created']}"
    )
    return 0


def _print_import_summary(kind: str, summary: dict) -> None:
    """Print the shared importer summary table (same fields as the API job)."""
    print()
    print(f"Import complete ({kind}):")
    print(f"  imported:   {summary['imported']}")
    print(f"  skipped:    {summary['skipped']} (already in library)")
    print(f"  stubs:      {summary['stub_articles']} (content could not be fetched)")
    print(f"  failed:     {summary['failed']}")
    print(
        f"  highlights: {summary['highlights_imported']} imported, "
        f"{summary['highlights_skipped']} skipped"
    )


def _run_cli_import(config, items, *, kind: str) -> int:
    """Drive `run_import` from a CLI verb: progress every 10 items, summary
    table at the end. Importers always skip existing articles (no --conflicts;
    simpler than bundle import)."""
    from tiro.ingestion.importers.base import run_import

    def progress(s):
        if s["processed"] % 10 == 0:
            print(
                f"  ... {s['processed']} processed "
                f"({s['imported']} imported, {s['skipped']} skipped, {s['failed']} failed)"
            )

    summary = run_import(config, items, kind=kind, progress_cb=progress)
    _print_import_summary(kind, summary)
    return 0


def cmd_import_instapaper(args):
    """Import an Instapaper CSV export."""
    from tiro.config import load_config
    from tiro.ingestion.importers import instapaper

    config = getattr(args, "_config_override", None) or load_config(args.config)
    path = Path(args.file)
    if not path.is_file():
        print(f"Error: file not found: {path}")
        sys.exit(1)
    return _run_cli_import(config, instapaper.parse_export(path), kind="instapaper")


def cmd_import_omnivore(args):
    """Import an Omnivore export zip."""
    from tiro.config import load_config
    from tiro.ingestion.importers import omnivore

    config = getattr(args, "_config_override", None) or load_config(args.config)
    path = Path(args.file)
    if not path.is_file():
        print(f"Error: file not found: {path}")
        sys.exit(1)
    return _run_cli_import(config, omnivore.parse_export(path), kind="omnivore")


def cmd_import_readwise(args):
    """Import a Readwise JSON export (articles + books; highlights anchored)."""
    from tiro.config import load_config
    from tiro.ingestion.importers import readwise

    config = getattr(args, "_config_override", None) or load_config(args.config)
    path = Path(args.file)
    if not path.is_file():
        print(f"Error: file not found: {path}")
        sys.exit(1)
    return _run_cli_import(config, readwise.parse_export(path), kind="readwise")


def cmd_delete(args):
    """Delete an article by id from all stores."""
    from tiro.config import load_config
    from tiro.lifecycle import delete_article

    config = load_config(args.config)
    if delete_article(config, args.id):
        print(f"Deleted article {args.id}.")
    else:
        print(f"No article with id {args.id}.")
        sys.exit(1)


def cmd_agent(args):
    """List registered agents or run one by name."""
    import json

    from tiro.agents import registry
    from tiro.agents.base import AgentRunError
    from tiro.agents.runtime import run_agent
    from tiro.config import load_config
    from tiro.database import migrate_db

    config = load_config(args.config)
    registry.ensure_builtins()

    if args.agent_cmd == "list":
        # Listing is pure static metadata (no library/DB touch needed) —
        # unlike "run", which needs a real, migrated library to write
        # agent_runs rows + trace files.
        for agent in sorted(registry.all_agents().values(), key=lambda a: a.name):
            inputs = ", ".join(f"{k}: {t.__name__}" for k, t in agent.inputs.items())
            print(f"  {agent.name}  v{agent.version}  [{agent.tier}]  inputs: {inputs or '(none)'}")
        return

    if not config.db_path.exists():
        print("No Tiro library found. Run `uv run tiro init` first.")
        sys.exit(1)
    migrate_db(config.db_path)

    inputs = {}
    for pair in args.input:
        if "=" not in pair:
            print(f"--input expects KEY=VALUE, got {pair!r}")
            sys.exit(2)
        key, _, value = pair.partition("=")
        try:
            inputs[key] = json.loads(value)
        except ValueError:
            inputs[key] = value          # bare strings stay strings
    try:
        res = run_agent(config, args.name, inputs)
    except AgentRunError as e:
        print(f"Run failed: {e}")
        if e.run_uid:
            print(f"  recorded as run {e.run_uid} (see /agents)")
        sys.exit(1)
    print(f"ok  run={res.run_uid}  tokens={res.tokens_in}->{res.tokens_out}"
          f"  cost=${res.cost_usd:.4f}  citations={len(res.citations)}")
    print(res.outputs.model_dump_json(indent=2))


_EVAL_PROVIDER_FIELDS = (
    "ai_heavy_provider", "ai_light_provider", "ai_heavy_model", "ai_light_model",
    "ai_openai_base_url", "ai_openai_api_key", "ai_claude_cli_path", "ai_codex_cli_path",
    "anthropic_api_key", "openai_api_key",
)


def cmd_evals(args):
    """Run the agent evals harness (structural by default; --real confirms cost)."""
    from tiro.evals.runner import run_structural

    providers = None
    if args.real:
        from tiro.config import load_config

        print("Real mode hits your configured providers and SPENDS MONEY "
              "(cost varies by provider/model — no upfront estimate).")
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            print("Aborted.")
            sys.exit(1)
        # Only --real reads the user's real config.yaml; structural mode
        # never touches it (see tiro/evals/runner.py's isolation contract).
        config = load_config(args.config)
        providers = {name: getattr(config, name) for name in _EVAL_PROVIDER_FIELDS}
    results = run_structural(args.agent, real=args.real, providers=providers)
    failed_total = 0
    for name, r in results.items():
        print(f"  {name}: {r['passed']} passed, {r['failed']} failed")
        for msg in r["failures"]:
            print(f"    FAIL {msg}")
        failed_total += r["failed"]
    sys.exit(1 if failed_total else 0)


def cmd_doctor(args):
    """Check (and optionally repair) consistency across all four stores."""
    import json as _json

    from tiro.config import load_config
    from tiro.database import migrate_db
    from tiro.doctor import fix, scan
    from tiro.vectorstore import get_collection, init_vectorstore

    config = getattr(args, "_config_override", None) or load_config(args.config)
    if not config.db_path.exists():
        print("No Tiro library found. Run `uv run tiro init` first.")
        sys.exit(1)
    migrate_db(config.db_path)
    try:
        get_collection()
    except RuntimeError:
        init_vectorstore(config.chroma_dir, config.default_embedding_model)

    if args.fix:
        fix_report = fix(config)
        post = scan(config)
        report = {**post, "actions": fix_report["actions"],
                  "reembed_failures": fix_report["reembed_failures"]}
    else:
        report = scan(config)

    if args.json:
        print(_json.dumps(report, indent=2))
    else:
        if args.fix:
            for action in report.get("actions", []):
                print(f"  fixed: {action}")
        for key in ("orphaned_markdown", "missing_markdown", "orphaned_vectors",
                    "vector_missing", "vector_unmarked", "vector_failed",
                    "audio_rows_missing_file", "audio_files_without_row"):
            items = report[key]
            if items:
                print(f"{key}: {len(items)}")
                for item in items:
                    print(f"  - {item}")
        housekeeping_found = False
        for key in ("unreferenced_tags", "unreferenced_entities",
                    "unreferenced_authors", "expired_sessions", "expired_login_tokens",
                    "wiki_index_drift", "annotations_index_drift", "agent_runs_stuck"):
            if report[key]:
                housekeeping_found = True
                print(f"{key}: {report[key]}")
        if report.get("agent_trace_orphans"):
            housekeeping_found = True
            print(f"agent_trace_orphans: {len(report['agent_trace_orphans'])}")
            for item in report["agent_trace_orphans"]:
                print(f"  - {item}")
        if report.get("annotations_guarded"):
            print(
                "annotations_guarded: a highlights/notes sidecar directory is missing or "
                "effectively empty while index rows still reference it — not deleting rows "
                "automatically. "
                "Restore the directory (annotations/ or notes/) and re-run."
            )
        if report.get("reembed_failures"):
            print(f"reembed_failures: {report['reembed_failures']}")
        if report.get("conflict_files"):
            print(f"conflict_files: {len(report['conflict_files'])} "
                  "(preserved losing versions — review and delete when done)")
            for name in report["conflict_files"]:
                print(f"  - {name}")
        if housekeeping_found:
            print("(housekeeping findings above are cleaned by --fix but do not fail this check)")
        if report["clean"]:
            print("All stores consistent. ✓")
        elif args.fix:
            print("Repairs applied — run `tiro doctor` again to verify.")
        else:
            print("Issues found. Run `tiro doctor --fix` (stop the server first).")

    if args.fix:
        failed = (not report["structurally_consistent"]) or report.get("reembed_failures", 0) > 0
    else:
        failed = not report["structurally_consistent"]
    sys.exit(1 if failed else 0)


def cmd_reconcile(args):
    """One reconcile pass: fold external library edits into SQLite/Chroma/
    anchors (sync S1). Run with the server stopped, like doctor — the
    server's own 30s loop covers the running case."""
    import json as _json
    from dataclasses import asdict

    from tiro.config import load_config
    from tiro.database import migrate_db
    from tiro.sync.reconcile import reconcile_library
    from tiro.vectorstore import get_collection, init_vectorstore, retry_pending_vectors

    config = getattr(args, "_config_override", None) or load_config(args.config)
    if not config.db_path.exists():
        print("No Tiro library found. Run `uv run tiro init` first.")
        sys.exit(1)
    migrate_db(config.db_path)
    try:
        get_collection()
    except RuntimeError:
        init_vectorstore(config.chroma_dir, config.default_embedding_model)

    report = reconcile_library(config, dry_run=args.dry_run)
    reembedded = 0
    if not args.dry_run:
        reembedded = retry_pending_vectors(config)

    if args.json:
        print(_json.dumps({**asdict(report), "reembedded": reembedded}, indent=2))
    else:
        prefix = "[dry-run] " if args.dry_run else ""
        print(f"{prefix}{report.summary()}")
        if reembedded:
            print(f"re-embedded: {reembedded}")
        if report.details.get("delete_guard"):
            print(f"GUARDED: {report.details['delete_guard']}")
        for name in report.details.get("conflict_files", []):
            print(f"conflict file written: {name}")
        for warn in report.details.get("anchor_warnings", []):
            print(f"highlight no longer anchors: {warn['highlight_uid']} "
                  f"({warn['status']})")
    sys.exit(0)


def cmd_migrate_library(args):
    """Copy the library to a new location (spec D3). The old copy is NEVER
    deleted — the user removes it manually after verifying."""
    import socket

    from tiro.config import load_config
    from tiro.library_move import MigrationError, migrate_library
    from tiro.paths import platform_default_library

    config = getattr(args, "_config_override", None) or load_config(args.config)
    dest = Path(args.dest).resolve() if args.dest else platform_default_library()
    source = config.library

    if dest == source:
        print(f"Library is already at {dest} — nothing to migrate.")
        sys.exit(0)

    # Best-effort port-in-use warning: copying a live library corrupts it.
    try:
        with socket.create_connection((config.host, config.port), timeout=1):
            print(
                f"WARNING: something is listening on {config.host}:{config.port}. "
                "Stop the Tiro server before migrating — copying a live "
                "ChromaDB/SQLite mid-write can corrupt the copy."
            )
    except OSError:
        pass

    if not args.yes:
        answer = input(
            f"Copy the library from {source} to {dest}?\n"
            "The OLD copy is never deleted — you remove it yourself after "
            "verifying the new location. [y/N] "
        )
        if answer.strip().lower() != "y":
            print("Aborted.")
            sys.exit(1)

    try:
        report = migrate_library(config, dest)
    except MigrationError as e:
        print(f"Migration aborted: {e}")
        sys.exit(1)

    if report["status"] == "already_at_destination":
        print(f"Library is already at {dest} — nothing to migrate.")
        sys.exit(0)

    print(
        f"Copied {report['files_copied']} files ({report['bytes_copied']} bytes) "
        f"to {dest}."
    )
    if report.get("config_relocated_to"):
        print(
            f"Your config lived inside the library and was relocated to "
            f"{report['config_relocated_to']} — it is safe to remove the old library."
        )
    print(
        f"Old library preserved at {report['source']} — verify the new "
        "location, then remove the old copy yourself."
    )
    sys.exit(0)


def cmd_migrate(args):
    """Apply pending database migrations."""
    from tiro.config import load_config
    from tiro.migrations import pre_migrate_snapshot, run_migrations

    config = getattr(args, "_config_override", None) or load_config(args.config)
    if not config.db_path.exists():
        print("No Tiro library found. Run `uv run tiro init` first.")
        sys.exit(1)

    # Symmetry with server-start (spec D4): full snapshot before a
    # version-crossing migration. Best-effort; prints the snapshot path when one
    # was taken so the user knows the rollback point.
    snapshot = pre_migrate_snapshot(config)
    if snapshot:
        print(f"Pre-migrate snapshot: {snapshot}")
    applied = run_migrations(config.db_path)
    if applied:
        for line in applied:
            print(f"applied: {line}")
    else:
        print("No pending migrations.")
    sys.exit(0)


def cmd_service(args):
    """Install/manage Tiro as a background service (launchd / systemd user unit).

    Targets the CLI/uv install — the Tauri desktop app manages its own sidecar
    and must not also be run as a service (install one or the other).
    """
    from tiro import service
    from tiro.config import load_config

    config = getattr(args, "_config_override", None) or load_config(args.config)
    rc = service.dispatch(
        args.service_command, config, follow=getattr(args, "follow", False)
    )
    sys.exit(rc)


def cmd_audit(args):
    """Show the external-API audit log (calls, tokens, cost estimates)."""
    import json as _json
    import re
    from datetime import date as _date

    from tiro.audit import read_audit_entries, summarize
    from tiro.config import load_config

    if args.month and not re.fullmatch(r"\d{4}-\d{2}", args.month):
        print("Invalid --month (expected YYYY-MM, e.g. 2026-07).")
        sys.exit(2)

    config = getattr(args, "_config_override", None) or load_config(args.config)
    day = args.date
    if not day and not args.month:
        day = _date.today().isoformat()
    entries = read_audit_entries(config, date=day, month=args.month, service=args.service)

    if args.month:
        rollup = summarize(entries)
        if args.json:
            print(_json.dumps(rollup, indent=2))
            return
        print(f"Audit summary for {args.month}:")
        for service, s in sorted(rollup.items()):
            cost = f"${s['cost_estimate']:.4f}" if s["cost_estimate"] else "-"
            print(f"  {service:12} {s['calls']:5} calls ({s['failures']} failed)  "
                  f"tokens {s['tokens_in']}/{s['tokens_out']}  chars {s['chars']}  est {cost}")
        if not rollup:
            print("  (no entries)")
        return

    if args.json:
        print(_json.dumps(entries, indent=2))
        return
    for e in entries:
        status = "ok" if e.get("success", True) else "FAIL"
        cost_val = e.get("cost_estimate")
        cost = f" ${cost_val:.4f}" if cost_val is not None else ""
        count_val = e.get("count")
        detail = e.get("model") or (str(count_val) if count_val is not None else "")
        print(f"{e.get('timestamp', '?')}  {e.get('service', '?'):10} "
              f"{e.get('endpoint', '?'):16} {status}{cost}  {detail}")
    if not entries:
        print(f"No audit entries for {day or args.month}.")


def cmd_status(args):
    """Library status without needing a running server."""
    from tiro import __version__
    from tiro.config import load_config
    from tiro.database import dir_bytes, get_connection

    config = getattr(args, "_config_override", None) or load_config(args.config)
    print(f"Tiro {__version__} — library: {config.library}")
    if not config.db_path.exists():
        print("No library found. Run `uv run tiro init` first.")
        sys.exit(1)

    conn = get_connection(config.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        by_status = conn.execute(
            "SELECT vector_status, COUNT(*) AS n FROM articles GROUP BY vector_status"
        ).fetchall()
        sessions = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE expires_at > datetime('now')"
        ).fetchone()["n"]
    finally:
        conn.close()

    breakdown = ", ".join(f"{r['vector_status']}: {r['n']}" for r in by_status) or "none"
    print(f"Articles: {n} ({breakdown})")
    print(f"SQLite: {config.db_path.stat().st_size:,} bytes | ChromaDB: {dir_bytes(config.chroma_dir):,} bytes | "
          f"Audio: {dir_bytes(config.library / 'audio'):,} bytes")
    print(f"Active sessions: {sessions}")
    print(f"Password: {'set' if config.auth_password_hash else 'NOT SET'} | "
          f"IMAP sync: {'on' if config.imap_enabled else 'off'} | "
          f"Digest schedule: {'on' if config.digest_schedule_enabled else 'off'}")
    mdns_status = f"on ({config.mdns_hostname}.local)" if config.mdns_enabled else "disabled"
    remote_status = config.remote_url if config.remote_url else "not set"
    update_status = "on" if config.update_check_enabled else "off"
    print(f"mDNS: {mdns_status} | Remote URL: {remote_status} | Update check: {update_status}")

    from tiro.llm import resolve_tier
    from tiro.llm_cli import check_cli_backend

    heavy_p, _ = resolve_tier(config, "heavy")
    light_p, _ = resolve_tier(config, "light")
    parts = [f"heavy: {heavy_p}", f"light: {light_p}"]
    for p in {heavy_p, light_p}:
        if p in ("claude-cli", "codex-cli"):
            parts.append(f"{p}: {check_cli_backend(config, p)}")
    print("AI backends: " + " | ".join(parts))


def cmd_set_password(args):
    """Set or reset the Tiro password."""
    import getpass

    from tiro import auth
    from tiro.config import load_config

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"No config file found at {config_path}. Run 'tiro init' first.")
        sys.exit(1)

    password = getpass.getpass("New password (min 8 chars): ")
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        sys.exit(1)

    config = load_config(config_path)
    auth.save_password_hash(config, auth.hash_password(password))
    print(f"Password saved to {config_path}. Existing sessions stay valid until they expire.")


def cmd_token(args):
    """Manage API tokens for non-browser clients (extension, MCP, scripts)."""
    from tiro import auth
    from tiro.config import load_config
    from tiro.database import init_db

    config = load_config(args.config)
    init_db(config.db_path)

    if args.token_command == "create":
        raw = auth.create_api_token(config.db_path, args.name)
        print(f"Created token '{args.name}'. Shown once — store it now.")
        print(f"Token: {raw}")
    elif args.token_command == "list":
        tokens = auth.list_api_tokens(config.db_path)
        if not tokens:
            print("No API tokens. Create one with: tiro token create <name>")
            return
        for t in tokens:
            last = t["last_used_at"] or "never"
            print(f"  [{t['id']}] {t['name']}  created {t['created_at']}  last used {last}")
    elif args.token_command == "revoke":
        if auth.revoke_api_token(config.db_path, args.id):
            print(f"Token {args.id} revoked.")
        else:
            print(f"No token with id {args.id}.")
            sys.exit(1)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(prog="tiro", description="Tiro — reading OS for the AI age")
    # Mirrors run.py's _config_path() and tiro/mcp/server.py's _config_path():
    # honor TIRO_CONFIG (absolute path) as the default so a CLI invoked with a
    # CWD that doesn't contain config.yaml doesn't silently fall back to
    # defaults. An explicit --config still wins (argparse default is only
    # used when the flag is omitted). Regression: run.py made this same
    # mistake once already (see test_run_py_config_path_honors_tiro_config).
    parser.add_argument(
        "--config", default=os.environ.get("TIRO_CONFIG", "config.yaml"),
        help="Path to config.yaml (default: $TIRO_CONFIG or ./config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="Initialize a new Tiro library")

    run_parser = subparsers.add_parser("run", help="Start the Tiro server")
    run_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    run_parser.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 for LAN access (e.g. read on your phone)")
    run_parser.add_argument("--insecure-no-auth", action="store_true",
                            help="Allow --lan without a password (dangerous)")
    run_parser.add_argument("--cert", help="TLS certificate file (must be given with --key)")
    run_parser.add_argument("--key", help="TLS private key file (must be given with --cert)")

    export_parser = subparsers.add_parser("export", help="Export library as a zip bundle")
    export_parser.add_argument("--output", "-o", default="tiro-export.zip", help="Output zip file path")
    export_parser.add_argument("--tag", help="Filter by tag name")
    export_parser.add_argument("--source-id", type=int, help="Filter by source ID")
    export_parser.add_argument("--rating-min", type=int, help="Minimum rating (-1, 1, or 2)")
    export_parser.add_argument("--date-from", help="Filter articles ingested after this date (YYYY-MM-DD)")

    backup_parser = subparsers.add_parser("backup", help="Write a full library snapshot (tar.zst)")
    backup_parser.add_argument("--output", help="Snapshot path (default: {library}/backups/manual/)")
    backup_parser.add_argument("--include-audio", action="store_true", help="Include cached MP3s")

    restore_parser = subparsers.add_parser("restore", help="Replace the library from a snapshot")
    restore_parser.add_argument("snapshot", help="Path to a .tar.zst snapshot")
    restore_parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    restore_parser.add_argument(
        "--force", action="store_true",
        help="Skip the running-server check (restoring while the server is up corrupts state)",
    )

    import_parser = subparsers.add_parser("import-emails", help="Bulk import .eml files")
    import_parser.add_argument("directory", type=Path, help="Directory containing .eml files")

    import_bundle_parser = subparsers.add_parser("import", help="Import a Tiro export bundle (zip)")
    import_bundle_parser.add_argument("bundle", help="Path to a tiro-export zip")
    import_bundle_parser.add_argument(
        "--conflicts", choices=["skip", "overwrite", "keep-both"], default="skip",
        help="What to do when an article already exists (default: skip)",
    )

    instapaper_parser = subparsers.add_parser(
        "import-instapaper", help="Import an Instapaper CSV export (always skips existing)"
    )
    instapaper_parser.add_argument("file", help="Path to the Instapaper CSV export")

    omnivore_parser = subparsers.add_parser(
        "import-omnivore", help="Import an Omnivore export zip (always skips existing)"
    )
    omnivore_parser.add_argument("file", help="Path to the Omnivore export .zip")

    readwise_parser = subparsers.add_parser(
        "import-readwise", help="Import a Readwise JSON export (always skips existing)"
    )
    readwise_parser.add_argument("file", help="Path to the Readwise JSON export")

    delete_parser = subparsers.add_parser("delete", help="Delete an article by id")
    delete_parser.add_argument("id", type=int)

    subparsers.add_parser("setup-email", help="Configure Gmail email integration")
    subparsers.add_parser("check-email", help="Check IMAP inbox for new newsletters")
    subparsers.add_parser("set-password", help="Set or reset the Tiro password")

    agent_parser = subparsers.add_parser("agent", help="List or run registered agents")
    agent_sub = agent_parser.add_subparsers(dest="agent_cmd", required=True)
    agent_sub.add_parser("list", help="List registered agents")
    agent_run = agent_sub.add_parser("run", help="Run an agent by name")
    agent_run.add_argument("name")
    agent_run.add_argument("--input", action="append", default=[],
                           metavar="KEY=VALUE",
                           help="Agent input (JSON value or bare string); repeatable")

    evals_parser = subparsers.add_parser("evals", help="Run the agent evals harness")
    evals_sub = evals_parser.add_subparsers(dest="evals_cmd", required=True)
    evals_run = evals_sub.add_parser("run", help="Run evals (structural/free by default)")
    evals_run.add_argument("agent", nargs="?", default=None)
    evals_run.add_argument("--real", action="store_true",
                           help="Hit real providers (asks for confirmation)")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check consistency across SQLite, ChromaDB, markdown and audio stores",
    )
    doctor_parser.add_argument("--fix", action="store_true",
                               help="Repair findings (stop the server first)")
    doctor_parser.add_argument("--json", action="store_true",
                               help="Machine-readable report")

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Fold external library edits (Obsidian etc.) into the index "
             "(server stopped)",
    )
    reconcile_parser.add_argument("--dry-run", action="store_true",
                                  help="Report what would change; act on nothing")
    reconcile_parser.add_argument("--json", action="store_true",
                                  help="Machine-readable report")

    subparsers.add_parser("status", help="Show library status and store sizes")

    subparsers.add_parser("migrate", help="Apply pending database migrations")

    migrate_lib_parser = subparsers.add_parser(
        "migrate-library",
        help="Copy the library to a new location (old copy is never deleted)",
    )
    migrate_lib_parser.add_argument(
        "dest", nargs="?",
        help="Destination directory (default: platform-standard location)",
    )
    migrate_lib_parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt",
    )

    service_parser = subparsers.add_parser(
        "service",
        help="Run Tiro at login as a background service (launchd / systemd)",
    )
    service_sub = service_parser.add_subparsers(dest="service_command", required=True)
    service_sub.add_parser("install", help="Install + start the service (survives reboot)")
    service_sub.add_parser("uninstall", help="Stop + remove the service")
    service_sub.add_parser("status", help="Show service state + /healthz probe")
    service_logs = service_sub.add_parser("logs", help="Show the service log")
    service_logs.add_argument("--follow", "-f", action="store_true", help="Stream new log lines")

    audit_parser = subparsers.add_parser("audit", help="Show the external-API audit log")
    audit_group = audit_parser.add_mutually_exclusive_group()
    audit_group.add_argument("--date", help="Day to show (YYYY-MM-DD, default today)")
    audit_group.add_argument("--month", help="Month summary (YYYY-MM)")
    audit_parser.add_argument(
        "--service",
        help="Filter by service (anthropic/openai_tts/imap/smtp/openai-compatible/claude-cli/codex-cli)",
    )
    audit_parser.add_argument("--json", action="store_true")

    token_parser = subparsers.add_parser("token", help="Manage API tokens")
    token_sub = token_parser.add_subparsers(dest="token_command", required=True)
    token_create = token_sub.add_parser("create", help="Create a token (shown once)")
    token_create.add_argument("name", help="Token name, e.g. 'chrome-extension'")
    token_sub.add_parser("list", help="List tokens")
    token_revoke = token_sub.add_parser("revoke", help="Revoke a token")
    token_revoke.add_argument("id", type=int, help="Token id from 'tiro token list'")

    args = parser.parse_args()

    # --cert/--key: both-or-neither, enforced as an argparse usage error
    # (not a plain print+exit) so it matches the shape of every other CLI
    # validation error and prints run_parser's own usage line. File-exists
    # validation happens later in cmd_run, since that's a runtime check
    # (the path might be valid syntax but point nowhere), not a parse-time
    # shape check.
    if args.command == "run" and bool(getattr(args, "cert", None)) != bool(getattr(args, "key", None)):
        run_parser.error("--cert and --key must be given together")

    if args.command == "init":
        cmd_init(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "backup":
        cmd_backup(args)
    elif args.command == "restore":
        cmd_restore(args)
    elif args.command == "import-emails":
        cmd_import_emails(args)
    elif args.command == "import":
        cmd_import_bundle(args)
    elif args.command == "import-instapaper":
        cmd_import_instapaper(args)
    elif args.command == "import-omnivore":
        cmd_import_omnivore(args)
    elif args.command == "import-readwise":
        cmd_import_readwise(args)
    elif args.command == "delete":
        cmd_delete(args)
    elif args.command == "setup-email":
        cmd_setup_email(args)
    elif args.command == "check-email":
        cmd_check_email(args)
    elif args.command == "set-password":
        cmd_set_password(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "reconcile":
        cmd_reconcile(args)
    elif args.command == "token":
        cmd_token(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "migrate":
        cmd_migrate(args)
    elif args.command == "migrate-library":
        cmd_migrate_library(args)
    elif args.command == "service":
        cmd_service(args)
    elif args.command == "agent":
        cmd_agent(args)
    elif args.command == "evals":
        cmd_evals(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

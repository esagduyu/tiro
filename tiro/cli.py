"""CLI entry points for Tiro."""

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml


def cmd_init(args):
    """Initialize a new Tiro library."""
    import shutil

    from tiro.config import load_config
    from tiro.database import init_db
    from tiro.vectorstore import init_vectorstore

    # Generate config.yaml from example template if it doesn't exist
    root_config = Path(args.config)
    if not root_config.exists():
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

    app = create_app(config)

    host = effective_host
    url = f"http://localhost:{config.port}"

    if args.lan:
        # create_app() already ran _detect_lan_ips() and populated
        # app.state.lan_ips (same helper) — reuse it here purely to print
        # the reachable URLs, avoiding a second round of socket calls.
        candidate_ips = sorted(app.state.lan_ips)
        if candidate_ips:
            for ip in candidate_ips:
                print(f"LAN mode: accessible at http://{ip}:{config.port}")
        else:
            print("LAN mode: binding to 0.0.0.0 (could not detect LAN IP)")

    if not args.no_browser:
        def open_browser():
            import urllib.request
            # Poll until the server is actually responding (up to 30s)
            for _ in range(60):
                time.sleep(0.5)
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{config.port}/healthz", timeout=1)
                    break
                except Exception:
                    continue
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    print(f"Starting Tiro at {url}")
    uvicorn.run(app, host=host, port=config.port)


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
        for key in ("unreferenced_tags", "unreferenced_entities", "expired_sessions"):
            if report[key]:
                housekeeping_found = True
                print(f"{key}: {report[key]}")
        if report.get("reembed_failures"):
            print(f"reembed_failures: {report['reembed_failures']}")
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


def cmd_migrate(args):
    """Apply pending database migrations."""
    from tiro.config import load_config
    from tiro.migrations import run_migrations

    config = getattr(args, "_config_override", None) or load_config(args.config)
    if not config.db_path.exists():
        print("No Tiro library found. Run `uv run tiro init` first.")
        sys.exit(1)

    applied = run_migrations(config.db_path)
    if applied:
        for line in applied:
            print(f"applied: {line}")
    else:
        print("No pending migrations.")
    sys.exit(0)


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
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="Initialize a new Tiro library")

    run_parser = subparsers.add_parser("run", help="Start the Tiro server")
    run_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    run_parser.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 for LAN access (e.g. read on your phone)")
    run_parser.add_argument("--insecure-no-auth", action="store_true",
                            help="Allow --lan without a password (dangerous)")

    export_parser = subparsers.add_parser("export", help="Export library as a zip bundle")
    export_parser.add_argument("--output", "-o", default="tiro-export.zip", help="Output zip file path")
    export_parser.add_argument("--tag", help="Filter by tag name")
    export_parser.add_argument("--source-id", type=int, help="Filter by source ID")
    export_parser.add_argument("--rating-min", type=int, help="Minimum rating (-1, 1, or 2)")
    export_parser.add_argument("--date-from", help="Filter articles ingested after this date (YYYY-MM-DD)")

    import_parser = subparsers.add_parser("import-emails", help="Bulk import .eml files")
    import_parser.add_argument("directory", type=Path, help="Directory containing .eml files")

    delete_parser = subparsers.add_parser("delete", help="Delete an article by id")
    delete_parser.add_argument("id", type=int)

    subparsers.add_parser("setup-email", help="Configure Gmail email integration")
    subparsers.add_parser("check-email", help="Check IMAP inbox for new newsletters")
    subparsers.add_parser("set-password", help="Set or reset the Tiro password")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check consistency across SQLite, ChromaDB, markdown and audio stores",
    )
    doctor_parser.add_argument("--fix", action="store_true",
                               help="Repair findings (stop the server first)")
    doctor_parser.add_argument("--json", action="store_true",
                               help="Machine-readable report")

    subparsers.add_parser("status", help="Show library status and store sizes")

    subparsers.add_parser("migrate", help="Apply pending database migrations")

    audit_parser = subparsers.add_parser("audit", help="Show the external-API audit log")
    audit_group = audit_parser.add_mutually_exclusive_group()
    audit_group.add_argument("--date", help="Day to show (YYYY-MM-DD, default today)")
    audit_group.add_argument("--month", help="Month summary (YYYY-MM)")
    audit_parser.add_argument("--service", help="Filter by service (anthropic/openai_tts/imap/smtp)")
    audit_parser.add_argument("--json", action="store_true")

    token_parser = subparsers.add_parser("token", help="Manage API tokens")
    token_sub = token_parser.add_subparsers(dest="token_command", required=True)
    token_create = token_sub.add_parser("create", help="Create a token (shown once)")
    token_create.add_argument("name", help="Token name, e.g. 'chrome-extension'")
    token_sub.add_parser("list", help="List tokens")
    token_revoke = token_sub.add_parser("revoke", help="Revoke a token")
    token_revoke.add_argument("id", type=int, help="Token id from 'tiro token list'")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "import-emails":
        cmd_import_emails(args)
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
    elif args.command == "token":
        cmd_token(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "migrate":
        cmd_migrate(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅        |
| < 0.2   | ❌ (hackathon builds — no auth; do not expose them to a network) |

## Threat model (what's in scope)

Tiro is a **local-first, single-user** server. It is designed to run on `127.0.0.1`, or on a trusted LAN / private tailnet behind its built-in password auth. Reports are especially welcome for:

- **Auth bypass** — reaching any non-allowlisted route without a valid session cookie or API token, CSRF on cookie-authenticated mutations, Host-header tricks, session/token handling flaws.
- **Sanitization boundary** — XSS or content injection via saved web pages, `.eml` files, IMAP-ingested newsletters, or LLM output rendered in the UI (server-side nh3 + client-side DOMPurify are both load-bearing).
- **Data integrity/exfiltration** — path traversal via ingestion or export, secrets (API keys, app passwords) leaking into logs, exports, or API responses.

Out of scope: attacks requiring an already-authenticated user or local shell access, running `--insecure-no-auth` (explicitly unsafe by contract), plain-HTTP sniffing on networks you control (HTTPS guidance ships in Phase 3), and vulnerabilities purely in upstream dependencies (report those upstream, though a heads-up is appreciated).

## How to report

Please **do not open a public issue** for vulnerabilities.

1. Preferred: [GitHub private vulnerability reporting](https://github.com/esagduyu/tiro/security/advisories/new) ("Report a vulnerability" on the repo's Security tab).
2. Fallback: email **esagduyu@gmail.com** with "TIRO SECURITY" in the subject.

Include reproduction steps and the version/commit you tested.

## What to expect

Tiro has a single volunteer maintainer, so honest expectations: acknowledgment within **7 days**, an assessment within **14**, and a fix prioritized ahead of all feature work for anything that breaks the auth or sanitization boundary. Coordinated disclosure is appreciated — I'll credit you in the release notes unless you prefer otherwise.

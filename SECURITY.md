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

## Sync encryption

Multi-device sync (0.9.0 `sync-beta`) encrypts everything it puts on a remote backend. The scheme:

- Your passphrase goes through **Argon2id** (per-library random salt and parameters recorded in the backend's plaintext `format.json`) to derive an **X25519 [age](https://age-encryption.org) identity**. The recovery code shown once at setup is that identity — equivalent to the passphrase, guard it the same way.
- Every journal segment, content object, and snapshot blob on the backend is age-encrypted to that recipient. There is no escrow and no server-side key: losing both the passphrase and the recovery code makes the synced data unrecoverable, by design.

**What an attacker with bucket access but without the passphrase sees:** encrypted blobs only — no article text, notes, highlights, or metadata. However, object **count**, **sizes**, and **timing** are visible, and objects are content-addressed by their **plaintext** hash, so *equality* of file versions across time is observable (an attacker can tell a file returned to a previous state, not what it says). This is an accepted posture for a single-user library.

**What the passphrase does not protect against:**

- A **compromised device** — `config.yaml` stores the sync identity (file mode 0600) so background cycles can run without prompting; anyone who can read your config can decrypt your synced data.
- Anyone holding **both** bucket access and the passphrase or recovery code.

`format.json` at the backend root is plaintext and unauthenticated (it has to be readable before any key exists). Devices pin the encryption mode and library id **locally** at setup and refuse downgrades or mismatches afterward — a tampered `format.json` cannot silently switch an encrypted library to plaintext or point a device at a different library.

**Filesystem backends default to encryption off** — the sync folder is your own disk, and if you place it inside a Dropbox/iCloud/Syncthing folder, that transport provides its own encryption. Enable encryption per-backend at setup if the folder itself is hostile territory.

## How to report

Please **do not open a public issue** for vulnerabilities.

1. Preferred: [GitHub private vulnerability reporting](https://github.com/esagduyu/tiro/security/advisories/new) ("Report a vulnerability" on the repo's Security tab).
2. Fallback: email **esagduyu@gmail.com** with "TIRO SECURITY" in the subject.

Include reproduction steps and the version/commit you tested.

## What to expect

Tiro has a single volunteer maintainer, so honest expectations: acknowledgment within **7 days**, an assessment within **14**, and a fix prioritized ahead of all feature work for anything that breaks the auth or sanitization boundary. Coordinated disclosure is appreciated — I'll credit you in the release notes unless you prefer otherwise.

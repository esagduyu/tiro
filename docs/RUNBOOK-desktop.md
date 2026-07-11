# Tiro desktop release runbook (owner-gated)

**Audience:** the repository owner (you). Everything in this file requires an
identity, credential, or account that an automated agent cannot and must not
hold — Apple Developer ID, the GitHub package registry, PyPI, Homebrew. Agents
wrote this file; **agents executed none of it.** Work the steps yourself, in
order, when you're ready to cut the `0.7.0` release to the public.

Each item is: **context** (why) → **exact commands** → **verification** (what
success looks like) → **rollback** (how to back out). Steps are independent
unless noted; do them in the numbered order the first time.

The build itself (unsigned `.app`/`.dmg`, the frozen server) is fully
documented in [`desktop/README.md`](../desktop/README.md) and is **not**
owner-gated — you can build and run it locally today. This runbook is only the
credentialed *distribution* half.

---

## Owner checklist — consolidated Phase 5 O-items

Every "owner must do this on real hardware/accounts" item scattered across the
M5.0–M5.2 reviews, gathered here. Tick them off as you go.

- [ ] **O-1 — Offline first-boot; re-freeze and re-run `smoke.sh` at release time.**
  The frozen binary boots with `HF_HUB_OFFLINE=1` + an empty `HF_HOME` and
  round-trips ingest + semantic search — proven by
  `desktop/pyinstaller/smoke.sh`. The offline layout was fixed + verified in
  M5.0, but the frozen artifact is a build-time snapshot: **re-freeze the server
  and re-run `smoke.sh` at release time** (and after any `.spec`, server, or
  version change) so the shipped `dist/` isn't stale — the smoke script's
  `/healthz` version assertion is the guard. See §1c and §7.
- [ ] **O-2 — Library-migration real-data drill.** Run `tiro migrate-library`
  against a **real** library (not a scratch one), confirm the copy verifies,
  the app reads from the new location, and the **old copy is untouched**. See §6.
- [ ] **O-3 — `tiro service` launchd live round-trip.** Install the service,
  reboot (or log out/in), confirm it comes back up and `/healthz` answers, read
  the logs, uninstall cleanly. See §5.
- [ ] **O-4 — Tauri windowed render on an unlocked display.** Launch the built
  `.app` on a real, unlocked screen (not headless/CI), confirm the window opens,
  reaches `/welcome` (first run) or `/inbox`, the menu works, and Quit kills the
  sidecar (no orphaned `tiro-server`). See §4.
- [ ] **O-5 — ghcr first push + make the package public.** See §2.
- [ ] **O-6 — macOS Developer ID signing + notarization** of the `.app` and
  `.dmg`. See §1. This is the top item — until it lands the release page says
  "beta, unsigned".
- [ ] **O-7 — PyPI publish** (unblocks `uvx tiro` / `uv tool install tiro`). See §3.
- [ ] **O-8 — Homebrew tap** (fast-follow, not blocking the release). See §8.
- [ ] **O-9 — Chrome Web Store** — pre-existing track, pointer only. See §9.
- [ ] **O-10 — Windows signing** — parked, documented only. See §10.

**Release-day order:** build (desktop/README) → §4/§5/§6 real-device drills →
§1 sign+notarize → tag `v0.7.0` + upload artifacts to the GitHub Release (§0) →
§2 ghcr push+public → §3 PyPI → §8/§9 fast-follow.

---

## 0. Tag the release and upload the desktop artifacts

**Context.** The notify-only update check (D5) polls
`api.github.com/repos/esagduyu/tiro/releases/latest`; the **GitHub Release** is
what its banner detects, and it's where users download the `.app`/`.dmg`. The
tag push also auto-fires the ghcr workflow (§2). Do this *after* the real-device
drills pass and *after* signing (§1) so the uploaded artifacts are the signed
ones.

**Commands.**
```bash
# from a clean main with the Phase 5 PR merged:
git checkout main && git pull
git tag -a v0.7.0 -m "Tiro 0.7.0 — desktop-beta (Phase 5)"
git push origin v0.7.0

# create the Release and attach the (signed) artifacts:
gh release create v0.7.0 \
  --title "Tiro 0.7.0 — desktop-beta" \
  --notes-file CHANGELOG.md \
  "desktop/tauri/src-tauri/target/release/bundle/dmg/Tiro_0.7.0_aarch64.dmg" \
  "desktop/tauri/src-tauri/target/release/bundle/macos/Tiro.app.zip"
```
(Zip the `.app` first: `ditto -c -k --keepParent Tiro.app Tiro.app.zip`.)

**Verification.**
- `gh release view v0.7.0` lists both assets.
- `curl -s https://api.github.com/repos/esagduyu/tiro/releases/latest | jq .tag_name`
  returns `"v0.7.0"`.
- On a running older Tiro, the "update available" banner appears within a day
  (or restart it to force a first-cycle check).

**Rollback.** `gh release delete v0.7.0 --cleanup-tag` removes the release and
the tag. (If the ghcr workflow already published, delete those image versions
too — §2 rollback.)

---

## 1. macOS Developer ID signing + notarization (O-6, the top item)

**Context.** The 0.7.0 `.app`/`.dmg` build unsigned, so Gatekeeper blocks them
on first open (right-click → Open is the documented beta workaround). Signing
with a **Developer ID Application** certificate + Apple **notarization** removes
that friction. The app embeds the PyInstaller server as a sidecar, so the
**hardened runtime** must be enabled with the entitlements PyInstaller/Python
need (JIT-free, but the unsigned-executable-memory + dyld-env entitlements are
required for a frozen Python that loads native extensions like torch/chromadb).

### 1a. Procure the certificate (one-time)
- Enroll in / confirm the **Apple Developer Program** ($99/yr) at
  developer.apple.com.
- In Xcode → Settings → Accounts → Manage Certificates → **+** → **Developer ID
  Application**. (Or create a CSR in Keychain Access and upload it in the
  developer portal → Certificates.)
- Confirm it's installed: `security find-identity -v -p codesigning` lists a
  `Developer ID Application: Your Name (TEAMID)` line. Note the **TEAMID**.

### 1b. Entitlements file
Create `desktop/tauri/src-tauri/entitlements.plist` (Tauri picks it up via
`bundle.macOS.entitlements` in `tauri.conf.json` — add that key):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.cs.allow-jit</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
</dict></plist>
```
`disable-library-validation` is the load-bearing one: the frozen server loads
third-party native `.dylib`s (torch, chromadb, tokenizers) that are not signed
by your team; without it the hardened runtime refuses to load them.

### 1c. Sign + notarize via Tauri (preferred)
Tauri signs and (optionally) notarizes during `tauri build` when these env vars
are set:
```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_ID="you@example.com"
export APPLE_PASSWORD="app-specific-password"   # appleid.apple.com → App-Specific Passwords
export APPLE_TEAM_ID="TEAMID"

cd desktop/pyinstaller && uv run pyinstaller --noconfirm --clean tiro-server.spec
cd ../tauri && npm install
npx tauri build --bundles app,dmg
```
Tauri runs `codesign` (hardened runtime + your entitlements, deep) and, with the
`APPLE_*` vars present, submits to `notarytool` and staples automatically.

### 1d. Manual sign + notarize (fallback, if Tauri's path fails)
```bash
APP="desktop/tauri/src-tauri/target/release/bundle/macos/Tiro.app"
# sign the embedded sidecar first (inside-out), then the app:
codesign --force --options runtime --timestamp \
  --entitlements desktop/tauri/src-tauri/entitlements.plist \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  "$APP/Contents/Resources/tiro-server/tiro-server"
codesign --force --options runtime --timestamp --deep \
  --entitlements desktop/tauri/src-tauri/entitlements.plist \
  --sign "Developer ID Application: Your Name (TEAMID)" "$APP"

# notarize the .dmg (build it after signing the .app):
DMG="desktop/tauri/src-tauri/target/release/bundle/dmg/Tiro_0.7.0_aarch64.dmg"
xcrun notarytool submit "$DMG" \
  --apple-id "you@example.com" --team-id "TEAMID" \
  --password "app-specific-password" --wait
xcrun stapler staple "$DMG"
xcrun stapler staple "$APP"
```

**Verification.**
```bash
codesign --verify --deep --strict --verbose=2 "$APP"        # → "valid on disk"
spctl -a -vvv -t install "$APP"                              # → "accepted / Notarized Developer ID"
xcrun stapler validate "$DMG"                                # → "The validate action worked!"
```
Best final proof: download the notarized `.dmg` on a **different** Mac that has
never seen this build and confirm it opens with a normal double-click (no
right-click-Open).

**Rollback.** Signing is non-destructive to source. If a build is bad, rebuild
from the unsigned recipe. Revoking a leaked cert is done in the developer
portal; a revoked cert invalidates future Gatekeeper checks but already-notarized
artifacts keep their stapled ticket.

**After success:** flip the README "Install Tiro" desktop bullet from
"Unsigned beta … right-click → Open" to the normal-download wording, and drop
the "beta, unsigned" note on the Release page.

---

## 2. ghcr first push + make the package public (O-5)

**Context.** `.github/workflows/docker.yml` is committed and builds a multi-arch
(`amd64`+`arm64`) image to `ghcr.io/esagduyu/tiro`. It triggers automatically on
a `v*` tag push (so §0's `git push origin v0.7.0` fires it), plus
`workflow_dispatch` for a supervised first run. The **first** push needs a
one-time permissions + visibility check that only the owner can do.

**Critical gotcha — `workflow_dispatch` must target the tag.** The image tags
(`0.7.0`, `0.7`, `latest`) come from `docker/metadata-action`'s `type=semver`
rule, which **only emits version tags when the workflow runs on a tag ref.** If
you trigger `workflow_dispatch` from a **branch**, you get no version tags and a
useless image. So either let the tag push auto-fire it, or in the Actions "Run
workflow" dropdown select **the `v0.7.0` tag** (not `main`) as the ref.

**Pre-flight (one-time, in the GitHub UI).**
1. Repo → Settings → Actions → General → Workflow permissions → confirm
   **Read and write permissions** (the workflow also declares
   `permissions: packages: write`, but the repo setting must allow it).
2. First run will create the package `esagduyu/tiro` under Packages.

**Trigger + watch.**
```bash
# Option A (normal): the tag push from §0 already started it. Watch:
gh run watch --repo esagduyu/tiro
# Option B (supervised manual): MUST pass the tag as ref
gh workflow run docker.yml --repo esagduyu/tiro --ref v0.7.0
gh run watch --repo esagduyu/tiro
```

**Make the package public + link it to the repo (one-time, UI).**
- github.com/users/esagduyu/packages/container/tiro/settings →
  **Change visibility → Public**.
- Same page → **Connect Repository** → `esagduyu/tiro` (so the package shows on
  the repo and inherits its README).

**Verification (both arches).**
```bash
docker pull ghcr.io/esagduyu/tiro:0.7.0
docker manifest inspect ghcr.io/esagduyu/tiro:0.7.0 \
  | jq '.manifests[].platform'          # → linux/amd64 AND linux/arm64
docker run --rm ghcr.io/esagduyu/tiro:0.7.0 uv run tiro status || true
# arm64 leg (on Apple silicon it's native; elsewhere QEMU):
docker pull --platform linux/arm64 ghcr.io/esagduyu/tiro:0.7.0
```

**Rollback.** Delete the bad image version at the package settings page
(Package → versions → delete), or `gh api -X DELETE
/user/packages/container/tiro/versions/<id>`. The mutable `latest`/`0.7` tags
move forward on the next good push.

---

## 3. PyPI publish (O-7)

**Context.** `uvx tiro` / `uv tool install tiro` in the README pull from PyPI;
until the first publish they don't work (README already says so). One-time
trusted-publisher setup is recommended over long-lived tokens.

**Setup (one-time).** On pypi.org: create the `tiro` project (or reserve the
name), then Project → Publishing → add a **Trusted Publisher** (GitHub, repo
`esagduyu/tiro`) — or generate a scoped API token.

**Commands.**
```bash
uv build                      # → dist/tiro-0.7.0.tar.gz + tiro-0.7.0-py3-none-any.whl
uv publish                    # uses UV_PUBLISH_TOKEN, or --token pypi-...
# sanity-check first on TestPyPI:
uv publish --publish-url https://test.pypi.org/legacy/ --token <testpypi-token>
```

**Verification.**
```bash
uvx tiro@0.7.0 --help          # resolves + runs from PyPI
uv tool install tiro && tiro status
```

**Rollback.** PyPI releases are **immutable** — you cannot overwrite `0.7.0`.
A bad upload means yanking it (`pypi.org` → release → Yank, which hides it from
new resolves but doesn't delete) and publishing a fixed `0.7.1`. Test on
TestPyPI first precisely because of this.

---

## 4. Tauri windowed render on an unlocked display (O-4)

**Context.** Window rendering and the sidecar lifecycle can only be verified on
a real, unlocked GUI session — not headless CI. This is the "does the app
actually open and quit cleanly" drill.

**Commands.**
```bash
open "desktop/tauri/src-tauri/target/release/bundle/macos/Tiro.app"
```

**Verification.**
- Window opens and lands on `/welcome` (first run on this machine) or
  `/inbox` (after setup).
- Menu → About shows `0.7.0`; Preferences… navigates to `/settings`.
- If port 8000 is free it's used; if occupied, the app still opens on a fallback
  port (the Chrome extension won't reach it in that case — expected).
- **Quit the app, then confirm no orphan:** `pgrep -fl tiro-server` prints
  nothing. (The explicit process-group kill is the thing under test.)

**Rollback.** N/A (read-only drill). If an orphan survives, `pkill -f
tiro-server` and file it — the kill handler regressed.

---

## 5. `tiro service` launchd live round-trip (O-3)

**Context.** The service CLI writes a launchd user agent (macOS) / systemd user
unit (Linux). Only a real install + reboot proves it survives a restart. Do this
with the **`uv tool`/from-source** install, **not** the desktop app (don't run
both at once).

**Commands (macOS).**
```bash
tiro service install
tiro service status          # launchd state + a /healthz probe → healthy
# reboot (or log out and back in), then:
tiro service status          # still healthy after restart
tiro service logs -f         # streams ~/Library/Logs/Tiro/tiro.log
tiro service uninstall
```
(Linux: same verbs; on a headless box run `loginctl enable-linger $USER` first
so the user unit survives logout.)

**Verification.** `status` reports the service loaded/running and `/healthz`
returns 200 both before and after the reboot; `uninstall` removes the plist and
`launchctl list | grep com.tiro` is empty afterward.

**Rollback.** `tiro service uninstall` (idempotent — safe even if nothing is
installed). Manually: `launchctl bootout gui/$UID
~/Library/LaunchAgents/com.tiro.app.plist && rm
~/Library/LaunchAgents/com.tiro.app.plist`.

---

## 6. Library-migration real-data drill (O-2)

**Context.** `migrate-library` copies-then-verifies and **never removes** the
source. Prove it on a real library once, since the whole feature is about not
losing data. Server **stopped** (copying a live DB corrupts it).

**Commands.**
```bash
# stop any running server / service first.
uv run tiro migrate-library            # dest defaults to the platform-standard path
# or an explicit dest:
uv run tiro migrate-library /Volumes/data/tiro-lib
```

**Verification.**
- The tool prints "Old library preserved at `<source>`" and the per-file verify
  passed.
- `config.yaml`'s `library_path` now points at the destination.
- Start Tiro: your articles, highlights, digests are all present.
- **The old directory is byte-for-byte intact** (`ls` it — nothing moved or
  deleted).
- Interrupted-run safety (optional): `Ctrl-C` mid-copy, re-run — it clears the
  partial dest (marker present) and restarts; the source is never touched.

**Rollback.** Because the source is untouched, rollback is just editing
`library_path` back to the old path in `config.yaml` (through the app or by
hand) and deleting the partial/failed destination. No data was moved.

---

## 7. Re-run the offline smoke gate after any `.spec` change (O-1)

**Context.** The offline first-boot guarantee (O-1) is already verified, but the
`.spec` model-staging invariant (true snapshot names, not blob hashes) is easy
to break. Re-run the structural guard after touching the spec or the model
bundling.

**Commands.**
```bash
cd desktop/pyinstaller
uv run pyinstaller --noconfirm --clean tiro-server.spec
./smoke.sh            # boots with HF_HUB_OFFLINE=1 + empty HF_HOME
```

**Verification.** `smoke.sh` exits 0 ("GATE PASSED") — it ingested a URL and
found it via semantic search with zero network, proving the seeded model loaded
offline.

**Rollback.** N/A (build/test only).

---

## 8. Homebrew tap (O-8, fast-follow — not blocking the release)

**Context.** A `brew install esagduyu/tap/tiro` convenience path. Fast-follow
after PyPI (the formula can install the published wheel via a Python virtualenv).

**Commands (sketch).**
```bash
gh repo create esagduyu/homebrew-tap --public
# add Formula/tiro.rb (python-virtualenv formula pointing at the PyPI sdist),
# with the sha256 of dist/tiro-0.7.0.tar.gz:
shasum -a 256 dist/tiro-0.7.0.tar.gz
brew install --build-from-source ./Formula/tiro.rb   # local test
brew audit --strict --new tiro
```

**Verification.** `brew install esagduyu/tap/tiro && tiro status` on a clean
machine.

**Rollback.** Delete or revert the formula commit in the tap repo; taps are just
git repos.

---

## 9. Chrome Web Store (O-9, pointer only)

The extension under `extension/` has a pre-existing publishing track (see the
Chrome Extension section of the README). Nothing in Phase 5 changed it — this is
a pointer, not a new procedure. Bump the extension `manifest.json` version in
lockstep only if you ship extension changes with this release.

---

## 10. Windows signing (O-10, parked)

Documented, not built for 0.7.0. The Windows path is PyInstaller + `nssm` for
run-at-login (`tiro service install` prints the recipe). An Authenticode signing
certificate (EV or standard) + `signtool sign /fd sha256 /tr <RFC3161-URL> /td
sha256` would sign a future Windows build. Parked until a Windows binary is on
the roadmap.

---

*Agents wrote this runbook and verified nothing against live services. Execute
it yourself.*

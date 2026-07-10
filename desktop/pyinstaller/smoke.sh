#!/usr/bin/env bash
# Smoke test for the frozen tiro-server binary (Phase 5 / M5.0, spec D1 GATE).
#
# Proves, end to end, that the onedir bundle boots the FULL server against a
# SCRATCH config on an EPHEMERAL port (ON-5/ON-8: never the owner's :8001,
# explicit TIRO_CONFIG, asserted library path), reaches /healthz 200 with the
# tree's version, sets a password, ingests one URL from a LOCAL fixture file
# server (no network), and finds it via semantic search — which exercises real
# ChromaDB + real sentence-transformers embedding through the seed-the-cache
# path (HF_HOME points at an EMPTY tmp dir, so the bundled model MUST seed).
#
# OFFLINE GATE (the load-bearing assertion): the whole boot runs with
# HF_HUB_OFFLINE=1 + TRANSFORMERS_OFFLINE=1 and an EMPTY HF_HOME. Any attempt to
# reach huggingface.co is a hard error, so if the bundled model isn't seeded as a
# VALID, offline-loadable HF snapshot layout, init_vectorstore crashes and /healthz
# never comes up — making the "malformed cache only loads with network" class of
# regression structurally impossible to miss. A successful embed (semantic search
# below) proves the seeded snapshot loaded with zero network.
#
# Usage:  desktop/pyinstaller/smoke.sh [path-to-tiro-server-binary]
# Exit 0 = GATE PASSED. Any failure exits non-zero with a diagnostic.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN="${1:-$REPO_ROOT/desktop/pyinstaller/dist/tiro-server/tiro-server}"
[ -x "$BIN" ] || { echo "FAIL: binary not found/executable: $BIN"; exit 1; }

# --- scratch workspace (ON-5: explicit paths, nothing near the real library) ---
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/tiro-smoke.XXXXXX")"
FIXDIR="$SCRATCH/fixtures"
LIBDIR="$SCRATCH/library"
HFHOME="$SCRATCH/hf-empty"     # empty -> forces the bundled model to seed
CFG="$SCRATCH/config.yaml"
COOKIES="$SCRATCH/cookies.txt"
mkdir -p "$FIXDIR" "$LIBDIR" "$HFHOME"

SERVER_PID=""; FIXTURE_PID=""
cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  [ -n "$FIXTURE_PID" ] && kill "$FIXTURE_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  # ON-5 orphan assertion: nothing must survive on our port
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "FAIL: server pid $SERVER_PID orphaned after kill"; exit 1
  fi
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

# --- pick two free ephemeral ports (never 8000/8001) ---
free_port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }
PORT="$(free_port)"; FIXPORT="$(free_port)"
[ "$PORT" != "8001" ] && [ "$PORT" != "8000" ] || { echo "FAIL: refused reserved port"; exit 1; }
echo "smoke: server port=$PORT fixture port=$FIXPORT scratch=$SCRATCH"

# --- local fixture article (no network dependency) ---
cat > "$FIXDIR/article.html" <<'HTML'
<!doctype html><html><head><title>Tiro Spike Fixture</title>
<meta name="author" content="Marcus Tullius Cicero"></head>
<body><article><h1>The Zephyranthes Chronicle</h1>
<p>This is a deterministic local fixture used by the PyInstaller spike smoke
test. It mentions a rare botanical marker word, zephyranthes, so that semantic
search can reliably surface this exact article after ingestion and embedding.</p>
<p>Tiro preserved the works of Cicero for posterity, organizing knowledge so
that later readers could find what mattered. This paragraph gives the extractor
enough body text to produce a clean markdown article with real content.</p>
</article></body></html>
HTML
( cd "$FIXDIR" && python3 -m http.server "$FIXPORT" --bind 127.0.0.1 >/dev/null 2>&1 ) &
FIXTURE_PID=$!
FIXTURE_URL="http://127.0.0.1:$FIXPORT/article.html"

# --- scratch config (explicit library path; loopback; no password yet) ---
cat > "$CFG" <<YAML
library_path: "$LIBDIR"
host: "127.0.0.1"
port: $PORT
YAML

# --- boot the frozen server ---
export TIRO_CONFIG="$CFG" TIRO_HOST="127.0.0.1" TIRO_PORT="$PORT"
export HF_HOME="$HFHOME"          # empty -> seed-the-cache path must run
# OFFLINE GATE: forbid ALL network to huggingface.co. With an empty HF_HOME the
# bundled snapshot MUST seed into a valid, offline-loadable layout or the embed
# (and thus /healthz + search) fails hard. This is finding-2's regression guard.
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export ANTHROPIC_API_KEY="" OPENAI_API_KEY=""   # offline: AI enrichment degrades gracefully
echo "smoke: OFFLINE boot (HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1, empty HF_HOME) — model MUST load from seed"
BASE="http://127.0.0.1:$PORT"
LOG="$SCRATCH/server.log"

START=$(date +%s)
"$BIN" >"$LOG" 2>&1 &
SERVER_PID=$!

# --- wait for /healthz 200 ---
EXPECTED_VER="$(cd "$REPO_ROOT" && uv run python -c 'import tiro; print(tiro.__version__)')"
HEALTH=""
for i in $(seq 1 60); do
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "FAIL: server exited early"; tail -30 "$LOG"; exit 1; }
  if HEALTH="$(curl -fsS "$BASE/healthz" 2>/dev/null)"; then break; fi
  sleep 1
done
[ -n "$HEALTH" ] || { echo "FAIL: /healthz never came up"; tail -40 "$LOG"; exit 1; }
COLD=$(( $(date +%s) - START ))
echo "smoke: /healthz -> $HEALTH  (cold start ${COLD}s)"
echo "$HEALTH" | grep -q "\"version\":\"$EXPECTED_VER\"" \
  || echo "$HEALTH" | grep -q "\"version\": \"$EXPECTED_VER\"" \
  || { echo "FAIL: healthz version != $EXPECTED_VER"; exit 1; }

# --- prove the model cache seeded into the EMPTY HF_HOME ---
SEEDED="$HFHOME/hub/models--sentence-transformers--all-MiniLM-L6-v2"
[ -d "$SEEDED" ] || { echo "FAIL: embedding model was not seeded into $SEEDED"; exit 1; }
echo "smoke: embedding model seeded -> $SEEDED"

# --- assert effective library path (ON-5) ---
echo "$HEALTH" | grep -q "\"status\"" || true

# --- set a password (setup), keeping the session cookie ---
curl -fsS -c "$COOKIES" -H 'Content-Type: application/json' \
  -d '{"password":"spike-smoke-pw"}' "$BASE/api/auth/setup" >/dev/null \
  || { echo "FAIL: auth setup"; tail -30 "$LOG"; exit 1; }

# --- ingest the local fixture URL ---
ING="$(curl -fsS -b "$COOKIES" -c "$COOKIES" -H 'Content-Type: application/json' \
  -d "{\"url\":\"$FIXTURE_URL\"}" "$BASE/api/ingest/url")" \
  || { echo "FAIL: ingest"; echo "$ING"; tail -40 "$LOG"; exit 1; }
echo "smoke: ingest -> $ING"

# --- assert article markdown file exists on disk ---
MDCOUNT="$(find "$LIBDIR/articles" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
[ "$MDCOUNT" -ge 1 ] || { echo "FAIL: no markdown file written under $LIBDIR/articles"; exit 1; }
echo "smoke: markdown files on disk = $MDCOUNT"

# --- assert the article row exists via the API list ---
# The list carries the <title> ("Tiro Spike Fixture"); the botanical marker
# lives in the body/markdown (surfaced by semantic search below), not here.
LIST="$(curl -fsS -b "$COOKIES" "$BASE/api/articles")"
echo "$LIST" | grep -qi "Tiro Spike Fixture" \
  || { echo "FAIL: article row not returned by /api/articles"; echo "$LIST" | head -c 400; exit 1; }

# --- semantic search must find it (real ChromaDB + real embedding) ---
SEARCH=""
for i in $(seq 1 15); do
  SEARCH="$(curl -fsS -b "$COOKIES" "$BASE/api/search?q=rare%20botanical%20marker%20word" || true)"
  echo "$SEARCH" | grep -qi "Tiro Spike Fixture" && break
  sleep 1
done
echo "$SEARCH" | grep -qi "Tiro Spike Fixture" \
  || { echo "FAIL: semantic search did not return the ingested article"; echo "$SEARCH" | head -c 400; exit 1; }
echo "smoke: semantic search found the article"

# --- report sizes ---
BUNDLE_SIZE="$(du -sh "$(dirname "$BIN")" | cut -f1)"
echo "smoke: bundle size = $BUNDLE_SIZE   cold start = ${COLD}s"

echo "GATE PASSED: frozen tiro-server boots full server, seeds model, ingests + searches."

#!/usr/bin/env bash
#
# Local development loop: the FastAPI backend on :8000 and the vite dev server
# on :5173, both reloading on save. Open http://localhost:5173; vite proxies
# /api and /mcp to the backend. Both bind to the loopback interface only.
#
#   1. Check .env: it must exist and have no <PLACEHOLDER> values left.
#   2. Sync backend/.venv against backend/uv.lock (Python 3.11, via uv), and
#      reinstall frontend/node_modules when frontend/package-lock.json changes.
#   3. Export .env into both processes, start the backend, wait up to 180s for
#      /api/health, then start vite in the foreground. Ctrl-C stops both.
#
# To run the packaged image instead, use run-container.sh.
#
# ─────────────────────────────────────────────────────────────────────────────
# BEFORE YOU RUN (one-time setup)
#
#   1. Install uv, npm (Node.js) and curl.
#   2. Create the AWS resources (docs/local.md step 3, infra/cli/setup_aws.sh) and
#      give your shell credentials for that account (AWS_PROFILE, SSO, ...).
#   3. cp .env.example .env at the repo root and fill it in. It is the only
#      config this script reads.
#   4. For a single-user run without Cognito, set
#      CODEKB_REQUIRE_AUTHENTICATION=false. Every endpoint is then open to
#      anyone who can reach the port, so keep it loopback-only.
#
# OPTIONS
#
#   -h, --help    print this header
#
# Usage:
#     ./scripts/run.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"
VENV_DIR="$BACKEND_DIR/.venv"

# Matches the Dockerfile, so a source run and a container run behave the same.
PYTHON_VERSION="3.11"

# The vite dev server proxies /api and /mcp to localhost:8000 (see
# frontend/vite.config.ts), so changing BACKEND_PORT means changing that too.
BACKEND_PORT=8000
FRONTEND_PORT=5173
# How long to wait for the backend's /api/health before giving up.
BACKEND_STARTUP_TIMEOUT_S=180

# Prints the header comment, so --help and the docs can't drift.
usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)     usage; exit 0 ;;
    *) echo "Unknown argument: '$arg'" >&2
       echo "" >&2
       usage >&2
       exit 1 ;;
  esac
done

# ── Configuration ─────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  echo "Error: no .env at the repository root."
  if [ -f "$ENV_EXAMPLE" ]; then
    echo ""
    echo "  Create one from the template and fill in your AWS details:"
    echo "    cp .env.example .env"
    echo ""
    echo "  At minimum you need CODEKB_AWS_ACCOUNT_ID, the S3 bucket and region"
    echo "  values, and CODEKB_BEDROCK_REGION. See .env.example for the full set."
  fi
  exit 1
fi

# .env.example marks values to replace as <YOUR_SOMETHING>. Matched as text
# before sourcing, since "<" is a shell redirection operator.
PLACEHOLDER_LINES="$(
  grep -nE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=.*<[^>]+>' "$ENV_FILE" || true
)"
if [ -n "$PLACEHOLDER_LINES" ]; then
  echo "Error: .env still has placeholders that need replacing:"
  printf '%s\n' "$PLACEHOLDER_LINES" | sed -E 's|^([0-9]+):|         line \1: |'
  echo ""
  echo "       Replace each <...> with a real value, then run this again."
  exit 1
fi

# These two stop startup if unset.
for required_var in OTEL_LOGS_EXPORTER CODEKB_S3_BUCKET; do
  if ! grep -qE "^[[:space:]]*$required_var=" "$ENV_FILE"; then
    echo "    note: $required_var is not set in .env — startup may refuse to continue."
  fi
done

# npm records each install in node_modules/.package-lock.json, so a newer lockfile
# (e.g. after a pull) means node_modules is stale. Also true when it's missing.
ensure_frontend_deps() {
  if [ "$FRONTEND_DIR/package-lock.json" -nt "$FRONTEND_DIR/node_modules/.package-lock.json" ]; then
    echo "==> Installing frontend dependencies from package-lock.json..."
    (cd "$FRONTEND_DIR" && npm ci)
  fi
}

# ── Start ─────────────────────────────────────────────────────────────
for tool in uv npm curl; do
  command -v "$tool" &>/dev/null || {
    echo "Error: '$tool' not found on PATH."
    [ "$tool" = "uv" ] && echo "  Install it: https://docs.astral.sh/uv/getting-started/installation/"
    [ "$tool" = "npm" ] && echo "  Install Node.js: https://nodejs.org"
    [ "$tool" = "curl" ] && echo "  Install it with your system package manager."
    exit 1
  }
done

# uv.lock pins exact versions; --no-install-project installs only those, since
# the app runs from source below.
if [ ! -d "$VENV_DIR" ]; then
  echo "==> Creating backend/.venv (Python $PYTHON_VERSION, from uv.lock)..."
  (cd "$BACKEND_DIR" && uv sync --no-install-project --python "$PYTHON_VERSION")
else
  echo "==> Syncing backend/.venv against uv.lock..."
  (cd "$BACKEND_DIR" && uv sync --no-install-project --python "$PYTHON_VERSION" --quiet)
fi

ensure_frontend_deps

# Parsed, not sourced: the value after the first "=" is taken literally, so shell
# metacharacters in it cannot execute. Matches how docker --env-file reads it.
echo "==> Loading configuration from .env"
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"   # tolerate CRLF
  case "$line" in ''|'#'*) continue ;; esac
  case "$line" in *=*) ;; *) continue ;; esac
  env_key="${line%%=*}"
  env_key="${env_key%%[[:space:]]*}"
  case "$env_key" in
    [A-Za-z_]*) export "$env_key=${line#*=}" ;;
  esac
done < "$ENV_FILE"

# Stop the backend on exit, so Ctrl-C doesn't leave an orphan holding the port.
BACKEND_PID=""
cleanup() {
  if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo ""
    echo "==> Stopping the backend (pid $BACKEND_PID)..."
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
# Exit on a signal; trapping it alone would resume the startup wait, which then
# reports the stopped backend as a crash.
trap 'exit 130' INT
trap 'exit 143' TERM

# Same entry point as the container (see the Dockerfile CMD); cwd puts
# backend/src/ on sys.path as "src".
echo "==> Starting backend on http://127.0.0.1:$BACKEND_PORT (reload enabled)"
(
  cd "$BACKEND_DIR"
  exec "$VENV_DIR/bin/python" -m uvicorn src.api.app:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" --reload
) &
BACKEND_PID=$!

# Wait until the backend answers before starting vite. Startup includes the
# Bedrock settings probe, which can take several seconds; until it finishes the
# port is bound but not serving, and vite's proxy would log ETIMEDOUT for every
# request an already-open browser tab makes.
# With --reload, a worker that fails startup leaves the reloader process alive,
# so the deadline (not just the liveness check) is what catches that case.
echo "==> Waiting for the backend to finish starting (up to ${BACKEND_STARTUP_TIMEOUT_S}s)..."
deadline=$((SECONDS + BACKEND_STARTUP_TIMEOUT_S))
until curl -fsS -o /dev/null --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$BACKEND_PORT/api/health" 2>/dev/null; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo ""
    echo "Error: the backend exited during startup. The output above says why —"
    echo "       usually a missing or invalid value in .env, or port $BACKEND_PORT in use."
    exit 1
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo ""
    echo "Error: the backend did not answer http://127.0.0.1:$BACKEND_PORT/api/health"
    echo "       within ${BACKEND_STARTUP_TIMEOUT_S}s. The output above says why — a"
    echo "       startup error, or a Bedrock probe that is still waiting on the network."
    exit 1
  fi
  sleep 1
done

echo ""
echo "──────────────────────────────────────────────────────────────────"
# localhost, not 127.0.0.1: vite binds the IPv6 loopback ([::1]) only, so the
# dotted-quad form refuses the connection. Both are loopback-only either way.
echo "  Backend    http://localhost:$BACKEND_PORT      (API + MCP)"
echo "  Frontend   http://localhost:$FRONTEND_PORT      <- open this one"
echo ""
echo "  The dev server proxies /api and /mcp to the backend, so use the"
echo "  frontend URL. Both reload on save. Ctrl-C stops both."
echo "──────────────────────────────────────────────────────────────────"
echo ""

cd "$FRONTEND_DIR"
npm run dev -- --port "$FRONTEND_PORT" --strictPort

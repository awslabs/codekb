#!/usr/bin/env bash
#
# Regression matrix: run the black-box suite twice against a locally-started
# CodeKB backend, once with authorization OFF and once with it ON, and report a
# combined result. This exists because the two modes exercise different,
# mutually-exclusive backend paths (see codekb_regression.py "phases" docs): the
# authz layer + scope resolution + list filtering only run with authz on, while
# the route-native 404/NOT_FOUND handling and the agent's unfiltered all-spaces
# search only run with authz off. One pass can't cover both.
#
# How it works, per mode:
#   1. Start the backend directly (python -m uvicorn src.api.app:app), NOT via
#      scripts/run.sh — run.sh also starts the frontend and blocks on it, and it
#      re-exports the repo-root .env over the shell. Starting uvicorn directly
#      lets us control config cleanly.
#   2. Point the backend at your server .env with CODEKB_DOTENV, then override
#      only the auth flags via shell exports. The backend loads .env with
#      override=False, so these exports win:
#         CODEKB_REQUIRE_AUTHENTICATION=false  (no Cognito -> X-User-Identity is
#                                               honored, so the suite can use two
#                                               distinct identities)
#         CODEKB_REQUIRE_AUTHORIZATION=false|true  (the matrix variable)
#      Cognito vars are forced empty so a server .env that sets them can't flip
#      the backend into JWT mode (which would 503 without a real pool).
#   3. Wait for GET /api/health, run the suite, then stop the backend.
#
# AWS credentials come from the surrounding environment (AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN / AWS_REGION) and are inherited by
# the backend. Everything past the read-only checks needs S3 / S3 Vectors /
# Bedrock access, so both passes incur real Bedrock cost.
#
# Usage:
#   tests/e2e/run_regression_matrix.sh --server-env /path/to/server.env [options]
#
# Options:
#   --server-env PATH   Backend .env (S3 buckets, region, Bedrock model, git
#                       hosts, ...). Required. Must NOT contain placeholders.
#   --port N            Backend port (default: 8000).
#   --with-overviews    Include the slow Space/Hyperspace Overview phases in
#                       both passes.
#   --phase LIST        Pass-through to the suite (comma-separated phases),
#                       e.g. a lighter authz-off pass. Applies to BOTH passes.
#   --only off|on       Run just one mode instead of both.
#   -h, --help          This help.
#
# ── Testing the deployed CloudFront instance (separate, manual) ─────────
# This matrix is LOCAL-only: it starts the backend from source and toggles
# authorization between two passes. It cannot cover the hosted surface. To test
# a deployed CloudFront instance, run the suite DIRECTLY (not this script)
# against its URL. That is the only way to exercise the real Cognito JWT path
# (issuer/audience/signature), CloudFront + WAF + edge headers, and the bundled
# SPA — none of which the local matrix touches.
#
#   # smoke only: no writes, no Bedrock cost (verifies auth + edge)
#   CODEKB_BASE_URL=https://<your-cloudfront-host>/ CODEKB_TOKEN='<fresh JWT>' \
#     python3 tests/e2e/codekb_regression.py --no-color \
#       --phase readonly,negative,headers
#
#   # lifecycle + MCP (creates and deletes real spaces; Bedrock cost). Excludes
#   # agent + refresh: CloudFront 504s the long agent impact/SSE calls, and the
#   # no-op refresh records no run.
#   CODEKB_BASE_URL=https://<your-cloudfront-host>/ CODEKB_TOKEN='<fresh JWT>' \
#     python3 tests/e2e/codekb_regression.py --no-color \
#       --phase readonly,negative,headers,git-space,queries,upload,hyperspaces,mcp
#
# Cloud-run notes:
#   - Use a FRESH JWT each run; Cognito id/access tokens expire ~1h after issue.
#   - Cross-identity authz and MCP-authz checks SKIP on a single JWT. To exercise
#     them, add --second-token '<a second Cognito user's JWT>'.
#   - It writes to a shared, populated deployment, so run it deliberately. The
#     deletes fire regardless of assertion outcomes, so it cleans up after itself.
#
# Exit code: 0 only if every mode that ran passed; non-zero otherwise.

set -uo pipefail

# ── Resolve paths ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"          # .../src/AwsCodeKB
BACKEND_DIR="$PKG_ROOT/backend"
VENV_PYTHON="$BACKEND_DIR/.venv/bin/python"
SUITE="$SCRIPT_DIR/codekb_regression.py"
LOG_DIR="${TMPDIR:-/tmp}/codekb-regression-matrix"

# ── Defaults and arg parsing ──────────────────────────────────────────
SERVER_ENV=""
PORT=8000
WITH_OVERVIEWS=""
PHASES=""
ONLY=""

usage() { sed -n '2,/^# Exit code/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --server-env) SERVER_ENV="${2:-}"; shift 2 ;;
    --port)       PORT="${2:-}"; shift 2 ;;
    --with-overviews) WITH_OVERVIEWS="--with-overviews"; shift ;;
    --phase)      PHASES="${2:-}"; shift 2 ;;
    --only)       ONLY="${2:-}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown argument: '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

# ── Preconditions ─────────────────────────────────────────────────────
fail() { echo "error: $*" >&2; exit 2; }

[ -n "$SERVER_ENV" ] || fail "--server-env is required (path to the backend .env)."
[ -f "$SERVER_ENV" ] || fail "server env file not found: $SERVER_ENV"
SERVER_ENV="$(cd "$(dirname "$SERVER_ENV")" && pwd)/$(basename "$SERVER_ENV")"  # absolute
[ -x "$VENV_PYTHON" ] || fail "backend venv not found at $VENV_PYTHON. Create it first (run scripts/run.sh once, or 'cd backend && uv sync')."
[ -f "$SUITE" ] || fail "regression suite not found at $SUITE"
command -v python3 >/dev/null 2>&1 || fail "python3 (for the suite) not found on PATH."

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] && [ -z "${AWS_PROFILE:-}" ]; then
  echo "warning: no AWS_ACCESS_KEY_ID or AWS_PROFILE in the environment; anything" >&2
  echo "         past the read-only checks will fail without AWS access." >&2
fi

# A server .env with unreplaced <PLACEHOLDER> values would start a broken
# backend; catch it the way run.sh does.
if grep -qE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=.*<[^>]+>' "$SERVER_ENV"; then
  fail "server env $SERVER_ENV still has <PLACEHOLDER> values; replace them first."
fi

mkdir -p "$LOG_DIR"

MODES=(off on)
case "$ONLY" in
  "")       : ;;
  off)      MODES=(off) ;;
  on)       MODES=(on) ;;
  *)        fail "--only must be 'off' or 'on', got '$ONLY'" ;;
esac

# ── Backend lifecycle ─────────────────────────────────────────────────
BACKEND_PID=""

stop_backend() {
  [ -n "$BACKEND_PID" ] || return 0
  if kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$BACKEND_PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  BACKEND_PID=""
}

# Always clean up the backend, even on Ctrl-C or an unexpected error.
trap 'stop_backend' EXIT INT TERM

wait_for_health() {   # $1 = base url
  local url="$1/api/health"
  for _ in $(seq 1 60); do
    if python3 - "$url" <<'PY' 2>/dev/null; then
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
      return 0
    fi
    # Bail early if the backend process already died.
    [ -n "$BACKEND_PID" ] && ! kill -0 "$BACKEND_PID" 2>/dev/null && return 1
    sleep 1
  done
  return 1
}

start_backend() {   # $1 = mode (off|on), $2 = logfile
  local mode="$1" logfile="$2"
  local authz="false"
  [ "$mode" = "on" ] && authz="true"

  # Config for this mode. Exports win over the server .env (override=False), and
  # CODEKB_DOTENV points the backend at that .env for everything else.
  #   - auth OFF + no Cognito  => X-User-Identity honored => two identities work
  #   - set BOTH the canonical name and its alias to the same value so a value in
  #     the server .env can't create a conflict (which fails closed to "on").
  (
    cd "$BACKEND_DIR" || exit 1
    export CODEKB_DOTENV="$SERVER_ENV"
    export CODEKB_REQUIRE_AUTHENTICATION=false
    export CODEKB_REQUIRE_AUTH=false
    export CODEKB_REQUIRE_AUTHORIZATION="$authz"
    # Force Cognito off so X-User-Identity is the identity source.
    export CODEKB_COGNITO_USER_POOL_ID=""
    export CODEKB_COGNITO_APP_CLIENT_IDS=""
    export CODEKB_COGNITO_MCP_CLIENT_ID=""
    export CODEKB_COGNITO_DOMAIN_URL=""
    exec "$VENV_PYTHON" -m uvicorn src.api.app:app --host 127.0.0.1 --port "$PORT"
  ) >"$logfile" 2>&1 &
  BACKEND_PID=$!
}

# ── Run one mode ──────────────────────────────────────────────────────
run_mode() {   # $1 = mode; returns the suite's exit code (or 1 on startup fail)
  local mode="$1"
  local base_url="http://127.0.0.1:$PORT"
  local server_log="$LOG_DIR/server-$mode.log"

  echo ""
  echo "══════════════════════════════════════════════════════════════════"
  echo "  MODE: authorization $(echo "$mode" | tr '[:lower:]' '[:upper:]')"
  echo "  backend log: $server_log"
  echo "══════════════════════════════════════════════════════════════════"

  start_backend "$mode" "$server_log"
  if ! wait_for_health "$base_url"; then
    echo "error: backend did not become healthy for mode '$mode'. Last log lines:" >&2
    tail -n 25 "$server_log" >&2
    stop_backend
    return 1
  fi
  echo "backend healthy (pid $BACKEND_PID) on $base_url"

  # Client env for the suite: local base URL, X-User-Identity (no token).
  local rc=0
  (
    unset CODEKB_TOKEN CODEKB_SECOND_TOKEN
    export CODEKB_BASE_URL="$base_url"
    # shellcheck disable=SC2086
    exec python3 "$SUITE" --no-color $WITH_OVERVIEWS ${PHASES:+--phase "$PHASES"}
  )
  rc=$?

  stop_backend
  # Give the port a moment to free before the next mode binds it.
  sleep 1
  return $rc
}

# ── Drive the matrix ──────────────────────────────────────────────────
declare -A RESULT
overall=0
for mode in "${MODES[@]}"; do
  if run_mode "$mode"; then
    RESULT[$mode]="PASS"
  else
    RESULT[$mode]="FAIL"
    overall=1
  fi
done

echo ""
echo "══════════════════════════════════════════════════════════════════"
echo "  Matrix summary"
for mode in "${MODES[@]}"; do
  printf "    authorization %-3s : %s\n" "$mode" "${RESULT[$mode]}"
done
echo "    backend logs      : $LOG_DIR/server-<mode>.log"
echo "══════════════════════════════════════════════════════════════════"

exit "$overall"

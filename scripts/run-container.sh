#!/usr/bin/env bash
#
# Run the packaged image locally, to check that something behaves the same
# packaged as it does under run.sh. Serves the baked-in UI and the API on
# http://127.0.0.1:8000, in the foreground, with no reload.
#
#   1. Check .env: it must exist and have no <PLACEHOLDER> values left.
#   2. Remove a leftover codekb-local container, if any.
#   3. Rebuild codekb:local with build-image.sh, so the container never runs
#      stale code. The layer cache keeps an unchanged rebuild cheap.
#   4. Run it as codekb-local, published on 127.0.0.1:8000 only, with .env as
#      --env-file. Ctrl-C stops and removes it.
#
# ─────────────────────────────────────────────────────────────────────────────
# BEFORE YOU RUN (one-time setup)
#
#   1. Install Docker or finch, and make sure it is running.
#   2. Create the AWS resources (docs/local.md step 3, infra/cli/setup_aws.sh) and
#      have credentials for that account. Any of these works; when several are
#      present, the SDK uses the first, in this order:
#        - exported AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN:
#          forwarded by name. They don't refresh.
#        - ~/.aws (static keys, profiles, SSO): mounted read-write so SSO can
#          refresh. AWS_PROFILE is forwarded when set. A credential_process
#          that runs a host program won't work in the container.
#        - EC2 instance role: nothing to pass. Needs an IMDS hop limit of 2,
#          because Docker's bridge adds a hop.
#   3. cp .env.example .env at the repo root and fill it in.
#   4. The image's UI has sign-in off, so for a single-user run set
#      CODEKB_REQUIRE_AUTHENTICATION=false. Every endpoint is then open to
#      anyone who can reach the port, so keep it loopback-only.
#
# OPTIONS
#
#   docker | finch       container runtime (default: docker)
#   --arch amd64|arm64   image architecture, passed to build-image.sh
#                        (default: this machine's)
#   -h, --help           print this header
#
# Usage:
#     ./scripts/run-container.sh
#     ./scripts/run-container.sh finch --arch amd64
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"

PORT=8000
# Tag that build-image.sh produces.
IMAGE_REF="codekb:local"
CONTAINER_NAME="codekb-local"

# Prints the header comment, so --help and the docs can't drift.
usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

DOCKER="docker"
ARCH=""
while [ $# -gt 0 ]; do
  case "$1" in
    docker|finch) DOCKER="$1" ;;
    --arch)
      [ $# -ge 2 ] || { echo "Error: --arch needs a value: amd64 or arm64." >&2; exit 1; }
      ARCH="$2"
      shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown argument: '$1'" >&2
       echo "" >&2
       usage >&2
       exit 1 ;;
  esac
  shift
done

command -v "$DOCKER" >/dev/null 2>&1 || {
  echo "Error: '$DOCKER' not found on PATH." >&2
  echo "  Install Docker, or pass 'finch' if that's your runtime." >&2
  exit 1
}

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

# Matched as text: .env.example marks values to replace as <YOUR_SOMETHING>.
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

# ── Container ─────────────────────────────────────────────────────────
# A leftover container with this name would make `run` fail.
if "$DOCKER" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER_NAME"; then
  echo "==> Removing the previous $CONTAINER_NAME container..."
  "$DOCKER" rm -f "$CONTAINER_NAME" >/dev/null
fi

build_args=("$DOCKER")
[ -n "$ARCH" ] && build_args+=(--arch "$ARCH")
bash "$SCRIPT_DIR/build-image.sh" "${build_args[@]}"

# ── Credentials ───────────────────────────────────────────────────────
# Make every source reachable; the SDK picks in its usual order: exported keys,
# then ~/.aws, then the instance role (IMDS, nothing to pass).
run_args=(--rm --name "$CONTAINER_NAME" -p "127.0.0.1:$PORT:8000" --env-file "$ENV_FILE")
# By name, so secret values stay out of `ps`.
for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_REGION; do
  [ -n "${!var:-}" ] && run_args+=(-e "$var")
done
# Only if present, or Docker creates it on the host as root. Read-write for SSO refresh.
[ -d "$HOME/.aws" ] && run_args+=(-v "$HOME/.aws:/home/codekb/.aws")

echo ""
echo "==> Starting $CONTAINER_NAME on http://127.0.0.1:$PORT"
echo "    The frontend is baked into the image; there is no separate dev server."
echo "    Ctrl-C stops the container."
echo ""
exec "$DOCKER" run "${run_args[@]}" "$IMAGE_REF"

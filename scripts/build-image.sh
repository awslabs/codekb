#!/usr/bin/env bash
#
# Build the container image from the repo-root Dockerfile and tag it
# codekb:local. run-container.sh runs that tag.
#
# The Dockerfile is multi-stage: it compiles the frontend and bakes it into the
# image, so the host needs no Node.js. The first build takes several minutes
# because the parser libraries compile; later builds reuse the layer cache. The
# UI's sign-in is off in this image (the VITE_* auth build args default off);
# deploy.sh builds the same Dockerfile with them set.
#
# ─────────────────────────────────────────────────────────────────────────────
# BEFORE YOU RUN (one-time setup)
#
#   1. Install Docker or finch, and make sure it is running.
#   2. The host needs access to public.ecr.aws, PyPI and npm.
#
# OPTIONS
#
#   docker | finch       container runtime (default: docker)
#   --arch amd64|arm64   target architecture (default: this machine's, so
#                        the image runs without emulation)
#   -h, --help           print this header
#
# Usage:
#     ./scripts/build-image.sh
#     ./scripts/build-image.sh finch --arch amd64
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# run-container.sh runs this tag.
IMAGE_REF="codekb:local"

# Prints the header comment, so --help and the docs can't drift.
usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

DOCKER="docker"
# Native by default: an emulated image builds and parses much slower.
case "$(uname -m)" in
  arm64|aarch64) ARCH="arm64" ;;
  *)             ARCH="amd64" ;;
esac
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

case "$ARCH" in
  amd64|arm64) ;;
  *) echo "Error: --arch must be amd64 or arm64, not '$ARCH'." >&2; exit 1 ;;
esac

command -v "$DOCKER" >/dev/null 2>&1 || {
  echo "Error: '$DOCKER' not found on PATH." >&2
  echo "  Install Docker, or pass 'finch' if that's your runtime." >&2
  exit 1
}

echo "==> Building $IMAGE_REF for linux/$ARCH with $DOCKER (context: repository root)..."
"$DOCKER" build --build-arg BASE_PLATFORM="linux/$ARCH" -t "$IMAGE_REF" "$REPO_ROOT"
